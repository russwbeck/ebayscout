"""
ebayscout/main.py

Flask + Slack Bolt service for ebayscout.

Handles:
  POST /run-scan      — daily eBay scan (called by Cloud Scheduler)
  GET  /health        — startup health check

Interaction flow:
  Automated daily scan : Cloud Scheduler → POST /run-scan
"""

import contextlib
import datetime
import ipaddress
import os
import time
import threading
import re
import json
import traceback
import uuid
from collections import Counter

import requests
from flask import Flask, request, jsonify
from google.cloud import secretmanager
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler

from . import config
from . import sheets_client
from . import notifier
from . import etsy_client
from . import match_logging as mlog
from . import pipeline_ingest as ping
from . import gemini_resolve as gres
from . import pipeline_classify
from . import normalize
from . import seen_items
from .utils import (
    extract_years,
    extract_decades,
    needed_years,
    build_year_queries,
    era_year_set,
    is_non_alerting_slogan,
    extract_lot_count,
    dedup_listings,
    select_hunt_ids,
)

# clip_matcher and image_proc import torch/clip/cv2 — loaded lazily so
# a missing .so or slow first-import doesn't kill the entire process at startup.
# They are imported inside startup() and the scan/analysis functions.

# ---------------------------------------------------------------------------
# Secrets — fetched at module load (gunicorn imports the module in the master
# process before forking, so these calls happen once)
# ---------------------------------------------------------------------------

def _get_secret(secret_id: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    name   = f"projects/{config.PROJECT_NUMBER}/secrets/{secret_id}/versions/latest"
    resp   = client.access_secret_version(request={"name": name})
    return resp.payload.data.decode("UTF-8")


print(">>> INIT: Fetching Slack secrets...", flush=True)
_slack_token     = _get_secret("EBAY_BOT_TOKEN")
_channel_id      = _get_secret("CHANNEL_ID_EBAY")
_signing_secret  = _get_secret("SIGNING_SECRET_ES")
print(">>> INIT: Slack secrets loaded.", flush=True)

# Slack Bolt app — serves the /crawl slash command via /slack/events.
app     = App(token=_slack_token, signing_secret=_signing_secret)
handler = SlackRequestHandler(app)

# External URL the slash handler uses to invoke /internal/crawl through the
# load balancer, so the heavy 500-lot run gets a fresh CPU-funded request (Cloud
# Run throttles CPU between requests). Set SERVICE_URL in the deploy env; falls
# back to localhost for local dev (CPU may throttle there). Mirrors buttonmatcher.
_SERVICE_URL     = os.environ.get("SERVICE_URL", "").rstrip("/")
_INTERNAL_SECRET = str(uuid.uuid4())   # per-startup token — never leaves this process
print(f">>> INIT: SERVICE_URL={'set (' + _SERVICE_URL + ')' if _SERVICE_URL else 'NOT SET — using localhost (CPU may throttle)'}",
      flush=True)

# ---------------------------------------------------------------------------
# Global state (populated by startup())
# ---------------------------------------------------------------------------

buy_rules:     dict = {}
vectors_loaded: bool = False

# match_logging.SheetLogger, built in startup() against the LOGGER_ID workbook.
# Fail-open: if it can't open the sheet, logging is silently disabled.
match_logger: mlog.SheetLogger | None = None

# Held for the duration of a daily scan / crawl so an overlapping trigger can't
# start a second concurrent run.
_scan_lock = threading.Lock()

# Guards the per-result seen read-modify-write in process_pipeline_lot (each lot
# is marked seen on confirmation, possibly from overlapping result threads).
_seen_lock = threading.Lock()

# Guards the per-result scan_log.jsonl append (a GCS read-modify-write). With
# several Gem workers feeding results concurrently, unguarded appends would clobber
# each other's lines; this serializes them within the single ebayscout instance.
_scanlog_lock = threading.Lock()

# --- Gemini → GCS pipeline state (Drive watcher → Gem → GCS → /pipeline/notify) -
# Shared secret the watcher presents on /pipeline/notify (watcher-direct path).
_PIPELINE_SHARED_SECRET = os.environ.get("PIPELINE_SHARED_SECRET", "")
# Dedup finished pipeline objects by "<name>#<generation>"; an overwrite (re-run
# of the same image) bumps generation and reprocesses. Resets on restart.
processed_pipeline_objects: set[str] = set()
# Idempotency guard for the notify→internal kick (Cloud Run may deliver twice).
pipeline_started_jobs: set[str] = set()
# job_id -> transient job dict for the notify→internal hop (rebuilt from the GCS
# object name + pending-context blob if empty after a cold start).
pending_jobs: dict[str, dict] = {}
# normalized slogan -> {years}; built once after CLIP init (see startup()).
slogan_years: dict[str, set] = {}


@contextlib.contextmanager
def _keep_cpu_hot():
    """
    Spin one lightweight thread so Cloud Run's CPU scheduler sees continuous
    activity and does not throttle the vCPU allocation.

    Why this is needed: background threads (CLIP hydration, manual image
    analysis) run after Flask has already returned an HTTP response.  Cloud
    Run considers the request done and is free to throttle the container's
    CPUs to near zero.  One spinning core signals "busy" while leaving the
    remaining CPU available to PyTorch.  The arithmetic loop uses < 2% of
    one core and produces no allocations or I/O.
    """
    _stop = threading.Event()

    def _spin():
        _x = 1.0
        while not _stop.is_set():
            for _ in range(10_000):
                _x = (_x * 1.0000001 + 0.0000001) % 1.0

    _t = threading.Thread(target=_spin, daemon=True)
    _t.start()
    try:
        yield
    finally:
        _stop.set()
        _t.join(timeout=1)


# ---------------------------------------------------------------------------
# CLIP wake-up helpers
# ---------------------------------------------------------------------------

def _ensure_clip_loaded() -> bool:
    """
    Synchronously load CLIP if it isn't loaded yet.  Returns True on success.

    Safe to call from any thread: clip_matcher.init() is lock-guarded and a
    no-op once initialized.  Wrapped in _keep_cpu_hot() so it survives Cloud
    Run's CPU throttling when called from a post-ack/post-response background
    thread.  This is the single load path shared by /run-scan and /test-clip.
    """
    global vectors_loaded
    if vectors_loaded:
        return True
    with _keep_cpu_hot():
        try:
            from . import clip_matcher as cm   # lazy: torch+clip imported here
            cm.init(config.BUCKET_NAME)
            vectors_loaded = True
            print(">>> WAKE: CLIP loaded.", flush=True)
            # Build the Gemini resolver's slogan→years multimap from the loaded
            # text DB (used by process_pipeline_lot's resolve step).
            try:
                global slogan_years
                _phrases, _years, _types = cm.text_db_arrays()
                slogan_years = gres.build_slogan_year_multimap(
                    _phrases, _years, normalize.normalize_key)
                print(f">>> WAKE: slogan_years built — {len(slogan_years)} keys.", flush=True)
            except Exception as exc:
                print(f"!!! WAKE: slogan_years build failed: {exc}", flush=True)
            return True
        except Exception as exc:
            print(f"!!! WAKE: CLIP init failed: {exc}", flush=True)
            traceback.print_exc()
            return False


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)


@flask_app.route("/run-scan", methods=["POST"])
def run_scan():
    """
    Triggered by Cloud Scheduler for the daily eBay scan.

    Runs the scan synchronously and returns 200 only when it finishes. This
    is deliberate: Cloud Run throttles CPU to ~0% outside request handling, so
    running inline keeps the full CPU allocated for the whole scan. A
    non-blocking lock makes a duplicate trigger a no-op (409).

    On a cold start the background hydration thread may have been CPU-throttled
    before it could finish. We load CLIP (and reload buy_rules if needed)
    synchronously here — the incoming request itself provides the CPU.

    One-shot params (manual curl only — Cloud Scheduler sends none of these):
      ?ignore_seen=1  process every fetched listing regardless of seen_items
                      (re-evaluate currently-visible inventory under new logic);
                      still checkpoints seen so it's resumable + forward-only after.
      ?dry_run=1      override config.DRY_RUN for this run only (post nothing,
                      write nothing) — pair with ignore_seen to preview volume
                      and tune NEEDED_MATCH_THRESHOLD before going live.
      ?year_crawl=1   source listings from year-augmented searches for needed
                      years (amount_needed > 0) instead of the general queries,
                      matching each result restricted to its search year. Reaches
                      deep inventory the general newest-100 windows miss.
      ?era_crawl=1    source listings from the Mellon + Citizens bank searches,
                      matching each restricted to its era's year range. Broader
                      than the year crawl (multi-year lots); run it after the
                      year crawl so seen-dedup skips what that already caught.
      ?limit=N        resumable chunk mode: process at most N unseen listings
                      this call (forward-only). A big backfill done as one
                      request outlives its CPU window and the tail runs under
                      throttle; chunking keeps each call fast. Re-issue the same
                      call (e.g. ?year_crawl=1&limit=150) until the JSON reports
                      remaining=0. Run live (omit dry_run) so the seen cursor
                      persists between chunks.
      ?hunt_ids=1     fetch the specific eBay item_ids in HUNT_IDS_BLOB directly
                      by ID and run them through the pipeline, recording each
                      listing's full current data (asking price, condition,
                      format) into the scan log / market DB. Runs additively
                      with ?year_crawl=1 (rebuild the market DB from a prior
                      run's IDs) or standalone (skips the general search). Pair
                      with ?limit=N for a big ID list; live (omit dry_run) to
                      persist records and advance the chunk cursor.
    """
    global buy_rules

    def _truthy(v: str | None) -> bool:
        return (v or "").strip().lower() in ("1", "true", "yes", "on")

    ignore_seen   = _truthy(request.args.get("ignore_seen"))
    year_crawl    = _truthy(request.args.get("year_crawl"))
    era_crawl     = _truthy(request.args.get("era_crawl"))
    hunt_ids      = _truthy(request.args.get("hunt_ids"))
    dry_run_param = True if _truthy(request.args.get("dry_run")) else None  # None → use config
    try:
        limit = max(0, int(request.args.get("limit", 0) or 0))
    except (TypeError, ValueError):
        limit = 0

    if not vectors_loaded:
        print(">>> SCAN: CLIP not ready — loading synchronously within request...", flush=True)
        if not _ensure_clip_loaded():
            return jsonify({"status": "clip init failed"}), 500

    if not buy_rules:
        print(">>> SCAN: buy_rules empty — reloading...", flush=True)
        try:
            sheets_json    = _get_secret("GOOGLE_SHEETS_JSON")
            spreadsheet_id = _get_secret("SPREADSHEET_ID")
            buy_rules      = sheets_client.load_buy_rules(sheets_json, spreadsheet_id)
        except Exception as exc:
            print(f"!!! SCAN: buy_rules reload failed (scan continues without them): {exc}",
                  flush=True)

    if not _scan_lock.acquire(blocking=False):
        print(">>> SCAN: Already running — ignoring duplicate trigger.", flush=True)
        return jsonify({"status": "already running"}), 409

    try:
        remaining = _run_daily_scan(ignore_seen=ignore_seen, dry_run=dry_run_param,
                                    year_crawl=year_crawl, era_crawl=era_crawl,
                                    limit=limit, hunt_ids=hunt_ids)
    finally:
        _scan_lock.release()

    return jsonify({
        "status":      "scan complete",
        "ignore_seen": ignore_seen,
        "year_crawl":  year_crawl,
        "era_crawl":   era_crawl,
        "hunt_ids":    hunt_ids,
        "dry_run":     config.DRY_RUN if dry_run_param is None else dry_run_param,
        "limit":       limit,
        "remaining":   remaining,   # chunk mode: unseen listings left (0 = done)
    }), 200


@flask_app.route("/health", methods=["GET"])
def health():
    # Always return 200 so Cloud Run health probes don't kill the container
    # during the 30-60s CLIP hydration window.
    if not vectors_loaded:
        return "OK - hydrating", 200
    return "OK - ready", 200


