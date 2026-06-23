#!/usr/bin/env python3
"""
watcher.py — Full Pipeline Watcher (Playwright edition)
=========================================================
Two input sources, same Gem → GCS → notify path:
  • Google Drive folder  — buttonmatcher's images (and any user-dropped photos)
  • GCS pipeline/input/   — ebayscout's lots (a service account can't own Drive
                            files on personal Gmail, so ebayscout uploads to GCS)

1. Polls the shared Drive folder AND the GCS input prefix for new images
2. Downloads them to a local staging dir
3. Uses Playwright to open your Gemini Gem, paste the image, capture response
4. Uploads original image + JSON response to Google Cloud Storage (pipeline/output/)
5. Notifies the right service (ebayscout OR buttonmatcher, routed by the image
   filename prefix) so it runs detection/matching and posts to Slack
6. Drive inputs → moved to Done/ in Drive; GCS inputs → deleted from pipeline/input/

Run:  python3 watcher.py
Config loaded from config.json in the same directory.
"""

import os, sys, json, time, base64, mimetypes, logging, threading, shutil, http.client, zlib
from pathlib import Path
import urllib.request, urllib.parse, urllib.error

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent / "watcher.log"),
    ],
)
log = logging.getLogger("pipeline")

# ── Config ─────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"

def _config_path() -> Path:
    """Resolve which config.json to use, so several workers can share one
    watcher.py from the same dir: `--config <path>` arg, else $WATCHER_CONFIG,
    else the default config.json next to this script."""
    argv = sys.argv
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 < len(argv):
            return Path(argv[i + 1]).expanduser()
    env = os.environ.get("WATCHER_CONFIG")
    return Path(env).expanduser() if env else CONFIG_PATH

def load_config():
    path = _config_path()
    if not path.exists():
        log.error(f"config not found: {path} (copy config.example.json and fill it in)")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)

# ── State ──────────────────────────────────────────────────────
seen_ids: set = set()
state_lock = threading.Lock()

# Failsafe: count CONSECUTIVE Gem failures (no parseable JSON came back) across
# both input paths. When the Gem runs out of tokens it stops returning the
# prompt's JSON object, so a run of these means the Gem is exhausted and the
# watcher should halt instead of churning through the queue. Reset on any success.
_consecutive_gem_failures = 0

# True once the Gem has produced at least one good JSON in THIS process — i.e.
# the session/Gem are confirmed working right now. Gate poison-input quarantine
# on this so a global outage (Gem down for everyone) never quarantines the queue.
_had_success_this_run = False

# Gemini's browser auth/session error ("Something went wrong (1095)"). This is a
# dead Google session, NOT out-of-tokens — retrying can't fix it, only --login can.
_AUTH_ERROR_SENTINEL = "ERROR: GEMINI_AUTH_1095"