# ---------------------------------------------------------------------------
# Slack slash command: /crawl <N> (on-demand2 search)
# ---------------------------------------------------------------------------

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    """Single entry point for all Slack interactions (slash commands), delegated
    to the Bolt handler. Mirrors buttonmatcher/main.py:4721-4727."""
    if request.content_type and "application/json" in request.content_type:
        data = request.get_json(silent=True)
        if data and data.get("type") == "url_verification":
            return data.get("challenge"), 200
    return handler.handle(request)


@app.command("/crawl")
def handle_crawl_command(ack, body):
    """on-demand2: search the fixed Citizens/Mellon/Central-Counties button query
    over up to N lots and feed each lot's primary photo into the Gemini pipeline.

    Seen-aware: lots already run are skipped (except the very first on-demand2
    run, which may include seen lots to reach the cap), and a lot is marked seen
    only on confirmation — so nothing is lost or double-run.

    Usage: `/crawl <N>` (e.g. `/crawl 800`), N in 1..CRAWL_MAX_LOTS_CAP. Slack
    requires an ack within 3s, so we validate + ack immediately and kick the
    heavy run via /internal/crawl through the load balancer (CPU-funded request).
    """
    raw = (body.get("text") or "").strip()
    cap = config.CRAWL_MAX_LOTS_CAP
    try:
        n = int(raw)
    except ValueError:
        ack(f"Usage: `/crawl <N>` — N = how many lots to search (1–{cap}). "
            f"Example: `/crawl 800`.")
        return
    if n < 1 or n > cap:
        ack(f"`/crawl {raw}` is out of range — pick N between 1 and {cap}. "
            f"(This launches a paid eBay + CLIP run, so the cap guards a typo.)")
        return

    ack(f"🔎 Starting `/crawl {n}` — searching up to {n} lots; runs in the "
        f"background and posts results to the channel as each Gem read returns.")

    def _kick():
        base    = _SERVICE_URL if _SERVICE_URL else "http://localhost:8080"
        headers = {"X-Internal-Secret": _INTERNAL_SECRET}
        try:
            # Long read timeout: the run executes synchronously inside this call
            # so CPU stays allocated for its whole duration.  3500s leaves 100s
            # headroom against Cloud Run's 3600s max request timeout.
            requests.post(f"{base}/internal/crawl", params={"n": n},
                          headers=headers, timeout=3500)
        except Exception as exc:
            print(f"!!! CRAWL: internal kick failed: {exc}", flush=True)
            try:
                notifier.send_warning(_slack_token, _channel_id,
                                      f"/crawl {n} failed to start: {exc}")
            except Exception:
                pass

    threading.Thread(target=_kick, daemon=True).start()


def _is_localhost(remote_addr: str | None) -> bool:
    try:
        return ipaddress.ip_address(remote_addr or "").is_loopback
    except ValueError:
        return False


@flask_app.route("/internal/crawl", methods=["POST"])
def internal_crawl():
    """Run the on-demand2 N-lot search synchronously in this request so Cloud Run
    keeps CPU allocated for the whole run. N comes from ?n= (clamped to
    1..CRAWL_MAX_LOTS_CAP). Auth: per-startup X-Internal-Secret header, or a
    localhost caller (local dev). Mirrors buttonmatcher /internal/match."""
    provided     = request.headers.get("X-Internal-Secret", "")
    from_localhost = _is_localhost(request.remote_addr)
    if provided != _INTERNAL_SECRET and not from_localhost:
        return jsonify({"status": "forbidden"}), 403

    cap = config.CRAWL_MAX_LOTS_CAP
    try:
        n = max(1, min(cap, int(request.args.get("n", 0) or 0)))
    except ValueError:
        return jsonify({"status": "bad n"}), 400

    if not vectors_loaded and not _ensure_clip_loaded():
        return jsonify({"status": "clip init failed"}), 500

    global buy_rules
    if not buy_rules:
        try:
            buy_rules = sheets_client.load_buy_rules(
                _get_secret("GOOGLE_SHEETS_JSON"), _get_secret("SPREADSHEET_ID"))
        except Exception as exc:
            print(f"!!! CRAWL: buy_rules reload failed: {exc}", flush=True)

    if not _scan_lock.acquire(blocking=False):
        return jsonify({"status": "already running"}), 409
    try:
        processed = _run_crawl(n)
    finally:
        _scan_lock.release()
    return jsonify({"status": "crawl complete", "n": n, "processed": processed}), 200


# ---------------------------------------------------------------------------
# Gemini → GCS pipeline ingest (Drive watcher → Gem → GCS → here)
# ---------------------------------------------------------------------------

def _gcs_blob_text(name: str, bucket_name: str = config.BUCKET_NAME) -> str:
    from google.cloud import storage
    return storage.Client().bucket(bucket_name).blob(name).download_as_text()


def _gcs_blob_to_file(name: str, dest: str, bucket_name: str = config.BUCKET_NAME) -> None:
    from google.cloud import storage
    storage.Client().bucket(bucket_name).blob(name).download_to_filename(dest)


def _delete_pipeline_output(*names: str | None, bucket_name: str = config.BUCKET_NAME) -> None:
    """Delete the pipeline/output image + .response.json once a lot is fully
    processed, so the shared GCS bucket doesn't fill up. Best-effort per object;
    a failure here never affects the result that was already posted."""
    from google.cloud import storage
    try:
        bucket = storage.Client().bucket(bucket_name)
    except Exception as exc:
        print(f">>> PIPELINE: output cleanup skipped (GCS client): {exc}", flush=True)
        return
    for name in names:
        if not name:
            continue
        try:
            bucket.blob(name).delete()
        except Exception as exc:
            print(f">>> PIPELINE: output cleanup skip {name}: {exc}", flush=True)


def _pipeline_key_from_image(image_name: str | None) -> str | None:
    """Recover the correlation key from pipeline/output/<prefix><key>.png."""
    if not image_name:
        return None
    base = image_name.rsplit("/", 1)[-1]
    pref = config.PIPELINE_OBJECT_PREFIX
    if base.startswith(pref):
        return base[len(pref):].split(".")[0]
    return None


def _restrict_years_from_ctx(ctx: dict, _cm) -> set[int] | None:
    """Mirror the daily-scan title-year/decade restriction now that CLIP is loaded."""
    decades = {int(d) for d in (ctx.get("title_decades") or [])}
    if decades:
        return decades
    tyears = [int(y) for y in (ctx.get("title_years") or [])]
    ref_years = _cm.reference_years()
    if len(tyears) == 1 and tyears[0] in ref_years:
        return {tyears[0]}
    return None


@flask_app.route("/pipeline/notify", methods=["POST"])
def pipeline_notify():
    """Trigger entrypoint for the Gemini pipeline. Fast-acks 204, then kicks
    /internal/pipeline (CPU-hot) in a thread. Auth: X-Pipeline-Secret (watcher),
    internal secret, or localhost. Body: {"object": "...response.json"} or a
    Pub/Sub envelope. Ignores non-response objects and objects that belong to
    another service (no ebayscout prefix and no pending-context blob)."""
    provided       = request.headers.get("X-Pipeline-Secret", "")
    internal       = request.headers.get("X-Internal-Secret", "")
    from_localhost = _is_localhost(request.remote_addr)
    authed = (
        (_PIPELINE_SHARED_SECRET and provided == _PIPELINE_SHARED_SECRET)
        or internal == _INTERNAL_SECRET
        or from_localhost
    )
    if not authed:
        return jsonify({"status": "forbidden"}), 403

    body = request.get_json(silent=True) or {}
    name = body.get("object") or body.get("name")
    if not name:
        env = ping.parse_pubsub_envelope(request.get_data())
        if env:
            name = env.get("name")
    if not ping.is_response_json(name):
        return ("", 204)   # not a .response.json — ignore

    image_name = ping.image_name_for_response(name)
    key        = _pipeline_key_from_image(image_name)
    base       = (image_name or "").rsplit("/", 1)[-1]
    is_ours    = base.startswith(config.PIPELINE_OBJECT_PREFIX)
    if not is_ours and not (key and seen_items.load_pending_context(key)):
        return ("", 204)   # belongs to another service — ack-and-drop

    dedup_key = f"{name}#{body.get('generation', '')}"
    if dedup_key in processed_pipeline_objects:
        return ("", 204)
    processed_pipeline_objects.add(dedup_key)

    job_id = uuid.uuid4().hex
    pending_jobs[job_id] = {"response_name": name, "image_name": image_name, "key": key}

    def _kick():
        base_url = _SERVICE_URL if _SERVICE_URL else "http://localhost:8080"
        headers  = {"X-Internal-Secret": _INTERNAL_SECRET}
        for attempt in range(4):
            try:
                requests.post(f"{base_url}/internal/pipeline",
                              json={"job_id": job_id}, headers=headers, timeout=3500)
                return
            except Exception as exc:
                print(f"!!! PIPELINE: internal kick failed (try {attempt}): {exc}", flush=True)
                time.sleep(2 ** attempt)

    threading.Thread(target=_kick, daemon=True).start()
    return ("", 204)


@flask_app.route("/internal/pipeline", methods=["POST"])
def internal_pipeline():
    """Run process_pipeline_lot synchronously so Cloud Run keeps CPU allocated
    for detection + CLIP. Auth: internal secret / localhost."""
    provided = request.headers.get("X-Internal-Secret", "")
    if provided != _INTERNAL_SECRET and not _is_localhost(request.remote_addr):
        return jsonify({"status": "forbidden"}), 403
    body   = request.get_json(silent=True) or {}
    job_id = body.get("job_id")
    if not job_id:
        return jsonify({"status": "no job_id"}), 400
    if job_id in pipeline_started_jobs:
        return jsonify({"status": "already started"}), 200
    pipeline_started_jobs.add(job_id)

    if not vectors_loaded and not _ensure_clip_loaded():
        return jsonify({"status": "clip init failed"}), 500
    global buy_rules
    if not buy_rules:
        try:
            buy_rules = sheets_client.load_buy_rules(
                _get_secret("GOOGLE_SHEETS_JSON"), _get_secret("SPREADSHEET_ID"))
        except Exception as exc:
            print(f"!!! PIPELINE: buy_rules reload failed: {exc}", flush=True)

    try:
        with _keep_cpu_hot():
            process_pipeline_lot(job_id)
    except Exception as exc:
        print(f"!!! PIPELINE: process failed for {job_id}: {exc}", flush=True)
        traceback.print_exc()
    return jsonify({"status": "pipeline complete"}), 200


@flask_app.route("/internal/pipelinetest", methods=["GET"])
def internal_pipelinetest():
    """Run the full pipeline path against an existing GCS object, for live
    verification without the watcher. ?object=pipeline/output/<f>.png.response.json"""
    provided = request.headers.get("X-Internal-Secret", "")
    if provided != _INTERNAL_SECRET and not _is_localhost(request.remote_addr):
        return jsonify({"status": "forbidden"}), 403
    name = request.args.get("object", "")
    if not ping.is_response_json(name):
        return jsonify({"status": "not a .response.json object"}), 400
    if not vectors_loaded and not _ensure_clip_loaded():
        return jsonify({"status": "clip init failed"}), 500
    global buy_rules
    if not buy_rules:
        try:
            buy_rules = sheets_client.load_buy_rules(
                _get_secret("GOOGLE_SHEETS_JSON"), _get_secret("SPREADSHEET_ID"))
        except Exception:
            pass
    job_id     = uuid.uuid4().hex
    image_name = ping.image_name_for_response(name)
    pending_jobs[job_id] = {
        "response_name": name, "image_name": image_name,
        "key": _pipeline_key_from_image(image_name),
    }
    with _keep_cpu_hot():
        process_pipeline_lot(job_id)
    return jsonify({"status": "ok", "job_id": job_id}), 200


def _stage_crop_fullres(src_img, det_crop, ci, det_w, det_h, roi_used):
    """Re-cut one reference crop from the FULL-RES source image instead of the
    ~800px detection working copy, so auto-staged reference crops aren't
    downscaled (detection itself stays at 800px — see _prepare_detection_image).

    `ci` is the crop's circle_info entry (detection-space geometry); `det_w/det_h`
    are the detection image dims. Maps the crop's box back to `src_img` by the
    uniform downscale factor and re-crops. Fail-safe: returns the original
    `det_crop` whenever a clean mapping isn't available (ROI-retry frame, missing
    geometry, degenerate box), so this can never produce a worse crop than today.
    """
    try:
        if roi_used or not det_w or not det_h:
            return det_crop
        H, W = src_img.shape[:2]
        if H == det_h and W == det_w:
            return det_crop                      # no downscale happened
        sx, sy = W / det_w, H / det_h
        if ci.get("shape") == "rect":
            x1, y1, x2, y2 = ci["x1"], ci["y1"], ci["x2"], ci["y2"]
        else:
            cx, cy = ci.get("x"), ci.get("y")
            if cx is None or cy is None:
                return det_crop
            ch, cw = det_crop.shape[:2]          # preserve detect's framing, scaled
            x1, y1 = cx - cw / 2.0, cy - ch / 2.0
            x2, y2 = x1 + cw, y1 + ch
        X1, Y1 = max(0, int(round(x1 * sx))), max(0, int(round(y1 * sy)))
        X2, Y2 = min(W, int(round(x2 * sx))), min(H, int(round(y2 * sy)))
        if X2 - X1 < 8 or Y2 - Y1 < 8:
            return det_crop
        hi = src_img[Y1:Y2, X1:X2]
        return hi if hi.size else det_crop
    except Exception:
        return det_crop


def process_pipeline_lot(job_id: str) -> None:
    """Consume one Gemini-pipeline result: download image+JSON, run independent
    Hough detection + Gemini reconciliation, CLIP match, then confirm slogans
    against Gemini's reading. At scale this lot is a unit of work for BOTH
    /crawl, so its output is deal-focused:

      * posts to #ebay-checker ONLY when the lot is a deal — it has a needed
        button, or its matched value exceeds the asking price;
      * AUTO-STAGES the surest crops (real Hough detection + Gemini-confirmed +
        CLIP overall >= STAGE_CONF) into reference/_staging/ for /reference review
        (no Yes/No prompt);
      * writes scan_log.jsonl + match_log rows and marks the lot seen here, per
        confirmation — never buffered to a run's end.

    Unlike buttonmatcher's process_pipeline_grid, there is NO Inventory/Sort/
    Scout chooser and NO vectors.pt write — ebayscout only STAGES crop FILES."""
    import tempfile
    import cv2
    from . import clip_matcher as _cm, detect_pipeline as _dp, image_proc as _ip

    job = pending_jobs.get(job_id) or {}
    response_name = job.get("response_name")
    if not response_name:
        print(f"!!! PIPELINE: no job context for {job_id}", flush=True)
        return
    image_name = job.get("image_name") or ping.image_name_for_response(response_name)
    key        = job.get("key") or _pipeline_key_from_image(image_name)

    # 1) download image + Gemini JSON from GCS
    try:
        gemini = ping.parse_gemini_response(_gcs_blob_text(response_name))
    except Exception as exc:
        print(f"!!! PIPELINE: response.json read failed for {response_name}: {exc}", flush=True)
        return
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        _gcs_blob_to_file(image_name, tmp.name)
        image_bgr = cv2.imread(tmp.name)
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
    if image_bgr is None:
        print(f"!!! PIPELINE: could not read image {image_name}", flush=True)
        return
    h, w = image_bgr.shape[:2]
    if max(h, w) > 2200:
        s = 2200 / max(h, w)
        image_bgr = cv2.resize(image_bgr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)

    # 2) recover the originating-listing context (survives cold start)
    ctx     = (seen_items.load_pending_context(key) if key else None) or {}
    item_id = ctx.get("item_id", key or "?")
    asking  = ctx.get("asking")
    title   = ctx.get("title", "(context lost)")
    url     = ctx.get("url", "")
    restrict_years = _restrict_years_from_ctx(ctx, _cm)

    gem_slogans = gemini.get("detected_slogans") or []
    gem_count   = gemini.get("total_button_count") or 0
    flagged     = [s for s in (gemini.get("flagged_problem_slogans") or []) if isinstance(s, dict)]
    flagged_indices = {s.get("index") for s in flagged if s.get("index") is not None}

    # Gemini-works vs Gemini-fails. A blank/empty response.json (no slogans AND no
    # count — parse_gemini_response fails open to EMPTY_ANALYSIS on parse errors)
    # means the Gem gave us nothing, so we fall back to Hough-only/CLIP-only. In
    # that fallback ONLY, yellow crops are surfaced for human review; when Gemini
    # works, classification is autoconfirm-or-ignore (no human prompt).
    gemini_ok = bool(gem_slogans) or gem_count > 0

    # 3) Hough detection, INDEPENDENT of the JSON (radius from full-button count)
    full_count = (gem_count - len(flagged)) if gem_count > 0 else 0
    expected   = full_count if full_count > 0 else None
    if expected is None:
        _n, _ = _dp.count_circles_unguided(image_bgr)
        expected = _n if (_n and _n > 0) else None
    if expected is None:
        print(f">>> PIPELINE: no buttons found in {image_name} — skipped.", flush=True)
        seen_items.delete_pending_context(key) if key else None
        _delete_pipeline_output(response_name, image_name)
        pending_jobs.pop(job_id, None)
        return
    _diag = {}
    crops, debug_img, det_rows, det_cols, circle_info = _dp.detect_buttons(
        image_bgr, rows=None, cols=None, expected=expected, debug=True,
        diag_out=_diag, truncate_to_expected=False,
    )

    # 4) reconcile missed buttons from Gemini x/y, in detection's coord space
    _ph, _pw = debug_img.shape[:2]
    _det_img = (image_bgr if (image_bgr.shape[0], image_bgr.shape[1]) == (_ph, _pw)
                else cv2.resize(image_bgr, (_pw, _ph), interpolation=cv2.INTER_AREA))
    if _diag.get("detector_used") == "grid" and gem_slogans:
        led_crops, led_info = _dp.gemini_led_crops(gem_slogans, _det_img)
        if led_crops:
            crops, circle_info = led_crops, led_info
    rec_crops, rec_info, crop_to_slogan, _rt = _dp.reconcile_with_gemini(
        circle_info, gem_slogans, _det_img)
    if rec_crops:
        crops = list(crops) + list(rec_crops)
        circle_info = list(circle_info) + list(rec_info)
    if not crops:
        print(f">>> PIPELINE: no crops for {image_name} after reconcile — skipped.", flush=True)
        seen_items.delete_pending_context(key) if key else None
        _delete_pipeline_output(response_name, image_name)
        pending_jobs.pop(job_id, None)
        return

    # 5) CLIP matching, INDEPENDENT of the JSON
    pil_crops   = _ip._bgr_to_pil(crops)
    diagnostics = _cm.match_crops_with_diagnostics(pil_crops, restrict_years=restrict_years)
    crop_candidates = {i: d["candidates"] for i, d in enumerate(diagnostics)}

    # 6) confirm slogans against Gemini's reading (two-pass)
    resolution = gres.resolve_with_gemini_slogans(
        crop_candidates, crop_to_slogan, slogan_years, flagged_indices,
        normalize_fn=normalize.normalize_key,
    )

    # 7) classify crops per the autoconfirmation decision tree (pure module).
    #    Gemini works:  green/auto → confirm; else Gemini slogan in top-10 AND
    #                   conf≥0.70 AND not flagged (res.auto) → confirm; else ignore.
    #    Gemini fails:  green/auto → confirm; else ignore. (The yellow tier is
    #                   not surfaced at scale — deals-only posting; see step 9.)
    auto_confirmed, _yellow = pipeline_classify.classify_crops(
        diagnostics, resolution, gemini_ok, job_id)

    # needed-button hits among the auto-confirmed crops
    needed_hits: dict[tuple, dict] = {}
    for b in auto_confirmed:
        overall = b["overall"]
        enriched = _check_needed_hit({"year": b["year"], "slogan": b["slogan"],
                                      "overall": overall or 0.0}, buy_rules)
        if enriched is not None:
            k = (b["year"], b["slogan"])
            if k not in needed_hits or (overall or 0) > needed_hits[k].get("overall", 0):
                needed_hits[k] = enriched

    # total matched lot value (every confirmed button has a sheet price, needed
    # or not) + undervalued-deal flag (pure helper)
    lot_value, undervalued, margin = pipeline_classify.lot_value_and_deal(
        auto_confirmed, _price_of, asking)

    # 8) AUTO-STAGE the surest crops (real Hough detection + Gemini-confirmed +
    #    overall >= STAGE_CONF) straight into reference/_staging for buttonmatcher's
    #    /reference review — no Yes/No prompt. Reuses stage_pipeline_crop + promote
    #    so there is one promotion path; ebayscout never writes vectors.pt.
    stageable = pipeline_classify.staging_candidates(
        auto_confirmed, circle_info, resolution, config.STAGE_CONF)
    manifest_crops: list[dict] = []
    _roi_used = bool(_diag.get("roi_retry"))
    for b in stageable:
        try:
            # Stage from the full-res source, not the ~800px detection copy.
            hi = _stage_crop_fullres(image_bgr, crops[b["crop_idx"]],
                                     circle_info[b["crop_idx"]], _pw, _ph, _roi_used)
            ok, buf = cv2.imencode(".jpg", hi, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            if not ok:
                continue
            gcs_name = seen_items.stage_pipeline_crop(job_id, b["n"], buf.tobytes())
            if not gcs_name:
                continue
            manifest_crops.append({
                "gcs_name": gcs_name, "year": b["year"], "slogan": b["slogan"],
                "entry_id": _cm.entry_id_for(b["year"], b["slogan"]),
            })
        except Exception as exc:
            print(f"!!! PIPELINE: crop stage failed (crop {b['n']}): {exc}", flush=True)
    if manifest_crops:
        staged = seen_items.promote_crops_to_reference_staging(
            job_id, {"job_id": job_id, "item_id": item_id, "crops": manifest_crops})
        print(f">>> PIPELINE: auto-staged {staged}/{len(manifest_crops)} crop(s) for "
              f"{item_id} -> reference/_staging (job={job_id}).", flush=True)

    # 9) post to Slack ONLY when this lot is a deal — a needed button, or matched
    #    lot value over the asking price. No generic per-lot card; no Yes/No;
    #    yellow-review crops are not surfaced at scale.
    listing = {"item_id": item_id, "title": title, "url": url, "listing_url": url,
               "current_price": asking, "seller": ctx.get("seller", ""),
               "gallery_url": ctx.get("gallery_url")}
    if needed_hits:
        needed = list(needed_hits.values())
        try:
            # Report the value of the WHOLE lot (all confirmed buttons), not just
            # the needed ones — `lot_value` above already sums every confirmed
            # button's sheet price.
            notifier.send_needed_alert(
                slack_token=_slack_token, channel=_channel_id, listing=listing,
                needed_buttons=needed, asking_price=asking or 0.0, lot_value=lot_value)
        except Exception as exc:
            print(f"!!! PIPELINE: needed alert failed for {item_id}: {exc}", flush=True)
    if undervalued:
        try:
            matches = []
            for b in auto_confirmed:
                _ps, _, _, _amt = sheets_client.get_buy_decision(
                    b["year"], b["slogan"], buy_rules)
                matches.append({"year": b["year"], "slogan": b["slogan"],
                                "overall": b["overall"], "max_price_single": _ps,
                                "amount_needed": _amt})
            notifier.send_undervalued_alert(
                slack_token=_slack_token, channel=_channel_id, listing=listing,
                matches=matches, lot_value=lot_value, asking_price=asking or 0.0,
                margin=margin, unmatched_count=max(0, len(crops) - len(auto_confirmed)))
        except Exception as exc:
            print(f"!!! PIPELINE: undervalued alert failed for {item_id}: {exc}", flush=True)

    # 10) write the research logs NOW — per confirmation, never buffered. Reaching
    #     here means we got a real reading (Gem-empty lots returned earlier).
    pipeline_command = f'{ctx.get("command", "/crawl")}-pipeline'
    try:
        record = _scan_log_record(
            listing=listing, photos_processed=1,
            best_score=max((b["overall"] or 0.0 for b in auto_confirmed), default=0.0),
            top_matches=[{"year": b["year"], "slogan": b["slogan"],
                          "overall": b["overall"] or 0.0} for b in auto_confirmed],
            needed_hit=bool(needed_hits), alerted=bool(needed_hits or undervalued),
            best_needed=(max(needed_hits.values(), key=lambda m: m.get("overall", 0))
                         if needed_hits else None),
        )
        with _scanlog_lock:                  # serialize the GCS read-modify-write
            seen_items.append_scan_log([record])
    except Exception as exc:
        print(f"!!! PIPELINE: scan_log append failed for {item_id}: {exc}", flush=True)

    # Per-crop match records carrying the Gemini reconcile fields, so the
    # match_log is schema-compatible with buttonmatcher's pipeline rows.
    try:
        if match_logger is not None and diagnostics:
            _det = mlog.build_detection_diag(
                h=_diag.get("h") or image_bgr.shape[0],
                w=_diag.get("w") or image_bgr.shape[1],
                bg_brightness=_diag.get("bg_brightness") or 0.0,
                bg_saturation=_diag.get("bg_saturation"),
                bg_is_white=bool(_diag.get("bg_is_white")),
                mask_path=_diag.get("mask_path"),
                hough_pass1_count=_diag.get("hough_pass1_count") or 0,
                hough_retry_count=_diag.get("hough_retry_count"),
                final_count_user=len(crops), final_count_noinput=None,
                user_count=None, detector_used=_diag.get("detector_used"),
                n_crops=len(crops),
                raw_hough=_diag.get("raw_hough"),
                circles_rejected=_diag.get("circles_rejected"),
                radius_min=_diag.get("radius_min"), radius_max=_diag.get("radius_max"),
                radius_mean=_diag.get("radius_mean"), radius_std=_diag.get("radius_std"),
                mask_components=_diag.get("mask_components"),
                border_removed=_diag.get("border_removed"),
                fill_removed=_diag.get("fill_removed"),
                overlap_removed=_diag.get("overlap_removed"),
                edge_density=_diag.get("edge_density"),
                brightness_std=_diag.get("brightness_std"),
                count_source=("gemini_led"
                              if (_diag.get("detector_used") == "grid" and gem_slogans)
                              else "gemini" if full_count > 0 else "unguided"),
                gemini_button_count=gem_count,
                n_recovered=len(rec_crops),
                reconcile_misses=(_rt or {}).get("misses"),
            )
            _records = [
                mlog.build_match_record(
                    service="ebayscout", command=pipeline_command, mode="pipeline",
                    job_id=job_id, thread_ts=None, channel_id=_channel_id, user_id=None,
                    crop_num=i + 1, check_id=f"{job_id}:{item_id}:{i + 1}",
                    detection=_det, bank=None,
                    restricted_top=d.get("restricted_top", []),
                    shadow_top=d.get("shadow_top", []),
                    shadow_enabled=bool(d.get("shadow_enabled")),
                )
                for i, d in enumerate(diagnostics)
            ]
            match_logger.log_image_crops(job_id, _records)
    except Exception as exc:
        print(f"!!! PIPELINE: match_log failed for {item_id}: {exc}", flush=True)

    _log_pipeline_count(job_id, item_id, gem_count, command=pipeline_command)

    # mark the lot seen on confirmation (never at feed) so it's never re-run; a
    # Gem that never answered leaves the lot un-seen (the watcher retries it), so
    # we only reach here for a real reading.
    if item_id and item_id != "?":
        _mark_item_seen_now(item_id)

    if key:
        seen_items.delete_pending_context(key)
    # Free the bucket: the image + .response.json are fully consumed now.
    _delete_pipeline_output(response_name, image_name)
    pending_jobs.pop(job_id, None)


def _mark_item_seen_now(item_id: str) -> None:
    """Mark one item seen the instant its pipeline result confirms: load -> add ->
    save under a lock so concurrent result threads can't clobber each other.
    seen_items.json on GCS is the source of truth; result volume is low (~one Gem
    at a time), so a full read-modify-write per result is cheap."""
    try:
        with _seen_lock:
            seen = seen_items.load_seen()
            seen_items.mark_seen(item_id, seen)
            seen_items.save_seen(seen)
    except Exception as exc:
        print(f"!!! PIPELINE: mark_seen({item_id}) failed: {exc}", flush=True)


def _log_pipeline_count(job_id: str, item_id: str, total_button_count: int,
                        command: str = "/crawl-pipeline") -> None:
    """Log the Gem's button-count estimate as its own confirm_log row
    (source='gemini_count'), preserving the old /crawl10 count telemetry."""
    if match_logger is None:
        return
    check_id = f"gemini_count:{item_id}"
    try:
        rec = mlog.build_confirm_record(
            service="ebayscout", command=command, job_id=job_id,
            thread_ts=None, crop_num=None, check_id=check_id, user_id=None,
            chosen_year=None, chosen_phrase=str(total_button_count),
            chosen_type=None, source="gemini_count",
            rank_restricted=None, rank_shadow=None, shadow_leaderboard_size=None,
        )
        match_logger.log_confirmation(check_id, rec)
    except Exception as exc:
        print(f"!!! PIPELINE: gemini_count log failed for {item_id}: {exc}", flush=True)


@flask_app.route("/test-clip", methods=["GET"])
def test_clip():
    """
    Debug endpoint: download one image, run detect+match, return raw per-crop
    scores (threshold=0.0 so every crop reports its actual score).

    Usage — pass either an eBay item ID or a direct image URL:
      curl "https://<service>/test-clip?item_id=v1|318369928679|0"
      curl "https://<service>/test-clip?url=<image_url>"
    """
    item_id   = request.args.get("item_id")
    image_url = request.args.get("url")
    if not item_id and not image_url:
        return jsonify({"error": "pass ?item_id=<ebay_id> or ?url=<image_url>"}), 400

    # Ensure CLIP is loaded
    if not _ensure_clip_loaded():
        return jsonify({"error": "CLIP init failed"}), 500

    try:
        import requests as req
        from . import image_proc as _ip
        from . import clip_matcher as _cm
        from . import ebay_client

        # Resolve eBay item ID → first picture URL
        if item_id:
            ebay_app_id  = _get_secret("EBAY_APP_ID")
            ebay_cert_id = _get_secret("EBAY_CERT_ID")
            urls = ebay_client.get_item_pictures(ebay_app_id, ebay_cert_id, item_id)
            if not urls:
                return jsonify({"error": "no images found for that item_id"}), 404
            image_url = urls[0]

        resp = req.get(image_url, timeout=20)
        resp.raise_for_status()
        image_bytes = resp.content

        with _keep_cpu_hot():
            crops = _ip.detect_and_crop(image_bytes)
            if not crops:
                return jsonify({"image_url": image_url, "crops": 0, "message": "no crops detected"})

            results = []
            for i, crop in enumerate(crops):
                match = _cm.match_crop(crop, threshold=0.0)
                results.append({"crop": i, "match": match})

        best = max((r["match"]["overall"] for r in results if r["match"]), default=0.0)
        return jsonify({
            "image_url": image_url,
            "crops": len(crops),
            "best_overall": round(best, 4),
            "details": results,
        })
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc), "type": type(exc).__name__}), 500