# ── Google Auth ────────────────────────────────────────────────
def get_access_token(cfg: dict, scopes: list) -> str:
    from google.oauth2 import service_account
    import google.auth.transport.requests
    creds = service_account.Credentials.from_service_account_info(
        cfg["service_account"], scopes=scopes
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token

def get_drive_token(cfg):
    return get_access_token(cfg, ["https://www.googleapis.com/auth/drive"])

def get_gcs_token(cfg):
    return get_access_token(cfg, ["https://www.googleapis.com/auth/devstorage.read_write"])

# ── Google Drive ───────────────────────────────────────────────
def drive_list_files(folder_id: str, token: str) -> list:
    q = f"'{folder_id}' in parents and mimeType contains 'image/' and trashed = false"
    url = (
        "https://www.googleapis.com/drive/v3/files"
        f"?q={urllib.parse.quote(q)}"
        "&fields=files(id,name,mimeType,createdTime)"
        "&orderBy=createdTime&pageSize=50"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read()).get("files", [])

def drive_download(file_id: str, dest_path: Path, token: str):
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r, open(dest_path, "wb") as f:
        shutil.copyfileobj(r, f)

def drive_get_or_create_done_folder(parent_folder_id: str, token: str) -> str:
    q = (
        f"'{parent_folder_id}' in parents"
        " and mimeType = 'application/vnd.google-apps.folder'"
        " and name = 'Done' and trashed = false"
    )
    url = f"https://www.googleapis.com/drive/v3/files?q={urllib.parse.quote(q)}&fields=files(id,name)"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        files = json.loads(r.read()).get("files", [])
    if files:
        return files[0]["id"]
    meta = json.dumps({
        "name": "Done",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_folder_id],
    }).encode()
    req = urllib.request.Request(
        "https://www.googleapis.com/drive/v3/files?fields=id",
        data=meta,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        folder_id = json.loads(r.read())["id"]
        log.info(f"Created Done folder: {folder_id}")
        return folder_id

def drive_move_to_done(file_id: str, current_parent: str, done_folder_id: str, token: str):
    parsed = urllib.parse.urlparse(
        f"https://www.googleapis.com/drive/v3/files/{file_id}"
        f"?addParents={done_folder_id}&removeParents={current_parent}&fields=id,parents"
    )
    conn = http.client.HTTPSConnection(parsed.netloc)
    conn.request("PATCH", parsed.path + "?" + parsed.query, body=b"{}",
                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    resp = conn.getresponse()
    if resp.status not in (200, 204):
        raise RuntimeError(f"Drive PATCH failed: {resp.status} {resp.read()}")
    log.info(f"Moved {file_id} to Done folder")

# ── GCS Upload ─────────────────────────────────────────────────
def upload_to_gcs(local_path: Path, mime: str, gcs_path: str, cfg: dict, token: str):
    bucket = cfg["gcs_bucket"]
    url = (
        f"https://storage.googleapis.com/upload/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o"
        f"?uploadType=media&name={urllib.parse.quote(gcs_path)}"
    )
    with open(local_path, "rb") as f:
        data = f.read()
    parsed = urllib.parse.urlparse(url)
    conn = http.client.HTTPSConnection(parsed.netloc)
    conn.request("POST", parsed.path + "?" + parsed.query, body=data,
                 headers={"Authorization": f"Bearer {token}", "Content-Type": mime,
                          "Content-Length": str(len(data))})
    resp = conn.getresponse()
    body = resp.read()
    if resp.status not in (200, 201):
        raise RuntimeError(f"GCS upload failed {resp.status}: {body}")
    log.info(f"Uploaded to GCS: {gcs_path}")

def upload_json_to_gcs(payload: dict, gcs_path: str, cfg: dict, token: str):
    bucket = cfg["gcs_bucket"]
    url = (
        f"https://storage.googleapis.com/upload/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o"
        f"?uploadType=media&name={urllib.parse.quote(gcs_path)}"
    )
    data = json.dumps(payload, indent=2).encode()
    parsed = urllib.parse.urlparse(url)
    conn = http.client.HTTPSConnection(parsed.netloc)
    conn.request("POST", parsed.path + "?" + parsed.query, body=data,
                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                          "Content-Length": str(len(data))})
    resp = conn.getresponse()
    body = resp.read()
    if resp.status not in (200, 201):
        raise RuntimeError(f"GCS JSON upload failed {resp.status}: {body}")
    log.info(f"Uploaded JSON to GCS: {gcs_path}")

# ── GCS input (ebayscout lots bypass Drive's SA storage limit) ─────────────
def gcs_list_objects(prefix: str, cfg: dict, token: str) -> list:
    """List objects under a bucket prefix (name + generation for dedup)."""
    bucket = cfg["gcs_bucket"]
    url = (
        f"https://storage.googleapis.com/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o"
        f"?prefix={urllib.parse.quote(prefix)}&fields=items(name,generation)"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read()).get("items", [])

def gcs_download_object(object_name: str, dest_path: Path, cfg: dict, token: str):
    bucket = cfg["gcs_bucket"]
    url = (
        f"https://storage.googleapis.com/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o/"
        f"{urllib.parse.quote(object_name, safe='')}?alt=media"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r, open(dest_path, "wb") as f:
        shutil.copyfileobj(r, f)

def gcs_delete_object(object_name: str, cfg: dict, token: str):
    bucket = cfg["gcs_bucket"]
    path = (
        f"/storage/v1/b/{urllib.parse.quote(bucket, safe='')}/o/"
        f"{urllib.parse.quote(object_name, safe='')}"
    )
    conn = http.client.HTTPSConnection("storage.googleapis.com")
    conn.request("DELETE", path, headers={"Authorization": f"Bearer {token}"})
    resp = conn.getresponse(); resp.read()
    if resp.status not in (200, 204):
        raise RuntimeError(f"GCS delete failed {resp.status}")
    log.info(f"Deleted GCS input: {object_name}")

# ── Notify the matching service ────────────────────────────────
def notify_service(url: str, secret: str, object_name: str, label: str) -> None:
    """POST {"object": <bucket-relative .response.json path>} with the shared
    secret header to a service's /pipeline/notify endpoint.  A 204 means accepted.

    Fail-soft and retried — a notify failure never blocks the pipeline (the object
    is already in GCS; you can re-fire manually).  Skips quietly if url/secret are
    unset so a single-service deployment keeps working.
    """
    if not url or not secret:
        log.info(f"{label} notify skipped (url / secret not set in config.json)")
        return
    data = json.dumps({"object": object_name}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json",
                         "X-Pipeline-Secret": secret},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                log.info(f"Notified {label} ({resp.status}) for {object_name}")
            return                                   # 204 = accepted
        except urllib.error.HTTPError as e:
            log.warning(f"{label} notify HTTP {e.code} for {object_name}")
            if e.code < 500:
                return                               # 4xx (auth/bad name) — no retry
        except Exception as e:
            log.warning(f"{label} notify attempt {attempt + 1} failed: {e}")
        time.sleep(2 ** attempt)                     # 1s, 2s, 4s backoff

def notify_pipeline(object_name: str, fname: str, cfg: dict) -> None:
    """Route a finished .response.json to exactly one service by the image's
    filename prefix.  Files the eBay-Scout uploads are named
    ``ebayscout__<key>.png`` (config.PIPELINE_OBJECT_PREFIX in ebayscout) — those
    go to ebayscout; everything else stays with buttonmatcher, as before.  The
    partition is exclusive so nothing is double-processed.

    Each service self-guards (ebayscout ack-drops objects with no ebayscout prefix
    and no pending-context blob), so a misroute is harmless — but routing here
    keeps the notify traffic clean.
    """
    ebay_prefix = cfg.get("ebayscout_filename_prefix", "ebayscout__")
    if fname.startswith(ebay_prefix):
        notify_service(
            cfg.get("ebayscout_notify_url"),
            # ebayscout may share buttonmatcher's secret; fall back to it if a
            # dedicated ebayscout secret isn't configured.
            cfg.get("ebayscout_pipeline_shared_secret") or cfg.get("pipeline_shared_secret"),
            object_name, "ebayscout",
        )
    else:
        notify_service(
            cfg.get("buttonmatcher_notify_url"),
            cfg.get("pipeline_shared_secret"),
            object_name, "buttonmatcher",
        )

# ── Playwright → Gemini ────────────────────────────────────────
def process_with_gemini(local_path: Path, fname: str, cfg: dict) -> str:
    """
    Opens the Gem URL in a Playwright browser, uploads the image,
    waits for the response to stop streaming, and returns the response text.
    Uses the user's real Chrome profile so they're already logged in.
    """
    from playwright.sync_api import sync_playwright
    import tempfile

    gem_url    = cfg["gem_url"]
    prompt     = cfg.get("custom_prompt", "")
    # Find Chrome on Chromebook
    chrome_paths = [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
        "/usr/bin/google-chrome",
    ]
    chrome_exe = next((p for p in chrome_paths if p and Path(p).exists()), None)
    if not chrome_exe:
        raise RuntimeError("Could not find Chrome/Chromium executable")

    log.info(f"Launching browser: {chrome_exe}")

    with sync_playwright() as p:
        # Launch with persistent context so we're logged in to Google. Each worker
        # MUST use its own profile dir (Chrome locks a profile to one process), so
        # this is per-config — point each parallel watcher at a different account's
        # profile via "user_data_dir" in its config.json.
        user_data_dir = cfg.get(
            "user_data_dir", str(Path.home() / ".config" / "playwright-gemini-profile"))
        context = p.chromium.launch_persistent_context(
            user_data_dir,
            executable_path=chrome_exe,
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        page = context.new_page()
        log.info(f"Navigating to Gem: {gem_url}")
        page.goto(gem_url, wait_until="domcontentloaded", timeout=30000)

        # Wait for Gemini input to appear
        log.info("Waiting for Gemini input...")
        input_sel = 'div[contenteditable="true"], rich-textarea .ql-editor, .ql-editor'
        page.wait_for_selector(input_sel, timeout=20000)
        page.wait_for_timeout(2000)

        # Upload image via file input if available, otherwise clipboard
        log.info(f"Uploading image: {local_path}")
        try:
            # Click "Upload & tools" button to reveal file chooser
            upload_btn = page.get_by_role("button", name="Upload & tools")
            upload_btn.click()
            page.wait_for_timeout(1000)

            # After clicking, look for an "Upload file" or similar option
            upload_file_opt = page.get_by_role("menuitem", name="Upload file")
            if not upload_file_opt:
                upload_file_opt = page.get_by_text("Upload file")

            with page.expect_file_chooser(timeout=5000) as fc_info:
                upload_file_opt.click()
            fc_info.value.set_files(str(local_path))
            log.info("Image attached via Upload & tools menu")
        except Exception as e:
            log.warning(f"Upload menu failed ({e}), trying direct file input")
            try:
                file_input = page.query_selector('input[type="file"]')
                if file_input:
                    file_input.set_input_files(str(local_path))
                    log.info("Image attached via file input")
                else:
                    _paste_image_via_clipboard(page, local_path)
            except Exception as e2:
                log.warning(f"File input failed ({e2}), trying clipboard")
                _paste_image_via_clipboard(page, local_path)

        page.wait_for_timeout(1500)

        # Click send
        log.info("Submitting to Gemini...")
        send_sel = 'button[aria-label*="Send"], button[data-test-id="send-button"]'
        send_btn = page.wait_for_selector(send_sel, timeout=10000)
        send_btn.click()

        # Wait for response to finish streaming
        log.info("Waiting for Gemini response...")
        response_text = _wait_for_response(page)
        log.info(f"Got response ({len(response_text)} chars)")

        context.close()
        return response_text

def _paste_image_via_clipboard(page, local_path: Path):
    """Copy image to clipboard using xclip then paste into Gemini."""
    import subprocess
    mime = mimetypes.guess_type(str(local_path))[0] or "image/png"
    result = subprocess.run(
        ["xclip", "-selection", "clipboard", "-t", mime, "-i", str(local_path)],
        capture_output=True
    )
    if result.returncode != 0:
        # Try xdotool approach
        result2 = subprocess.run(
            ["xdg-open", str(local_path)],
            capture_output=True
        )
        raise RuntimeError(f"xclip failed: {result.stderr.decode()}")

    input_sel = 'div[contenteditable="true"], .ql-editor'
    input_el = page.query_selector(input_sel)
    if input_el:
        input_el.click()
        page.keyboard.press("Control+v")
        log.info("Image pasted via clipboard")

def _page_has_auth_error(page) -> bool:
    """True if the Gemini page is showing the 1095 / 'Something went wrong'
    auth-session banner instead of answering. Cheap, fail-open (False on error)."""
    try:
        body = page.inner_text("body", timeout=1000).lower()
    except Exception:
        return False
    return "something went wrong" in body and "1095" in body or "error 1095" in body


def _is_auth_error(text) -> bool:
    """Did this reply text signal the 1095 auth-session error?"""
    if text == _AUTH_ERROR_SENTINEL:
        return True
    low = (text or "").lower()
    return "1095" in low and "something went wrong" in low


def _wait_for_response(page, timeout_ms=180000) -> str:
    """Poll until Gemini's response is a COMPLETE JSON object and stops changing.

    Gemini shows a transient "Analyzing…" placeholder while it thinks.  That text
    is *stable* but it is NOT the answer — the old logic accepted it and closed the
    browser, cutting Gemini off mid-thought (raw_response = "…Analyzing").  So we
    only accept a response once it actually contains a JSON object ('{' … '}'),
    has been unchanged for a few cycles, and shows no thinking/streaming indicator.
    A placeholder has no braces, so it can never satisfy this — we keep waiting."""
    response_sels = [
        ".response-container",
        ".model-response",
        "[data-message-author-role='model']",
        ".conversation-turn .markdown",
        "model-response",
        ".gemini-response",
    ]
    deadline = time.time() + timeout_ms / 1000
    last_text = ""
    stable_count = 0
    STABLE_NEEDED = 4  # 4 × 1.5s = 6s unchanged

    # Give Gemini time to start responding
    page.wait_for_timeout(3000)

    while time.time() < deadline:
        # Check loading / thinking / streaming indicator is gone
        loading = page.query_selector(
            '[aria-label*="thinking"], .loading-indicator, [data-is-streaming="true"]'
        )

        # Get latest response text
        text = ""
        for sel in response_sels:
            els = page.query_selector_all(sel)
            if els:
                text = els[-1].inner_text().strip()
                break

        # Bail out fast on the 1095 auth/session error — it never resolves by
        # waiting, and we want the caller to halt with the re-login message
        # rather than burn the full timeout on a dead session.
        if _page_has_auth_error(page):
            return _AUTH_ERROR_SENTINEL

        # The final answer is a JSON object; "Analyzing…" and other placeholders
        # have no braces, so they can never count as done — keep waiting for JSON.
        looks_like_json = "{" in text and "}" in text

        if not loading and looks_like_json and text == last_text:
            stable_count += 1
            if stable_count >= STABLE_NEEDED:
                return text
        else:
            last_text = text
            stable_count = 0

        page.wait_for_timeout(1500)

    # Timed out without ever seeing a JSON object (still "Analyzing", or an error)
    return last_text or "ERROR: Response timed out (no JSON)"

# ── Multi-worker sharding ──────────────────────────────────────
def _owns(name: str, cfg: dict) -> bool:
    """True if THIS worker is responsible for `name`.

    Run one watcher per Gemini account (each account in a family plan has its own
    token budget). All workers poll the SAME input prefixes, but each processes
    only its deterministic crc32 shard, so the lots are partitioned with zero
    overlap and no locking — N workers ≈ N× throughput. crc32 (not the builtin
    hash, which is per-process salted) is stable across processes so the shards
    never collide. worker_count=1 (the default) owns everything."""
    count = int(cfg.get("worker_count", 1) or 1)
    if count <= 1:
        return True
    index = int(cfg.get("worker_index", 0) or 0)
    base = name.rsplit("/", 1)[-1]
    return (zlib.crc32(base.encode("utf-8")) % count) == index


# ── Gem-exhaustion failsafe ────────────────────────────────────
def _has_parseable_json(response_text: str) -> bool:
    """True if a JSON object can be recovered from the Gem's reply — mirrors
    ebayscout's tolerant pipeline_ingest._loads_loose. The Gem commonly wraps the
    JSON in a chat preamble/prose ("Button Identifier said\\n\\n{…}") or markdown
    fences, so a strict whole-string parse is NOT a reliable failure signal; we
    also try the OUTERMOST {…} substring. Only a reply with no recoverable object
    (a token-exhaustion banner, or the timeout sentinel) counts as a failure."""
    t = (response_text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    t = t.strip()
    try:
        json.loads(t)
        return True
    except Exception:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a != -1 and b > a:
        try:
            json.loads(t[a:b + 1])
            return True
        except Exception:
            return False
    return False


def _parse_gem_response(response_text: str) -> tuple:
    """Parse the Gem's reply. Returns (response_json, gem_ok).

    `response_json` is the payload we upload, kept identical to the proven prior
    behavior: the parsed dict when the whole reply is clean JSON, else
    {"raw_response": text} (ebayscout's parser digs the JSON back out of that).

    `gem_ok` is the failsafe signal and uses the TOLERANT check
    (_has_parseable_json), so a valid object wrapped in prose/fences is a success,
    not a failure. A reply with no recoverable JSON (out of tokens / timeout) is
    the only thing that counts against the consecutive-failure limit."""
    clean = (response_text or "").strip()
    if clean.startswith("```"):
        clean = "\n".join(clean.split("\n")[1:])
    if clean.endswith("```"):
        clean = "\n".join(clean.split("\n")[:-1])
    try:
        response_json = json.loads(clean.strip())
    except json.JSONDecodeError:
        response_json = {"raw_response": response_text}
    return response_json, _has_parseable_json(response_text)


def _notify_slack_webhook(cfg: dict, text: str) -> None:
    """Best-effort Slack ping via an incoming-webhook URL (optional; the watcher
    has no Slack bot token otherwise). Silent if slack_webhook_url isn't set."""
    url = cfg.get("slack_webhook_url")
    if not url:
        return
    try:
        data = json.dumps({"text": text}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log.warning(f"Slack webhook notify failed: {e}")


def _fail_counts_path(cfg) -> Path:
    staging_dir = Path(cfg.get("staging_dir", str(Path.home() / "buttons" / "staging")))
    return staging_dir / "input_fail_counts.json"


def _load_fail_counts(cfg) -> dict:
    try:
        with open(_fail_counts_path(cfg)) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_fail_counts(cfg, counts) -> None:
    try:
        p = _fail_counts_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(counts, f)
    except Exception as e:
        log.warning(f"could not persist input fail counts: {e}")


def _clear_input_failure(object_name: str, cfg: dict) -> None:
    """A success — forget this input's prior failures."""
    base = object_name.rsplit("/", 1)[-1]
    counts = _load_fail_counts(cfg)
    if counts.pop(base, None) is not None:
        _save_fail_counts(cfg, counts)


def _note_input_failure(object_name, local_path, mime, cfg) -> None:
    """Backstop for a single POISON input (one the Gem can never turn into JSON
    even though it's working): after quarantine_limit failures across runs, move
    it out of the input prefix so it stops blocking the queue forever.

    Gated on _had_success_this_run, so a global outage (Gem down for everyone)
    NEVER quarantines the whole queue — that case is handled by the consecutive-
    failure halt and the 1095 re-login halt. quarantine_limit<=0 disables it.
    Call this BEFORE deleting local_path (the local copy is re-uploaded aside)."""
    limit = int(cfg.get("quarantine_limit", 8) or 0)
    if limit <= 0:
        return
    base = object_name.rsplit("/", 1)[-1]
    counts = _load_fail_counts(cfg)
    counts[base] = counts.get(base, 0) + 1
    n = counts[base]
    _save_fail_counts(cfg, counts)
    if n < limit or not _had_success_this_run:
        return
    try:
        qprefix = cfg.get("gcs_quarantine_prefix", "pipeline/quarantine")
        token = get_gcs_token(cfg)
        if local_path and Path(local_path).exists():
            upload_to_gcs(local_path, mime, f"{qprefix}/{base}", cfg, token)
        gcs_delete_object(object_name, cfg, token)
        counts.pop(base, None)
        _save_fail_counts(cfg, counts)
        msg = (f"🚮 Quarantined {base} after {n} failed Gem attempts (the Gem "
               f"works on other inputs, so this one is poison) → {qprefix}/. "
               f"Removed from the input queue.")
        log.error(msg)
        _notify_slack_webhook(cfg, msg)
    except Exception as e:
        log.error(f"Failed to quarantine {base}: {e}", exc_info=True)


def _register_gem_outcome(gem_ok: bool, cfg: dict, response_text: str = "") -> None:
    """Track Gem outcomes and HALT the watcher when it can't make progress.

    - success: reset the failure streak and mark the Gem confirmed-working.
    - 1095 auth error: halt IMMEDIATELY with re-login instructions — retrying a
      dead Google session is pointless and just loops.
    - otherwise (no parseable JSON, e.g. out of tokens): count consecutive
      failures and halt once gem_empty_limit pile up.

    On a failure the caller has already left the input queued (not uploaded, not
    notified, not deleted), so a restart resumes draining it — nothing is lost."""
    global _consecutive_gem_failures, _had_success_this_run
    if gem_ok:
        with state_lock:
            _consecutive_gem_failures = 0
            _had_success_this_run = True
        return

    if _is_auth_error(response_text):
        msg = ("🛑 Watcher halted: Gemini returned an auth/session error "
               "(1095 / 'Something went wrong') — this is NOT out-of-tokens, the "
               "Google session for this profile is dead. Re-login in a terminal: "
               "`python3 watcher.py --login` with this worker's config, then "
               "restart. Inputs are still queued — nothing lost.")
        log.error(msg)
        _notify_slack_webhook(cfg, msg)
        sys.exit(1)

    limit = int(cfg.get("gem_empty_limit", 5))
    with state_lock:
        _consecutive_gem_failures += 1
        n = _consecutive_gem_failures
    log.warning(f"Gem returned no parseable JSON ({n}/{limit} consecutive). "
                f"Input left queued for retry.")
    if n >= limit:
        msg = (f"🛑 Watcher halted: {n} consecutive Gem failures — the Gem is "
               f"likely out of tokens. The unprocessed inputs are still queued; "
               f"refill the Gem and restart the watcher to resume.")
        log.error(msg)
        _notify_slack_webhook(cfg, msg)
        sys.exit(1)


# ── Process one file end-to-end ────────────────────────────────
def process_file(f: dict, cfg: dict):
    fid   = f["id"]
    fname = f["name"]
    mime  = f.get("mimeType", mimetypes.guess_type(fname)[0] or "image/jpeg")
    staging_dir = Path(cfg.get("staging_dir", str(Path.home() / "buttons" / "staging")))
    staging_dir.mkdir(parents=True, exist_ok=True)
    local_path = staging_dir / fname

    try:
        # 1. Download from Drive
        log.info(f"Downloading {fname}...")
        token_drive = get_drive_token(cfg)
        drive_download(fid, local_path, token_drive)
        log.info(f"Downloaded to {local_path}")

        # 2. Send to Gemini via Playwright
        log.info(f"Sending to Gemini: {fname}")
        response_text = process_with_gemini(local_path, fname, cfg)

        # 3. Parse response as JSON. A Gem that's out of tokens returns no JSON;
        #    leave the input in place (don't upload/notify/move-to-Done) so it's
        #    retried on restart, and trip the consecutive-failure failsafe.
        response_json, gem_ok = _parse_gem_response(response_text)
        if not gem_ok:
            log.warning(f"{fname}: Gem returned no JSON — leaving in Drive for retry.")
            local_path.unlink(missing_ok=True)
            _register_gem_outcome(False, cfg, response_text)   # may halt (auth/limit)
            return
        _register_gem_outcome(True, cfg)               # reset the failure streak

        payload = {
            "fileName": fname,
            "driveId": fid,
            "processedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "gemUrl": cfg["gem_url"],
            "response": response_json,
        }

        # 4. Upload to GCS
        log.info(f"Uploading to GCS: {fname}")
        token_gcs = get_gcs_token(cfg)
        prefix = cfg.get("gcs_prefix", "pipeline/output")

        # Save payload JSON to temp file then upload
        json_path = staging_dir / (fname + ".response.json")
        with open(json_path, "w") as jf:
            json.dump(payload, jf, indent=2)

        upload_to_gcs(local_path, mime, f"{prefix}/{fname}", cfg, token_gcs)
        upload_to_gcs(json_path, "application/json", f"{prefix}/{fname}.response.json", cfg, token_gcs)

        # 4b. Notify the right service (fail-soft) — image + JSON are both in GCS.
        # Routed by the image filename prefix: ebayscout__* → ebayscout, else
        # buttonmatcher. The .response.json object name is what /pipeline/notify expects.
        notify_pipeline(f"{prefix}/{fname}.response.json", fname, cfg)

        # 5. Move in Drive to Done/
        log.info(f"Moving {fname} to Done/ in Drive...")
        token_drive2 = get_drive_token(cfg)
        done_folder_id = drive_get_or_create_done_folder(cfg["shared_folder_id"], token_drive2)
        drive_move_to_done(fid, cfg["shared_folder_id"], done_folder_id, token_drive2)

        # 6. Clean up local staging files
        local_path.unlink(missing_ok=True)
        json_path.unlink(missing_ok=True)

        log.info(f"✅ Complete: {fname}")

    except Exception as e:
        log.error(f"❌ Failed to process {fname}: {e}", exc_info=True)
        if local_path.exists():
            local_path.unlink(missing_ok=True)

# ── Process one GCS-input file end-to-end ──────────────────────
def process_gcs_input(object_name: str, cfg: dict):
    """Process one ebayscout lot delivered via the GCS input prefix (instead of
    Drive). Mirrors process_file, but the source is a GCS object and there is no
    Drive 'Done' move — the input object is deleted from GCS when finished."""
    fname = object_name.rsplit("/", 1)[-1]                 # ebayscout__<key>.png
    mime  = mimetypes.guess_type(fname)[0] or "image/png"
    staging_dir = Path(cfg.get("staging_dir", str(Path.home() / "buttons" / "staging")))
    staging_dir.mkdir(parents=True, exist_ok=True)
    local_path = staging_dir / fname
    json_path  = staging_dir / (fname + ".response.json")

    try:
        # 1. download the input image from GCS
        log.info(f"Downloading GCS input {object_name}...")
        gcs_download_object(object_name, local_path, cfg, get_gcs_token(cfg))

        # 2. send to Gemini via Playwright
        response_text = process_with_gemini(local_path, fname, cfg)

        # 3. parse response as JSON. On a Gem failure (out of tokens) DO NOT
        #    upload/notify and DO NOT delete the input object — leave it in
        #    pipeline/input/ so a restart re-processes it — and trip the failsafe.
        response_json, gem_ok = _parse_gem_response(response_text)
        if not gem_ok:
            log.warning(f"{fname}: Gem returned no JSON — leaving GCS input queued for retry.")
            if not _is_auth_error(response_text):
                # Genuine no-JSON (poison/empty): bump the per-input counter and
                # maybe quarantine. Skip for auth errors — those halt below and
                # must never be quarantined (the whole queue would be at risk).
                _note_input_failure(object_name, local_path, mime, cfg)
            local_path.unlink(missing_ok=True)
            json_path.unlink(missing_ok=True)
            _register_gem_outcome(False, cfg, response_text)   # may halt (auth/limit)
            return
        _clear_input_failure(object_name, cfg)         # success — forget past failures
        _register_gem_outcome(True, cfg)               # reset the failure streak

        payload = {
            "fileName": fname,
            "gcsInput": object_name,
            "processedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "gemUrl": cfg["gem_url"],
            "response": response_json,
        }

        # 4. upload image + response to the OUTPUT prefix, keeping the same
        #    basename so the ebayscout__<key> correlation carries through
        out_prefix = cfg.get("gcs_prefix", "pipeline/output")
        with open(json_path, "w") as jf:
            json.dump(payload, jf, indent=2)
        token_gcs = get_gcs_token(cfg)
        upload_to_gcs(local_path, mime, f"{out_prefix}/{fname}", cfg, token_gcs)
        upload_to_gcs(json_path, "application/json",
                      f"{out_prefix}/{fname}.response.json", cfg, token_gcs)

        # 4b. notify (routes to ebayscout by the ebayscout__ filename prefix)
        notify_pipeline(f"{out_prefix}/{fname}.response.json", fname, cfg)

        # 5. delete the input object so it isn't reprocessed
        gcs_delete_object(object_name, cfg, get_gcs_token(cfg))

        # 6. clean up local staging files
        local_path.unlink(missing_ok=True)
        json_path.unlink(missing_ok=True)
        log.info(f"✅ Complete (GCS input): {fname}")

    except Exception as e:
        log.error(f"❌ Failed to process GCS input {object_name}: {e}", exc_info=True)
        local_path.unlink(missing_ok=True)
        json_path.unlink(missing_ok=True)

# ── Main polling loop ──────────────────────────────────────────
def polling_loop():
    cfg = load_config()
    poll_seconds = cfg.get("poll_interval_seconds", 30)
    w_count = int(cfg.get("worker_count", 1) or 1)
    w_index = int(cfg.get("worker_index", 0) or 0)
    log.info(f"Polling shared folder {cfg['shared_folder_id']} every {poll_seconds}s "
             f"(worker {w_index} of {w_count})")

    while True:
        try:
            token = get_drive_token(cfg)
            files = drive_list_files(cfg["shared_folder_id"], token)

            for f in files:
                if not _owns(f["name"], cfg):          # another worker's shard
                    continue
                fid = f["id"]
                with state_lock:
                    if fid in seen_ids:
                        continue
                    seen_ids.add(fid)

                log.info(f"New file: {f['name']} ({fid})")
                # Process synchronously — one file at a time
                process_file(f, cfg)

            # --- GCS input: ebayscout lots (bypasses Drive's SA storage limit) ---
            in_prefix = cfg.get("gcs_input_prefix", "pipeline/input")
            try:
                gtoken = get_gcs_token(cfg)
                for obj in gcs_list_objects(in_prefix, cfg, gtoken):
                    name = obj["name"]
                    if name.endswith("/"):            # skip folder placeholders
                        continue
                    if not _owns(name, cfg):          # another worker's shard
                        continue
                    uid = f"{name}#{obj.get('generation', '')}"
                    with state_lock:
                        if uid in seen_ids:
                            continue
                        seen_ids.add(uid)
                    log.info(f"New GCS input: {name}")
                    process_gcs_input(name, cfg)
            except Exception as e:
                log.error(f"GCS input polling error: {e}", exc_info=True)

        except Exception as e:
            log.error(f"Polling error: {e}", exc_info=True)

        time.sleep(poll_seconds)

def login_mode(cfg: dict) -> None:
    """One-time helper: open the configured profile + Gem URL and wait, so you can
    sign that profile into its Google account by hand. Run once per worker
    (`python3 watcher.py --login` with that worker's config.json), complete the
    Google sign-in in the window, then press Enter here to save + close. The
    profile keeps you logged in for every later watcher run."""
    from playwright.sync_api import sync_playwright
    user_data_dir = cfg.get(
        "user_data_dir", str(Path.home() / ".config" / "playwright-gemini-profile"))
    chrome_exe = next((p for p in (
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        shutil.which("google-chrome") or "", shutil.which("chromium") or "",
    ) if p and Path(p).exists()), None)
    log.info(f"LOGIN: opening profile {user_data_dir} at {cfg['gem_url']}")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir, executable_path=chrome_exe, headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = context.new_page()
        page.goto(cfg["gem_url"], wait_until="domcontentloaded", timeout=60000)
        input(">>> Sign into THIS account in the browser window, open the Gem once, "
              "then press Enter here to save the session and close...\n")
        context.close()
    log.info("LOGIN: session saved.")


# ── Entry point ────────────────────────────────────────────────
if __name__ == "__main__":
    cfg = load_config()

    # Check playwright is installed
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    if "--login" in sys.argv:
        # --login opens a browser and blocks on input() until you press Enter.
        # Under a supervisor/nohup/& with no terminal, input() hits EOF instantly,
        # the process exits, the supervisor relaunches it, and you get a thousand
        # browser windows. Refuse to run without an interactive terminal.
        if not sys.stdin.isatty():
            log.error("--login must be run in an interactive terminal (it waits "
                      "for you to sign in and press Enter). No TTY detected — "
                      "refusing, to avoid spawning runaway browser windows. Run it "
                      "by hand in a foreground shell, outside the watcher's "
                      "launcher/systemd/nohup.")
            sys.exit(2)
        login_mode(cfg)
        sys.exit(0)

    log.info("Pipeline watcher starting (Playwright edition)...")
    polling_loop()