# ---------------------------------------------------------------------------
# eBay Marketplace Account Deletion endpoint (required for production API)
# ---------------------------------------------------------------------------
#
# eBay requires all Developers Program apps to either subscribe to account-
# deletion notifications or opt out.  This endpoint handles both legs of the
# protocol:
#
#   GET  /ebay/account-deletion?challenge_code=<code>
#        → eBay sends this immediately after you save the endpoint URL in the
#          developer portal to verify you own the URL.
#        → We respond with SHA-256(challengeCode + verificationToken + endpoint)
#          as {"challengeResponse": "<hex>"}.
#
#   POST /ebay/account-deletion
#        → eBay sends this whenever an eBay user requests data deletion.
#        → ebayscout does NOT store eBay user personal data (seen_items.json
#          contains only public listing IDs, not user identifiers), so no
#          deletion action is needed — we just acknowledge receipt.
#
# Setup:
#   1. Store a 32-80 char token (alphanumeric + _ -) in Secret Manager:
#        printf 'YOUR_TOKEN_HERE' | gcloud secrets create \
#          EBAY_DELETION_VERIFICATION_TOKEN --data-file=- \
#          --project=project-60d488c5-9c8e-4acc-aac
#   2. In developer.ebay.com → Application Keys → Notifications:
#        Endpoint:           https://ebay-scout-404960106109.us-east1.run.app/ebay/account-deletion
#        Verification token: <same token you stored above>

_ebay_deletion_token: str | None = None   # lazily fetched on first GET


@flask_app.route("/ebay/account-deletion", methods=["GET", "POST"])
def ebay_account_deletion():
    global _ebay_deletion_token

    # ------------------------------------------------------------------ GET
    # eBay challenge handshake — called once when you save the endpoint URL
    # in the developer portal.
    if request.method == "GET":
        import hashlib

        challenge_code = request.args.get("challenge_code", "")
        if not challenge_code:
            return jsonify({"error": "missing challenge_code"}), 400

        # Fetch and cache the verification token
        if _ebay_deletion_token is None:
            try:
                _ebay_deletion_token = _get_secret("EBAY_DELETION_VERIFICATION_TOKEN")
            except Exception as exc:
                print(f"!!! EBAY DELETION: Failed to fetch verification token: {exc}",
                      flush=True)
                return jsonify({"error": "server configuration error"}), 500

        endpoint = config.EBAY_DELETION_ENDPOINT

        # Hash order mandated by eBay: challengeCode + verificationToken + endpoint
        m = hashlib.sha256()
        m.update(challenge_code.encode("utf-8"))
        m.update(_ebay_deletion_token.encode("utf-8"))
        m.update(endpoint.encode("utf-8"))
        challenge_response = m.hexdigest()

        print(f">>> EBAY DELETION: Challenge OK — code={challenge_code[:8]}...",
              flush=True)
        return jsonify({"challengeResponse": challenge_response})

    # ------------------------------------------------------------------ POST
    # Account deletion notification. ebayscout stores no eBay user personal
    # data (seen_items.json holds only public listing IDs), so there is
    # nothing to delete — just acknowledge with 200. We intentionally do not
    # log these: eBay sends a high, constant volume and the lines drown out
    # the scan logs.
    return "", 200


# ---------------------------------------------------------------------------
# Daily scan (called from /run-scan endpoint)
# ---------------------------------------------------------------------------

def _scan_log_record(
    listing:          dict,
    photos_processed: int,
    best_score:       float,
    top_matches:      list[dict],
    needed_hit:       bool,
    alerted:          bool,
    best_needed:      dict | None = None,
) -> dict:
    """
    Build one JSONL scan-log record for a processed listing.

    Beyond the alert fields, this captures market-analysis groundwork (see
    tools/market_report.py): the per-crop YEAR composition of the lot
    (`year_counts`), how many buttons we detected (`crops_scored`) and the
    stated lot size (`title_count`), plus the eBay buying format / condition /
    bid count. Together with `asking` these let a report estimate cost/button
    per YEAR from single-year-lot comps — the metric for pricing listings.
    `top_matches` arrives as the full per-crop best-match list (one per crop).
    """
    title = listing.get("title", "")
    # Year composition of the lot, from each crop's best match.
    year_counts: dict[str, int] = dict(Counter(
        str(m["year"]) for m in top_matches if m.get("year") is not None
    ))
    return {
        "ts":            datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "item_id":       listing.get("item_id", ""),
        "title":         title,
        "listing_url":   listing.get("listing_url", ""),
        "seller":        listing.get("seller", ""),
        "asking":        listing.get("current_price", 0.0),
        "currency":      listing.get("currency", "USD"),
        "buying_options": listing.get("buying_options", []),
        "condition":     listing.get("condition", ""),
        "bid_count":     listing.get("bid_count"),
        "photos_scored": photos_processed,
        "crops_scored":  len(top_matches),          # detected buttons (proxy)
        "title_count":   extract_lot_count(title),  # stated lot size, if any
        "title_years":   sorted(extract_years(title)),
        "year_counts":   year_counts,               # year -> # crops matched
        "best_score":    round(best_score, 4),
        "top_matches":   [
            {"year": m["year"], "slogan": m["slogan"], "overall": round(m["overall"], 4)}
            for m in sorted(top_matches, key=lambda x: x["overall"], reverse=True)[:5]
        ],
        "best_needed":   (
            {"year": best_needed["year"], "slogan": best_needed["slogan"],
             "overall": round(best_needed["overall"], 4)}
            if best_needed else None
        ),
        "needed_hit":    needed_hit,
        "alerted":       alerted,
    }


def _run_era_queries(ebay_client, ebay_app_id, ebay_cert_id, era_queries: list) -> list:
    """
    Run era-named (query, era) pairs, splitting PSU-prefixed queries into a
    Sports-Mem category-restricted call (same noise guard as the general PSU
    queries). Returns the combined, search_era-tagged listings.
    """
    non_psu = [(q, e) for (q, e) in era_queries if not q.startswith("PSU ")]
    psu     = [(q, e) for (q, e) in era_queries if q.startswith("PSU ")]
    out: list = []
    if non_psu:
        out.extend(ebay_client.find_era_augmented_listings(
            client_id=ebay_app_id, client_secret=ebay_cert_id,
            era_queries=non_psu, excluded_sellers=config.EXCLUDED_SELLERS,
            max_results=config.EBAY_MAX_RESULTS,
        ))
    if psu:
        out.extend(ebay_client.find_era_augmented_listings(
            client_id=ebay_app_id, client_secret=ebay_cert_id,
            era_queries=psu, excluded_sellers=config.EXCLUDED_SELLERS,
            max_results=config.EBAY_MAX_RESULTS,
            category_ids=config.SPORTS_MEMO_CATEGORY_ID,
        ))
    return out


def _log_confirmation(job_id: str, check_id: str, crop_num: int,
                      top: dict, diagc: dict, command: str) -> None:
    """Write one confirm_log row the instant a crop is auto/green confirmed.

    rank_shadow (the confirmed year's rank in the unrestricted leaderboard) is the
    headline automation metric: rank 1 means no restrictions were needed.
    """
    if match_logger is None:
        return
    shadow_full = diagc.get("shadow_full") or []
    try:
        rec = mlog.build_confirm_record(
            service="ebayscout", command=command, job_id=job_id, thread_ts=None,
            crop_num=crop_num, check_id=check_id, user_id=None,
            chosen_year=top["year"], chosen_phrase=top["slogan"], chosen_type="Football",
            source=("auto_resolve" if top["overall"] >= config.AUTO_RESOLVE_THRESHOLD else "green"),
            rank_restricted=mlog.rank_of(top["year"], diagc.get("restricted_top") or []),
            rank_shadow=mlog.rank_of(top["year"], shadow_full),
            shadow_leaderboard_size=len(shadow_full),
            restricted_top=diagc.get("restricted_top") or [],
            shadow_top=diagc.get("shadow_top") or [],
            rank_image_only=None,
        )
        match_logger.log_confirmation(check_id, rec)
    except Exception as exc:
        print(f"!!! LOG: confirm record failed for {check_id}: {exc}", flush=True)


def _check_needed_hit(top: dict, buy_rules: dict) -> dict | None:
    """If `top` (a confirmed year/slogan match) satisfies a standing need
    (amount_needed > 0, and not a placeholder/non-alerting slogan), return an
    enriched copy with max_price_single + amount_needed; otherwise None.
    """
    price_single, _, _, amount_needed = sheets_client.get_buy_decision(
        top["year"], top["slogan"], buy_rules
    )
    if amount_needed <= 0:
        return None
    if is_non_alerting_slogan(top["slogan"], config.NON_ALERTING_SLOGAN_PATTERNS):
        return None
    enriched = dict(top)
    enriched["max_price_single"] = price_single
    enriched["amount_needed"]    = amount_needed
    return enriched


def _price_of(year, slogan) -> float:
    """Max single-sale price for one button from the buy sheet (0.0 if absent).
    EVERY button in the sheet has a price, needed or not — so this values the
    WHOLE lot, not just the needed buttons."""
    price_single, _, _, _ = sheets_client.get_buy_decision(year, slogan, buy_rules)
    return sheets_client.parse_price(price_single)


def _evaluate_listing(
    listing: dict,
    picture_urls: list[str],
    restrict_years: set[int] | None,
    *,
    command: str,
    job_id: str,
    title_years: set[int],
    title_count: int | None,
    log_enabled: bool = True,
    return_first_image: bool = False,
) -> dict:
    """Detect + match a listing's crops, apply the green/auto confirmation gate,
    evaluate needed buttons, and log per-image match rows + per-confirmation rows
    PER EVENT (flushed as each image is processed, never buffered to the end).

    A crop counts as an identified button only when its top match is auto-confirmed
    (overall >= AUTO_RESOLVE_THRESHOLD) or in the green (overall >= GREEN_THRESHOLD,
    or its #1 leads #2 by >= GREEN_GAP). Among confirmed buttons, amount_needed > 0
    (with placeholder slogans suppressed) marks a needed lot.

    When return_first_image is True, the result also includes
    "first_image_bytes" — the raw bytes of photo 0 (or None if it couldn't be
    downloaded) — for callers that want to run additional analysis on the
    primary lot photo (e.g. /crawl10's Gemini triage).

    Returns a summary dict; the caller sends Slack alerts and marks the item seen.
    """
    from . import image_proc as _ip
    from . import clip_matcher as _cm

    item_id = listing["item_id"]
    mode    = "crawl500" if command in ("/crawl", "/crawl500") else "scan"
    bank    = listing.get("search_era") or ""

    per_photo_cap  = max(config.MAX_CROPS_PER_PHOTO, title_count or 0)
    listing_budget = max(config.MAX_CROPS_PER_LISTING, title_count or 0)

    best_score_seen  = 0.0
    photos_processed = 0
    confirmed:   dict[tuple, dict] = {}   # (year, slogan) -> best confirmed top match
    yellow:      dict[tuple, dict] = {}   # (year, slogan) -> best yellow candidate for review
    needed_hits: dict[tuple, dict] = {}   # (year, slogan) -> enriched needed match
    log_top_matches: list[dict] = []
    best_needed: dict | None = None
    first_image_bytes: bytes | None = None
    crop_counter = 0

    for photo_idx, photo_url in enumerate(picture_urls[: config.MAX_PHOTOS_PER_LISTING]):
        if photo_idx == 1:
            # Two-stage gating: only pull photos 2+ when photo 1 looked promising.
            promising = bool(title_years) or best_score_seen >= config.REJECTION_THRESHOLD
            if not promising:
                break
        if listing_budget <= 0:
            break

        try:
            image_bytes     = _ip.download_image(photo_url)
            if return_first_image and photo_idx == 0:
                first_image_bytes = image_bytes
            crops, det_diag = _ip.detect_and_crop(
                image_bytes, max_crops=per_photo_cap, return_diag=True)
        except Exception as exc:
            print(f"!!! SCAN: Photo processing failed: {exc}", flush=True)
            continue

        if crops:
            crops = crops[:listing_budget]
            listing_budget -= len(crops)
        if not crops:
            continue
        photos_processed += 1

        try:
            diagnostics = _cm.match_crops_with_diagnostics(crops, restrict_years=restrict_years)
        except Exception as exc:
            print(f"!!! SCAN: match_crops_with_diagnostics failed: {exc}", flush=True)
            continue

        # Package the Phase 1 multi-pass fields from image_proc into noinput_diag
        # so they reach the ni_* columns in match_log.  Only populated in scan mode
        # (count mode leaves them None, producing blank cells — correct behaviour).
        _ni = {
            "conservative": det_diag.get("ni_conservative"),
            "standard":     det_diag.get("ni_standard"),
            "aggressive":   det_diag.get("ni_loose"),       # logged as "aggressive" to match Phase 1 schema
            "selected":     det_diag.get("final_count_user") or len(crops),
            "confidence":   det_diag.get("ni_confidence"),
            "layout_conf":  None,    # not computed in image_proc (scan mode skips layout)
            "outliers":     None,
            "pass_winner":  det_diag.get("ni_pass_winner"),
            "contour_count": None,   # Phase 2/3 not yet in image_proc
            "merged_count":  None,
            "source":        "hough_only",
            "variant":       "hsv",
        }
        _noinput_diag = _ni if _ni.get("conservative") is not None else None

        detection = mlog.build_detection_diag(
            h=det_diag.get("h") or 0, w=det_diag.get("w") or 0,
            bg_brightness=det_diag.get("bg_brightness") or 0.0,
            bg_saturation=det_diag.get("bg_saturation"),
            bg_is_white=bool(det_diag.get("bg_is_white")),
            mask_path=det_diag.get("mask_path"),
            hough_pass1_count=det_diag.get("hough_pass1_count") or 0,
            hough_retry_count=None,
            final_count_user=det_diag.get("final_count_user") or len(crops),
            final_count_noinput=None,
            user_count=None,
            detector_used=det_diag.get("detector_used"),
            n_crops=len(crops),
            raw_hough=det_diag.get("raw_hough"),
            circles_rejected=det_diag.get("circles_rejected"),
            radius_min=det_diag.get("radius_min"), radius_max=det_diag.get("radius_max"),
            radius_mean=det_diag.get("radius_mean"), radius_std=det_diag.get("radius_std"),
            mask_components=det_diag.get("mask_components"),
            # Priority 5 (per-stage breakdown) + Priority 4 (whole-image quality)
            border_removed=det_diag.get("border_removed"),
            fill_removed=det_diag.get("fill_removed"),
            overlap_removed=det_diag.get("overlap_removed"),
            edge_density=det_diag.get("edge_density"),
            brightness_std=det_diag.get("brightness_std"),
            noinput_diag=_noinput_diag,
        )

        image_records: list[dict] = []
        for diagc in diagnostics:
            crop_counter += 1
            check_id   = f"{job_id}:{item_id}:{crop_counter}"
            candidates = diagc["candidates"]
            gap        = diagc["gap"]

            # One match_log row per crop, regardless of confirmation outcome.
            image_records.append(mlog.build_match_record(
                service="ebayscout", command=command, mode=mode, job_id=job_id,
                thread_ts=None, channel_id=_channel_id, user_id=None,
                crop_num=crop_counter, check_id=check_id, detection=detection,
                bank=bank, restricted_top=diagc["restricted_top"],
                shadow_top=diagc["shadow_top"], shadow_enabled=diagc["shadow_enabled"],
            ))

            if not candidates:
                continue
            top = candidates[0]
            best_score_seen = max(best_score_seen, top["overall"])
            log_top_matches.append(top)

            # --- Incorrect-match guard -------------------------------------------
            # The data showed one auto-resolve with rank_shadow=2 and NaN gap
            # (only one restricted candidate, no runner-up).  A missing gap means
            # the year restriction artificially eliminated all alternatives; we
            # require a minimum gap even for high absolute scores to prevent a lone
            # uncontested candidate from slipping through at 0.85 on image signal
            # alone.  Threshold calibrated so the three data rows with gap < 0.05
            # (the highest-risk set) would have been held for human review.
            MIN_AUTO_GAP = 0.05
            # A MISSING gap (None) or NaN is the lone-uncontested-candidate case
            # the comment above describes — the year restriction left no runner-up.
            # `gap != gap` is True only for NaN; treat both as a bad gap so a lone
            # 0.85 can no longer auto-confirm (the previous `gap is not None`
            # condition skipped the guard exactly when gap was missing).
            _gap_bad = gap is None or gap != gap or gap < MIN_AUTO_GAP
            if _gap_bad and top["overall"] < config.AUTO_RESOLVE_THRESHOLD + 0.05:
                # Treat as yellow even if score technically qualifies as green
                _cm_confirmed = False
            else:
                _cm_confirmed = _cm.is_confirmed(top["overall"], gap)

            # GREEN/AUTO gate — only a confirmed top match counts as a button.
            if not _cm_confirmed:
                # Any non-confirmed candidate at/above RED goes to human review —
                # including a guard-demoted >= GREEN one, which the old
                # `_red <= overall < _green` window silently dropped.
                _red = getattr(config, "RED_THRESHOLD", 0.65)
                if top["overall"] >= _red:
                    key = (top["year"], top["slogan"])
                    entry = dict(top)
                    entry.update({"check_id": check_id, "item_id": item_id, "gap": gap})
                    if key not in yellow or top["overall"] > yellow[key]["overall"]:
                        yellow[key] = entry
                continue

            key = (top["year"], top["slogan"])
            if key not in confirmed or top["overall"] > confirmed[key]["overall"]:
                confirmed[key] = top
            if log_enabled:
                _log_confirmation(job_id, check_id, crop_counter, top, diagc, command)

            # Does this confirmed button satisfy a standing need?
            enriched = _check_needed_hit(top, buy_rules)
            if enriched is None:
                continue
            if best_needed is None or top["overall"] > best_needed["overall"]:
                best_needed = {"year": top["year"], "slogan": top["slogan"],
                               "overall": top["overall"]}
            if key not in needed_hits or top["overall"] > needed_hits[key]["overall"]:
                needed_hits[key] = enriched

        # Flush this image's rows immediately (durability: a later crash/throttle
        # must not discard rows for images already processed).
        if log_enabled and match_logger is not None and image_records:
            match_logger.log_image_crops(job_id, image_records)

    return {
        "photos_processed": photos_processed,
        "best_score":       best_score_seen,
        "confirmed":        list(confirmed.values()),
        "yellow":           list(yellow.values()),    # human-review candidates
        "needed":           list(needed_hits.values()),
        "log_top_matches":  log_top_matches,
        "best_needed":      best_needed,
        "first_image_bytes": first_image_bytes,
    }


def _post_yellow_review(listing: dict, yellow_buttons: list, job_id: str,
                        confirmed_buttons: list,
                        gemini_summary: str | None = None) -> None:
    """Post a stripped-down human-review block for yellow-confidence buttons.

    Two-step interaction:
      Step 1 — "How many buttons do you see in this lot?"
               Quick-select buttons (1-5, 6-10, 11-20, 21-30, 30+).
               Answer logged as a special confirm_log row (source='user_count').

      Step 2 — For each yellow button: "Do you see [year] — [slogan]?"
               ✅ Yes / ❌ No buttons posted as a single compact message.
               Each answer logged to confirm_log (source='human_verify_yes'
               or 'human_verify_no').

    gemini_summary, if provided (set by /crawl10's Gemini triage step), is
    prepended to the header as an informational line — it does NOT skip or
    replace the human Yes/No review for whatever remains in yellow_buttons.

    Fail-open: any Slack API error is caught and printed, never raised.
    """
    try:
        item_id = listing.get("item_id", "?")
        title   = listing.get("title", "?")[:60]
        url     = listing.get("url") or listing.get("listing_url") or ""
        asking  = listing.get("current_price")
        price_str = f" · ${asking:.2f}" if asking else ""

        # ── Header ───────────────────────────────────────────────────────────
        header_text = (
            f"*Scout review* · <{url}|{title}>{price_str}\n"
            f"✅ Auto-confirmed: {len(confirmed_buttons)} button(s)\n"
            f"🟡 Yellow (needs your eye): {len(yellow_buttons)} candidate(s)"
        )
        if gemini_summary:
            header_text = f"{gemini_summary}\n\n{header_text}"

        # ── Step 1: how many buttons? ─────────────────────────────────────────
        count_meta = json.dumps({"job_id": job_id, "item_id": item_id})
        count_elements = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": label},
                "action_id": f"scout_count_{label.replace('+', 'plus')}",
                "value": json.dumps({"job_id": job_id, "item_id": item_id,
                                     "bucket": label}),
            }
            for label in ["1-5", "6-10", "11-20", "21-30", "30+"]
        ]

        # ── Step 2: yes/no for each yellow button ────────────────────────────
        yellow_blocks = []
        for btn in yellow_buttons[:8]:   # cap at 8 to stay within Slack block limits
            yr    = btn.get("year",   "?")
            sl    = btn.get("slogan", "?")
            score = btn.get("overall", 0)
            gap   = btn.get("gap")
            gap_str = f" · gap {gap:.2f}" if gap is not None else ""
            verify_val = json.dumps({
                "job_id":   job_id,
                "item_id":  item_id,
                "check_id": btn.get("check_id"),
                "year":     yr,
                "slogan":   sl,
                "overall":  round(score, 4),
            })
            yellow_blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🟡 *{yr}* — _{sl}_ ({int(score*100)}%{gap_str})",
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Yes"},
                    "style": "primary",
                    "action_id": "scout_verify_yes",
                    "value": verify_val,
                },
            })
            yellow_blocks.append({
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ No — not in lot"},
                    "style": "danger",
                    "action_id": "scout_verify_no",
                    "value": verify_val,
                }],
            })

        blocks = [
            {"type": "section",
             "text": {"type": "mrkdwn", "text": header_text}},
            {"type": "section",
             "text": {"type": "mrkdwn",
                      "text": "📦 *How many buttons do you see in this lot?*"}},
            {"type": "actions", "elements": count_elements},
            {"type": "divider"},
        ] + yellow_blocks

        app.client.chat_postMessage(
            token=_slack_token,
            channel=_channel_id,
            blocks=blocks,
            text=f"Scout review: {len(yellow_buttons)} yellow button(s) — {title}",
        )
        print(
            f">>> SCOUT_REVIEW: posted {len(yellow_buttons)} yellow button(s) "
            f"for item {item_id}.",
            flush=True,
        )
    except Exception as exc:
        print(f"!!! SCOUT_REVIEW: post failed for {listing.get('item_id','?')}: {exc}",
              flush=True)


# --- SLACK ACTIONS: scout human-review responses ----------------------------

@app.action("scout_verify_yes")
def handle_scout_verify_yes(ack, body):
    ack()
    _handle_scout_verify(body, verified=True)


@app.action("scout_verify_no")
def handle_scout_verify_no(ack, body):
    ack()
    _handle_scout_verify(body, verified=False)


def _handle_scout_verify(body, *, verified: bool) -> None:
    """Log a Yes/No human-verification answer to confirm_log."""
    try:
        val      = json.loads(body["actions"][0]["value"])
        job_id   = val.get("job_id")
        check_id = val.get("check_id")
        year     = val.get("year")
        slogan   = val.get("slogan")
        overall  = val.get("overall")
        source   = "human_verify_yes" if verified else "human_verify_no"

        rec = mlog.build_confirm_record(
            service="ebayscout", command="/crawl",
            job_id=job_id, thread_ts=None,
            crop_num=None, check_id=check_id,
            user_id=(body.get("user") or {}).get("id", ""),
            chosen_year=year, chosen_phrase=slogan,
            chosen_type="Football", source=source,
            rank_restricted=None, rank_shadow=None,
            shadow_leaderboard_size=None,
        )
        if match_logger is not None:
            match_logger.log_confirmation(check_id or f"verify:{job_id}", rec)

        # Update the button in-place to show it's been answered
        emoji = "✅" if verified else "❌"
        try:
            app.client.chat_update(
                token=_slack_token,
                channel=body["channel"]["id"],
                ts=body["message"]["ts"],
                text=f"{emoji} {year} — {slogan} ({source})",
                blocks=[{
                    "type": "section",
                    "text": {"type": "mrkdwn",
                             "text": f"{emoji} *{year}* — _{slogan}_ · logged"},
                }],
            )
        except Exception:
            pass   # UI update is cosmetic — don't let it break logging

        print(
            f">>> SCOUT_VERIFY: {source} for {year} / {slogan[:30]} "
            f"(overall={overall}, job={job_id})",
            flush=True,
        )
    except Exception as exc:
        print(f"!!! SCOUT_VERIFY: failed: {exc}", flush=True)


@app.action(re.compile(r"^scout_count_"))
def handle_scout_count(ack, body):
    """Log the user's button-count estimate for a lot."""
    ack()
    try:
        val     = json.loads(body["actions"][0]["value"])
        job_id  = val.get("job_id")
        item_id = val.get("item_id")
        bucket  = val.get("bucket", "?")

        # Log as a synthetic confirm_log row so it's queryable alongside
        # det_count_noinput.  chosen_phrase carries the bucket string;
        # source='user_count' identifies the row type.
        rec = mlog.build_confirm_record(
            service="ebayscout", command="/crawl",
            job_id=job_id, thread_ts=None,
            crop_num=None, check_id=f"count:{item_id}",
            user_id=(body.get("user") or {}).get("id", ""),
            chosen_year=None, chosen_phrase=bucket,
            chosen_type=None, source="user_count",
            rank_restricted=None, rank_shadow=None,
            shadow_leaderboard_size=None,
        )
        if match_logger is not None:
            match_logger.log_confirmation(f"count:{item_id}", rec)

        # Replace the count buttons with a confirmation so the user knows it landed
        try:
            app.client.chat_update(
                token=_slack_token,
                channel=body["channel"]["id"],
                ts=body["message"]["ts"],
                text=f"📦 Button count logged: {bucket}",
                blocks=[{
                    "type": "section",
                    "text": {"type": "mrkdwn",
                             "text": f"📦 Button count logged: *{bucket}*"},
                }],
            )
        except Exception:
            pass

        print(
            f">>> SCOUT_COUNT: logged bucket={bucket} "
            f"for item={item_id} job={job_id}",
            flush=True,
        )
    except Exception as exc:
        print(f"!!! SCOUT_COUNT: failed: {exc}", flush=True)


def _run_daily_scan(
    ignore_seen: bool = False,
    dry_run: bool | None = None,
    year_crawl: bool = False,
    era_crawl: bool = False,
    limit: int = 0,
    hunt_ids: bool = False,
) -> int:
    """
    Runs the full eBay + Etsy scan pipeline, reusing the already-loaded
    buy_rules and clip_matcher state rather than re-initialising.

    ignore_seen: when True, process every fetched listing regardless of the
                 seen_items dedup store (one-shot backfill). seen is still
                 checkpointed so the run is resumable and later runs stay
                 forward-only.
    dry_run:     None → use config.DRY_RUN; True/False overrides it for this run.
    year_crawl:  when True, source listings from year-augmented searches for
                 needed years (eBay only) instead of the general queries; each
                 result is matched restricted to its search year.
    era_crawl:   when True, source listings from the Mellon + Citizens bank
                 searches (eBay only); each result is matched restricted to its
                 era's year range. Run after the year crawl (seen-dedup skips
                 what it caught).
    limit:       when > 0, process at most this many UNSEEN listings this call
                 (resumable chunk mode). Always forward-only so repeated calls
                 walk the backlog one request-window at a time, keeping each at
                 full CPU. Must run live (not dry_run) for the seen cursor to
                 persist between chunks.
    hunt_ids:    fetch the specific item_ids in the HUNT_IDS_BLOB list directly
                 by ID (Browse getItem) and feed them through the pipeline, so
                 each gets a full scan_log/market record with its current asking
                 price. Runs additively during year_crawl (rebuilds the market
                 DB from a prior run's IDs) and standalone via ?hunt_ids=1
                 (skips the general search). Pair with limit for big ID lists.

    Returns the number of unseen listings still remaining after this call
    (chunk mode); 0 when not chunked or the backlog is exhausted.
    """
    from . import ebay_client, seen_items as seen_store

    dry_run = config.DRY_RUN if dry_run is None else dry_run

    print(
        f">>> SCAN: Daily scan starting "
        f"[dry_run={dry_run}, ignore_seen={ignore_seen}, year_crawl={year_crawl}, "
        f"era_crawl={era_crawl}]...",
        flush=True,
    )

    # eBay credentials — Browse API needs both the App ID (client id) and
    # the Cert ID (client secret) for the client-credentials OAuth grant.
    try:
        ebay_app_id  = _get_secret("EBAY_APP_ID")
        ebay_cert_id = _get_secret("EBAY_CERT_ID")
    except Exception as exc:
        print(f"!!! SCAN: eBay credentials not available — skipping eBay: {exc}", flush=True)
        ebay_app_id = ebay_cert_id = None

    seen = seen_store.load_seen()

    all_listings: list[dict] = []

    if year_crawl:
        # --- Year-augmented needed-year deep crawl (eBay only) ---
        years = needed_years(buy_rules)
        if not years:
            print(">>> SCAN: year_crawl requested but no needed years in buy_rules — exiting.",
                  flush=True)
            return 0
        if not (ebay_app_id and ebay_cert_id):
            print(">>> SCAN: year_crawl needs eBay credentials — exiting.", flush=True)
            return 0
        print(f">>> SCAN: Year crawl over {len(years)} needed years: "
              f"{sorted(years)}", flush=True)
        try:
            all_listings.extend(ebay_client.find_year_augmented_listings(
                client_id=ebay_app_id,
                client_secret=ebay_cert_id,
                year_queries=build_year_queries(config.YEAR_CRAWL_TERMS, years),
                excluded_sellers=config.EXCLUDED_SELLERS,
                max_results=config.EBAY_MAX_RESULTS,
            ))
            # PSU terms restricted to Sports Memorabilia, same as the general scan.
            all_listings.extend(ebay_client.find_year_augmented_listings(
                client_id=ebay_app_id,
                client_secret=ebay_cert_id,
                year_queries=build_year_queries(config.YEAR_CRAWL_PSU_TERMS, years),
                excluded_sellers=config.EXCLUDED_SELLERS,
                max_results=config.EBAY_MAX_RESULTS,
                category_ids=config.SPORTS_MEMO_CATEGORY_ID,
            ))
        except Exception as exc:
            print(f"!!! SCAN: year crawl query failed: {exc}", flush=True)
    elif era_crawl:
        # --- On-demand Mellon + Citizens bank crawl (eBay only) ---
        if not (ebay_app_id and ebay_cert_id):
            print(">>> SCAN: era_crawl needs eBay credentials — exiting.", flush=True)
            return 0
        print(f">>> SCAN: Era crawl over {len(config.MELLON_CITIZENS_ERA_QUERIES)} "
              f"bank queries (Mellon + Citizens).", flush=True)
        try:
            all_listings.extend(_run_era_queries(
                ebay_client, ebay_app_id, ebay_cert_id, config.MELLON_CITIZENS_ERA_QUERIES))
        except Exception as exc:
            print(f"!!! SCAN: era crawl query failed: {exc}", flush=True)
    elif not hunt_ids:
        # General daily pass — skipped when hunting IDs standalone (?hunt_ids=1
        # with no year/era crawl), since the hunt supplies its own listings.
        if ebay_app_id and ebay_cert_id:
            try:
                ebay_listings = _collect_ebay_listings(ebay_app_id, ebay_cert_id)
                all_listings.extend(ebay_listings)
                print(f">>> SCAN: eBay returned {len(ebay_listings)} listings "
                      f"(general + PSU/sports).", flush=True)
            except Exception as exc:
                print(f"!!! SCAN: eBay query failed: {exc}", flush=True)
        else:
            print(">>> SCAN: Skipping eBay (no EBAY_APP_ID / EBAY_CERT_ID).", flush=True)

        # Etsy listings (general pass only — Etsy has no year-augmented crawl)
        try:
            etsy_api_key = _get_secret("ETSY_API_KEY")
        except Exception as exc:
            print(f"!!! SCAN: ETSY_API_KEY not available — skipping Etsy: {exc}", flush=True)
            etsy_api_key = None

        if etsy_api_key:
            try:
                etsy_listings = etsy_client.find_all_listings(
                    api_key=etsy_api_key,
                    queries=config.EBAY_SEARCH_QUERIES,
                    excluded_sellers=config.ETSY_EXCLUDED_SELLERS,
                    max_results=config.EBAY_MAX_RESULTS,
                )
                all_listings.extend(etsy_listings)
                print(f">>> SCAN: Etsy returned {len(etsy_listings)} listings.", flush=True)
            except Exception as exc:
                print(f"!!! SCAN: Etsy query failed: {exc}", flush=True)
        else:
            print(">>> SCAN: Skipping Etsy (no ETSY_API_KEY).", flush=True)

    # ID hunt: fetch specific known item_ids directly (e.g. recovered from a
    # prior run's logs) to rebuild full market data — most importantly each
    # listing's asking price, which the scan stdout never recorded. Three drivers:
    #   - ?hunt_ids=1   explicit on-demand hunt (chunk it with ?limit=N)
    #   - ?year_crawl=1 hunts alongside the yearly crawl
    #   - the plain DAILY scheduled run auto-drains DAILY_HUNT_BUDGET ids/day in
    #     the background — free, bounded, and self-stopping once all ids are seen.
    # Forward-only: already-seen ids are dropped BEFORE fetching, so we never
    # spend a getItem call (or money) on an id we won't process this run.
    auto_daily = (not year_crawl and not era_crawl and not hunt_ids
                  and not ignore_seen and not dry_run)
    if hunt_ids or year_crawl or (auto_daily and config.DAILY_HUNT_BUDGET > 0):
        if ebay_app_id and ebay_cert_id:
            hunt_all = seen_store.load_hunt_ids()
            cap = limit if limit > 0 else (config.DAILY_HUNT_BUDGET if auto_daily else 0)
            hunt = select_hunt_ids(hunt_all, seen, ignore_seen=ignore_seen, cap=cap)
            unseen_total = (len(hunt_all) if ignore_seen
                            else sum(1 for i in hunt_all if seen_store.is_new(i, seen)))
            if hunt:
                print(f">>> SCAN: ID hunt — fetching {len(hunt)} of {unseen_total} unseen "
                      f"id(s) (cap={cap or 'none'}); {max(0, unseen_total - len(hunt))} "
                      f"will remain after this run.", flush=True)
                try:
                    all_listings.extend(
                        ebay_client.find_listings_by_ids(ebay_app_id, ebay_cert_id, hunt))
                except Exception as exc:
                    print(f"!!! SCAN: ID hunt failed: {exc}", flush=True)
            elif hunt_all:
                print(">>> SCAN: ID hunt — backlog drained (all hunt ids already seen).",
                      flush=True)
        else:
            print(">>> SCAN: hunt requested but no eBay credentials — skipping hunt.",
                  flush=True)

    if not all_listings:
        print(">>> SCAN: No listings retrieved from any source — exiting.", flush=True)
        return 0

    # Drop cross-pass duplicate item_ids before processing. The general (eBay +
    # PSU + Etsy) and the year/era crawls overlap heavily, so the same listing is
    # routinely fetched 2-3x; without this it is processed and counted repeatedly
    # (the May 2026 backfill was ~20% duplicate work — 1024 rows / 814 unique).
    _pre_dedup = len(all_listings)
    all_listings = dedup_listings(all_listings)
    if len(all_listings) != _pre_dedup:
        print(f">>> SCAN: De-duplicated listings {_pre_dedup} -> {len(all_listings)} "
              f"({_pre_dedup - len(all_listings)} cross-pass duplicates dropped).", flush=True)

    # Forward-only by default; ignore_seen re-evaluates everything visible.
    # Chunk mode (limit > 0) is ALWAYS forward-only/seen-filtered so repeated
    # calls advance through the backlog a request-window at a time instead of
    # re-processing the same head — see the run_scan docstring. A large backfill
    # done as one request outlives its CPU-funded window and the tail crawls
    # under throttle (the May 2026 backfill: 750 listings fast, then ~274 over
    # 15h); chunking keeps every call at full CPU.
    if ignore_seen and not limit:
        candidate = all_listings   # one-shot backfill: re-evaluate everything visible
    else:
        candidate = [l for l in all_listings if seen_store.is_new(l["item_id"], seen)]

    _chunk_remaining = 0
    if limit and limit > 0:
        new_listings = candidate[:limit]
        _chunk_remaining = max(0, len(candidate) - len(new_listings))
        print(f">>> SCAN: chunk mode — {len(candidate)} unseen of {len(all_listings)} "
              f"fetched; processing {len(new_listings)} this chunk, "
              f"{_chunk_remaining} remaining after.", flush=True)
        if dry_run:
            print(">>> SCAN: WARNING — chunk mode under dry_run writes no seen_items, "
                  "so the cursor will NOT advance; run live to walk the backlog.", flush=True)
    else:
        new_listings = candidate
    ebay_new     = sum(1 for l in new_listings if not l["item_id"].startswith("etsy_"))
    etsy_new     = sum(1 for l in new_listings if     l["item_id"].startswith("etsy_"))
    print(
        f">>> SCAN: {len(new_listings)} listings to process"
        f"{' (ignore_seen backfill)' if ignore_seen else ' new'}.",
        flush=True,
    )

    stat_alerted              = 0
    stat_confirmed_not_needed = 0   # has a confirmed (green/auto) button, none needed
    stat_rejected             = 0   # no crop reached the green/auto confirmation gate
    _listings_since_save = 0
    scan_log_records: list[dict] = []   # one record per processed listing (groundwork data)
    _scanlog_flushed = 0                # how many records already appended to GCS

    from . import clip_matcher as _cm  # lazy — torch/clip imported here if not yet

    ref_years = _cm.reference_years()  # known years; gates single-title-year restriction
    job_id = str(uuid.uuid4())         # one logging job id for this scan run

    for listing in new_listings:
        item_id = listing["item_id"]
        asking  = listing.get("current_price", 0.0)
        title   = listing.get("title", "?")
        seller  = listing.get("seller", "")

        # Decide which years matching may consider for this listing (tight→broad).
        # A decade marker in the title ("1990s") means the lot spans the whole
        # decade — broaden, never lock to one year, or we miss most of the lot.
        title_years_all = extract_years(title)
        title_decades   = extract_decades(title)
        search_year     = listing.get("search_year")
        search_era      = listing.get("search_era")
        if search_year:
            # Year-crawl hit. If the title is a decade lot, broaden the exact
            # search year to the whole decade so other-year buttons aren't missed.
            restrict_years: set[int] | None = {int(search_year)} | title_decades
        elif search_era:
            restrict_years = era_year_set(search_era, config.BUTTON_ERAS) or None
        elif title_decades:
            # General result that names a decade → consider that whole decade.
            restrict_years = title_decades
        elif len(title_years_all) == 1 and next(iter(title_years_all)) in ref_years:
            restrict_years = {next(iter(title_years_all))}
        else:
            restrict_years = None

        try:
            if item_id.startswith("etsy_"):
                picture_urls = [listing["gallery_url"]] if listing.get("gallery_url") else []
            elif ebay_app_id and ebay_cert_id:
                picture_urls = ebay_client.get_item_pictures(ebay_app_id, ebay_cert_id, item_id)
                if not picture_urls and listing.get("gallery_url"):
                    picture_urls = [listing["gallery_url"]]
            else:
                picture_urls = [listing["gallery_url"]] if listing.get("gallery_url") else []

            result = _evaluate_listing(
                listing, picture_urls, restrict_years,
                command="/run-scan", job_id=job_id,
                title_years=title_years_all, title_count=extract_lot_count(title),
                log_enabled=not dry_run,
            )
            best_score_seen  = result["best_score"]
            photos_processed = result["photos_processed"]
            confirmed        = result["confirmed"]
            needed_buttons   = result["needed"]
            log_top_matches  = result["log_top_matches"]
            best_needed      = result["best_needed"]

            # Rejected = no crop reached the green/auto confirmation gate.
            if not confirmed:
                stat_rejected += 1
                # Greppable title log: filter Cloud Logging for "TITLE: [rejected".
                print(f">>> TITLE: [rejected {best_score_seen:.2f}] [{seller}] {title}", flush=True)
                scan_log_records.append(_scan_log_record(
                    listing, photos_processed, best_score_seen, log_top_matches,
                    needed_hit=False, alerted=False, best_needed=best_needed,
                ))
            else:
                listing_alerted = False
                if needed_buttons:
                    # Value of the WHOLE lot (every confirmed button has a sheet
                    # price, needed or not), not just the needed buttons.
                    lot_value, _, _ = pipeline_classify.lot_value_and_deal(
                        confirmed, _price_of, None)
                    print(
                        f">>> TITLE: [needed {best_score_seen:.2f} "
                        f"{needed_buttons[0]['year']} {needed_buttons[0]['slogan']}] [{seller}] {title}",
                        flush=True,
                    )
                    if dry_run:
                        print(f"    [DRY RUN] Would post needed-buttons alert for {item_id}", flush=True)
                    else:
                        notifier.send_needed_alert(
                            slack_token=_slack_token, channel=_channel_id, listing=listing,
                            needed_buttons=needed_buttons, asking_price=asking, lot_value=lot_value,
                        )
                    listing_alerted = True

                if listing_alerted:
                    stat_alerted += 1
                else:
                    stat_confirmed_not_needed += 1
                    print(f">>> TITLE: [confirmed-not-needed {best_score_seen:.2f}] "
                          f"[{seller}] {title}", flush=True)

                scan_log_records.append(_scan_log_record(
                    listing, photos_processed, best_score_seen, log_top_matches,
                    needed_hit=bool(needed_buttons), alerted=listing_alerted,
                    best_needed=best_needed,
                ))

        except Exception as exc:
            print(f"!!! SCAN: Error processing {item_id}: {exc}", flush=True)
            traceback.print_exc()

        seen_store.mark_seen(item_id, seen)
        _listings_since_save += 1
        if _listings_since_save >= 50:
            if not dry_run:
                seen_store.save_seen(seen)
            # Checkpoint the scan-log data every 50 too (both modes) so a 30-min
            # timeout on a big run never loses the records collected so far.
            pending = scan_log_records[_scanlog_flushed:]
            if pending and seen_store.append_scan_log(pending):
                _scanlog_flushed = len(scan_log_records)
            _listings_since_save = 0

    if dry_run:
        print("[DRY RUN] Skipping save_seen().", flush=True)
    elif not seen_store.save_seen(seen):
        notifier.send_warning(_slack_token, _channel_id,
                              "Failed to save seen_items.json — next scan may re-alert.")

    # Flush any scan-log records not yet checkpointed (both modes). The bulk is
    # already on GCS from the every-50 checkpoints above — this writes the tail.
    pending = scan_log_records[_scanlog_flushed:]
    if pending and not seen_store.append_scan_log(pending):
        print("!!! SCAN: Failed to append final scan log to GCS.", flush=True)
    else:
        _scanlog_flushed = len(scan_log_records)
    print(f">>> SCAN: scan-log records this run: {len(scan_log_records)} "
          f"(flushed {_scanlog_flushed}).", flush=True)

    # Dry-run preview also posts the single Slack digest of candidate scores.
    if dry_run:
        try:
            notifier.send_backfill_digest(
                slack_token=_slack_token,
                channel=_channel_id,
                records=scan_log_records,
                threshold=config.NEEDED_MATCH_THRESHOLD,
            )
        except Exception as exc:
            print(f"!!! SCAN: Failed to post backfill digest: {exc}", flush=True)

    if dry_run:
        print(
            f"[DRY RUN] Summary: alerted={stat_alerted}, "
            f"confirmed_not_needed={stat_confirmed_not_needed}, rejected={stat_rejected}",
            flush=True,
        )
    else:
        try:
            notifier.send_scan_summary(
                slack_token=_slack_token,
                channel=_channel_id,
                alerted=stat_alerted,
                confirmed_not_needed=stat_confirmed_not_needed,
                rejected=stat_rejected,
                ebay_count=ebay_new,
                etsy_count=etsy_new,
            )
        except Exception as exc:
            print(f"!!! SCAN: Failed to post scan summary: {exc}", flush=True)

    if limit and limit > 0:
        print(f">>> SCAN: chunk complete — {_chunk_remaining} unseen listings remain "
              f"(re-run the same chunk call to continue; 0 = backlog exhausted).", flush=True)
    print(">>> SCAN: Daily scan complete.", flush=True)
    return _chunk_remaining


# ---------------------------------------------------------------------------
# on-demand2 / crawl (called from /internal/crawl)
# ---------------------------------------------------------------------------

def _feed_lot_to_pipeline(listing: dict, ebay_app_id: str, ebay_cert_id: str,
                          command: str) -> str | None:
    """Push one lot's PRIMARY photo into the Gemini pipeline (upload-only).

    Downloads photo 0, writes a pending-context blob (so the async Gem result can
    be correlated back to this listing even after a cold start), and uploads the
    image to the GCS pipeline-input prefix the watcher polls. ``command``
    ("/crawl") is carried in the context. Returns the
    correlation key on success, or None when the lot has no photo.

    Does NOT mark the lot seen — that happens on confirmation in
    process_pipeline_lot, so a lot the Gem never answers is retried, never lost.
    """
    from . import ebay_client, image_proc as _ip

    item_id = listing["item_id"]
    title   = listing.get("title", "?")
    seller  = listing.get("seller", "")

    picture_urls = ebay_client.get_item_pictures(ebay_app_id, ebay_cert_id, item_id)
    if not picture_urls and listing.get("gallery_url"):
        picture_urls = [listing["gallery_url"]]
    if not picture_urls:
        print(f">>> PIPELINE FEED: {item_id} has no photos — skipping.", flush=True)
        return None

    image_bytes = _ip.download_image(picture_urls[0])   # PRIMARY photo only

    # Correlation key carried in the GCS object name; the authoritative listing
    # context is persisted to GCS so the async result survives a cold start.
    key = uuid.uuid4().hex[:12]
    ctx = {
        "key":          key,
        "item_id":      item_id,
        "title":        title,
        "seller":       seller,
        "asking":       listing.get("current_price"),
        "url":          listing.get("url") or listing.get("listing_url") or "",
        "gallery_url":  listing.get("gallery_url"),
        "search_era":   listing.get("search_era") or "",
        "title_years":   sorted(extract_years(title)),
        "title_decades": sorted(extract_decades(title)),
        "command":      command,
        "created":      datetime.datetime.utcnow().isoformat() + "Z",
    }
    seen_items.save_pending_context(key, ctx)
    if not seen_items.upload_pipeline_input(key, image_bytes):
        raise RuntimeError("pipeline-input upload to GCS failed")
    print(f">>> PIPELINE FEED: uploaded {item_id} -> pipeline as key={key} ({command}).",
          flush=True)
    return key


def _collect_ebay_listings(ebay_app_id, ebay_cert_id):
    """The shared eBay pull used by BOTH the daily scan and /crawl, so the two
    never search different sets: the general queries (EBAY_SEARCH_QUERIES) plus
    the PSU queries restricted to Sports-Mem (drops Power-Supply-Unit noise),
    with the standard seller/keyword/category safeguards from config. Returns a
    combined (not-yet-deduped) list; callers dedup."""
    out = ebay_client.find_all_listings(
        client_id=ebay_app_id, client_secret=ebay_cert_id,
        queries=config.EBAY_SEARCH_QUERIES,
        excluded_sellers=config.EXCLUDED_SELLERS,
        max_results=config.EBAY_MAX_RESULTS,
    )
    out += ebay_client.find_all_listings(
        client_id=ebay_app_id, client_secret=ebay_cert_id,
        queries=config.PSU_SEARCH_QUERIES,
        excluded_sellers=config.EXCLUDED_SELLERS,
        max_results=config.EBAY_MAX_RESULTS,
        category_ids=config.SPORTS_MEMO_CATEGORY_ID,
    )
    return out


def _run_crawl(n: int) -> int:
    """Feed the daily eBay pull — the SAME search as the 9am scan (general + PSU/
    sports queries via _collect_ebay_listings, same seller/keyword/category
    safeguards) — into the Gemini pipeline, capped at N lots. /crawl and the daily
    scan therefore search identical sets; only the cap + processing differ.

    SEEN-aware: each lot's primary photo is pushed into the pipeline (watcher →
    Gem → GCS → /pipeline/notify); detection, matching, deal-posting, auto-
    staging, and the per-lot logs (scan_log.jsonl + marking the lot seen) all
    happen asynchronously in process_pipeline_lot as each Gem read returns. A lot
    is marked seen ONLY on confirmation, so a lot the Gem never answers is
    retried, never lost.

    First run (per the GCS marker) may re-feed already-seen lots to reach N;
    every run after feeds only unseen lots. Returns the number of lots fed.
    """
    from . import ebay_client, seen_items as seen_store

    try:
        ebay_app_id  = _get_secret("EBAY_APP_ID")
        ebay_cert_id = _get_secret("EBAY_CERT_ID")
    except Exception as exc:
        print(f"!!! CRAWL: eBay credentials unavailable — aborting: {exc}", flush=True)
        notifier.send_warning(_slack_token, _channel_id, "/crawl: no eBay credentials.")
        return 0

    first_run = not seen_store.ondemand2_first_run_done()
    print(f">>> CRAWL: starting (n={n}, first_run={first_run}) — same eBay pull "
          f"as the daily scan.", flush=True)

    try:
        all_listings = _collect_ebay_listings(ebay_app_id, ebay_cert_id)
    except Exception as exc:
        print(f"!!! CRAWL: search failed: {exc}", flush=True)
        notifier.send_warning(_slack_token, _channel_id, f"/crawl search failed: {exc}")
        return 0

    all_listings = dedup_listings(all_listings)
    seen = seen_store.load_seen()

    # Skip lots already run (in `seen`); on the very first run feed everything to
    # reach the N cap. `seen` is marked on confirmation (process_pipeline_lot),
    # never here, so failed/un-answered lots stay re-feedable.
    if first_run:
        candidate = all_listings
    else:
        candidate = [l for l in all_listings if seen_store.is_new(l["item_id"], seen)]
    new_listings = candidate[: n]
    print(f">>> CRAWL: {len(all_listings)} unique found; feeding "
          f"{len(new_listings)} into the pipeline (cap {n}).", flush=True)

    if not new_listings:
        notifier.send_warning(_slack_token, _channel_id,
                              "/crawl: no lots to feed this run.")
        if first_run:
            seen_store.mark_ondemand2_first_run_done()
        return 0

    fed = 0
    for listing in new_listings:
        item_id = listing["item_id"]
        seller  = listing.get("seller", "")
        try:
            if _feed_lot_to_pipeline(listing, ebay_app_id, ebay_cert_id, "/crawl"):
                fed += 1
        except Exception as exc:
            print(f"!!! CRAWL: feed failed for {item_id} [{seller}]: {exc}", flush=True)
            traceback.print_exc()

    if first_run:
        seen_store.mark_ondemand2_first_run_done()

    notifier.send_warning(
        _slack_token, _channel_id,
        f"🔎 `/crawl {n}`: fed {fed}/{len(new_listings)} lot(s) into the Gemini "
        f"pipeline{' (first run — included already-seen lots)' if first_run else ''}. "
        f"Deals (needed buttons / undervalued lots) post here as each Gem read "
        f"returns; confirmed lots are marked seen so they are never re-run."
    )
    print(f">>> CRAWL: feed complete — fed={fed}/{len(new_listings)} "
          f"(n={n}, first_run={first_run}).", flush=True)
    return fed


# ---------------------------------------------------------------------------
# Startup (called from gunicorn post_fork hook)
# ---------------------------------------------------------------------------

def startup() -> None:
    """Load Google Sheets, the match-logging workbook, and CLIP in the background."""
    global buy_rules, match_logger

    print(">>> STARTUP: Loading buy rules...", flush=True)
    try:
        sheets_json    = _get_secret("GOOGLE_SHEETS_JSON")
        spreadsheet_id = _get_secret("SPREADSHEET_ID")
        buy_rules      = sheets_client.load_buy_rules(sheets_json, spreadsheet_id)
    except Exception as exc:
        print(f"!!! STARTUP: Sheets error: {exc}", flush=True)

    # Open the shared match-logging workbook (LOGGER_ID) and build the per-event
    # SheetLogger. Fail-open: any error disables logging but never blocks the scan.
    try:
        sheets_json   = _get_secret("GOOGLE_SHEETS_JSON")
        logger_id     = _get_secret("LOGGER_ID")
        gclient       = sheets_client.get_gspread_client(sheets_json)
        match_ws, confirm_ws = mlog.open_log_sheets(gclient, logger_id)
        match_logger  = mlog.SheetLogger(match_ws, confirm_ws, service="ebayscout")
    except Exception as exc:
        print(f"!!! STARTUP: match-logging init failed (logging disabled): {exc}", flush=True)
        match_logger = mlog.SheetLogger(None, None, service="ebayscout")

    # Hydrate CLIP in the background.  On a cold, CPU-throttled container this
    # may not finish until an HTTP request (a scan) provides CPU — those paths
    # call _ensure_clip_loaded() to force it.
    threading.Thread(target=_ensure_clip_loaded, daemon=True).start()


