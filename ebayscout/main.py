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
import hmac
import os
import re
import threading
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
from . import pipeline_classify
from . import yolo_test
from . import normalize
from . import price_log
from . import purchases
from . import scan_log
from . import seen_items
from . import seller_listings
from . import shipping_backfill
from . import confusable_slogans as cfs
from . import edition_twins as edt
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

# Slack channel IDs and Google Sheet keys, not credentials. Secret Manager bills
# every stored version past the free six, so these are read from a plain Cloud
# Run env var of the same name when one is set, with Secret Manager as the
# fallback. Tokens and API keys always come from Secret Manager. The values stay
# on the service config, not in this repo, which is public.
_PLAIN_ENV_IDS = frozenset({"CHANNEL_ID_EBAY", "SPREADSHEET_ID", "LOGGER_ID"})


def _get_secret(secret_id: str) -> str:
    if secret_id in _PLAIN_ENV_IDS:
        value = os.environ.get(secret_id, "").strip()
        if value:
            return value
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

# Guards the seen read-modify-write (_mark_item_seen_now, _flush_seen_marks):
# the handoff marks each lot seen as buttonmatcher accepts it.
_seen_lock = threading.Lock()


# Shared secret for buttonmatcher's /internal/ebay_scout_lot (the handoff) and
# the operator's manual calls here (_operator_request_ok).  Named for the
# retired Gemini pipeline's watcher, which presented it first.
_PIPELINE_SHARED_SECRET = os.environ.get("PIPELINE_SHARED_SECRET", "")

# Edition-twin registry (edition_twins.py): normalized slogan -> [entries],
# built once after CLIP init (see _ensure_clip_loaded()) from the loaded text
# DB. Same slogan text reused across multiple
# editions (different year and/or sport) can't be disambiguated by
# text-agreement auto-confirm alone; _evaluate_listing's auto-resolve gate
# checks this registry and routes a twin-family winner to human (yellow)
# review instead of silently picking an edition. Kill switch:
# BUTTONMATCHER_TWIN_GUARD=0 (shared convention with buttonmatcher).
TWIN_REGISTRY: dict = {}
CONFUSABLE_REGISTRY: dict = {}


def _twin_guard_enabled() -> bool:
    return os.environ.get("BUTTONMATCHER_TWIN_GUARD", "1").strip() not in (
        "0", "false", "False",
    )


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
            # Edition-twin registry, from the loaded text DB.
            # Fail-open: a bad entry or a build exception must never block CLIP
            # wake-up; TWIN_REGISTRY just stays {} and the guard is a no-op.
            try:
                global TWIN_REGISTRY
                _phrases, _years, _types = cm.text_db_arrays()
                _twin_entries = [
                    {"slogan": p, "year": y, "type": t}
                    for p, y, t in zip(_phrases, _years, _types)
                ]
                TWIN_REGISTRY = edt.build_twin_registry(_twin_entries, normalize.normalize_key)
                print(f">>> TWINS: {edt.registry_summary(TWIN_REGISTRY)}", flush=True)
            except Exception as exc:
                TWIN_REGISTRY = {}
                print(f"!!! TWINS: registry build failed (guard fails open): {exc}", flush=True)
            # Confusable-slogan registry — the same entries again.  Unlike
            # buttonmatcher this does NOT gate auto-confirm (there is no human
            # lane here to demote into); it only flags the deal alert, because a
            # same-year look-alike moves the two numbers the alert reports:
            # amount_needed and max_price_single.  Fail-open: an empty registry
            # just means alerts post unflagged, exactly as they do today.
            try:
                global CONFUSABLE_REGISTRY
                CONFUSABLE_REGISTRY = cfs.build_confusable_registry(
                    cfs.CONFUSABLE_GROUPS, _twin_entries, normalize.normalize_key)
                print(f">>> CONFUSABLE: {cfs.registry_summary(CONFUSABLE_REGISTRY)}",
                      flush=True)
            except Exception as exc:
                CONFUSABLE_REGISTRY = {}
                print(f"!!! CONFUSABLE: registry build failed (alerts post "
                      f"unflagged): {exc}", flush=True)
            return True
        except Exception as exc:
            print(f"!!! WAKE: CLIP init failed: {exc}", flush=True)
            traceback.print_exc()
            return False


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)


# ---------------------------------------------------------------------------
# Request auth (2026-09-26 security pass)
# ---------------------------------------------------------------------------
# The service is public -- Slack's /crawl and eBay's account-deletion callback
# both need it to be -- so the app checks every non-Slack route itself.  Before
# this, /run-scan and /test-clip checked nothing: anyone could start the costly
# crawls (?year_crawl / ?ignore_seen) or make the service fetch any URL and run
# CLIP on it.  The internal routes also admitted any 127.0.0.1 caller; every
# self-call already sends the secret, so that exception served nothing and was
# the one door a request forgery would use.

def _secret_matches(provided, expected) -> bool:
    """Constant-time header check; an unset (empty) secret never matches."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(str(provided).encode("utf-8"),
                               str(expected).encode("utf-8"))


def _internal_request_ok(req) -> bool:
    """True when ``req`` carries this process's X-Internal-Secret."""
    return _secret_matches(req.headers.get("X-Internal-Secret", ""),
                           _INTERNAL_SECRET)


def _operator_request_ok(req) -> bool:
    """A manual call from the operator: the watcher's shared secret, which the
    operator can read from Secret Manager (see DEPLOY.md), or an internal call."""
    return (_internal_request_ok(req)
            or _secret_matches(req.headers.get("X-Pipeline-Secret", ""),
                               _PIPELINE_SHARED_SECRET))


_runtime_sa: list[str] = []      # cache for _runtime_sa_email (success only)


def _runtime_sa_email() -> str:
    """This service's own runtime service account, from the metadata server.
    DEPLOY.md creates the Scheduler job with the same account, so it is the
    default the scheduler token is checked against.  "" if unavailable."""
    if _runtime_sa:
        return _runtime_sa[0]
    try:
        r = requests.get(
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            "service-accounts/default/email",
            headers={"Metadata-Flavor": "Google"}, timeout=2)
        email = r.text.strip() if r.ok else ""
    except Exception:
        email = ""
    if email:
        _runtime_sa.append(email)
    return email


def _scheduler_token_ok(token: str, host: str) -> bool:
    """Cloud Scheduler's OIDC token: Google-signed, from an allowed service
    account, addressed to this service.

    Allowed accounts: the SCHEDULER_SA_EMAIL env var (comma-separated) when set;
    otherwise config.SCHEDULER_SA_EMAIL -- the live job's account -- plus this
    service's own runtime account.  The scheduler's audience is the
    service URL (DEPLOY.md: --oidc-token-audience="${SERVICE_URL}"), and it calls
    that same URL, so the request's own host is accepted beside SERVICE_URL /
    SCHEDULER_AUDIENCE -- the account check is what an outsider cannot pass.
    """
    allowed = {e.strip() for e in
               os.environ.get("SCHEDULER_SA_EMAIL", "").split(",") if e.strip()}
    if not allowed:
        allowed = {e for e in (getattr(config, "SCHEDULER_SA_EMAIL", ""),
                               _runtime_sa_email()) if e}
    if not allowed:
        print("!!! AUTH: bearer token refused — no scheduler service account "
              "known (set SCHEDULER_SA_EMAIL).", flush=True)
        return False
    # Scheduler's default audience, when the job sets none, is the full target
    # URL -- so the /run-scan form of each base is accepted too.
    bases = [b.rstrip("/") for b in (_SERVICE_URL, f"https://{host}" if host else "")
             if b]
    audiences = {a.rstrip("/") for a in (
        [os.environ.get("SCHEDULER_AUDIENCE", "").strip()] + bases
        + [f"{b}/run-scan" for b in bases]) if a}
    try:
        from google.oauth2 import id_token as _idtok
        from google.auth.transport import requests as _greq
        # audience=None: signature, expiry and issuer only; audience below.
        claims = _idtok.verify_oauth2_token(token, _greq.Request())
    except Exception as exc:
        print(f"!!! AUTH: bad OIDC token: {exc}", flush=True)
        return False
    if claims.get("email") not in allowed or not claims.get("email_verified"):
        print(f"!!! AUTH: token from {claims.get('email')!r} is not an allowed "
              f"scheduler account.", flush=True)
        return False
    if str(claims.get("aud") or "").rstrip("/") not in audiences:
        print(f"!!! AUTH: token audience {claims.get('aud')!r} is not this "
              f"service.", flush=True)
        return False
    return True


def _run_scan_authorized(req) -> bool:
    """/run-scan: Cloud Scheduler's token, or the operator's shared secret."""
    if _operator_request_ok(req):
        return True
    auth = req.headers.get("Authorization", "")
    return (auth.startswith("Bearer ")
            and _scheduler_token_ok(auth.split(" ", 1)[1], req.host))


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

    Auth: Cloud Scheduler's OIDC token or the X-Pipeline-Secret header (see
    _run_scan_authorized; DEPLOY.md has the manual curl).
    """
    global buy_rules
    if not _run_scan_authorized(request):
        return jsonify({"status": "forbidden"}), 403
    # Read before the scan runs, so a long scan can't carry it past midnight UTC.
    purchases_day = purchases.is_pull_day(datetime.datetime.now(datetime.timezone.utc))

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
        if not (year_crawl or era_crawl or hunt_ids) and config.DAILY_PIPELINE_FEED:
            # DEFAULT daily scan: hand the day's unseen lots to buttonmatcher
            # (_run_crawl; until 2026-10-25 it fed the Gemini pipeline). /crawl
            # is the manual one-off. The year/era/hunt market-DB variants stay on
            # the CLIP path below; DAILY_PIPELINE_FEED=0 reverts the default to
            # the CLIP scan.
            effective_dry = config.DRY_RUN if dry_run_param is None else dry_run_param
            n = limit if limit > 0 else config.DAILY_PIPELINE_N
            remaining = _run_crawl(n, source="daily", ignore_seen=ignore_seen,
                                   dry_run=effective_dry)
        else:
            remaining = _run_daily_scan(ignore_seen=ignore_seen, dry_run=dry_run_param,
                                        year_crawl=year_crawl, era_crawl=era_crawl,
                                        limit=limit, hunt_ids=hunt_ids)
        # Mondays, the plain scheduled scan also pulls the operator's purchases
        # (operator, 2026-10-04): still inside this request, so it keeps the CPU.
        # A manual variant (?year_crawl, ?limit, …) or a dry run never does.
        plain_run = not (year_crawl or era_crawl or hunt_ids or ignore_seen or limit)
        dry = config.DRY_RUN if dry_run_param is None else dry_run_param
        if purchases_day and plain_run and not dry:
            _pull_purchases(purchases.MAX_DAYS, scheduled=True)
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
    over up to N lots and hand each lot to buttonmatcher (yolo_test.py), which
    runs it as an automatic Scout lot in #inventory-bot.

    Seen-aware: lots already run are skipped (except the very first on-demand2
    run, which may include seen lots to reach the cap), and a lot is marked seen
    once buttonmatcher has accepted it — so nothing is lost or double-run.

    Usage: `/crawl <N>` (e.g. `/crawl 800`), N in 1..CRAWL_MAX_LOTS_CAP. Slack
    requires an ack within 3s, so we validate + ack immediately and kick the
    heavy run via /internal/crawl through the load balancer (CPU-funded request).
    """
    raw = (body.get("text") or "").strip()
    cap = config.CRAWL_MAX_LOTS_CAP
    is_purchases, days, purchases_err = purchases.parse_command(raw)
    if is_purchases:
        _start_purchases_pull(ack, days, purchases_err)
        return
    if shipping_backfill.parse_command(raw):
        _start_shipping_backfill(ack)
        return
    is_seller, seller = seller_listings.parse_command(raw)
    if is_seller:
        _start_seller_pull(ack, seller)
        return
    if re.match(r"(?i)^\s*catch-?up\b", raw):
        # Retired 2026-10-25 with the Gemini pipeline: it fed still-listed lots
        # to the Gem so their price rows got written.
        ack("`/crawl catchup` is retired with the Gemini pipeline, which it fed. "
            "Lots handed to buttonmatcher write their own price rows.")
        return
    try:
        n = int(raw)
    except ValueError:
        ack(f"Usage: `/crawl <N>` — N = how many lots to search (1–{cap}). "
            f"Example: `/crawl 800`.  Or `/crawl seller <username>` to pull one "
            f"seller's Penn State prices into the Logger sheet, "
            f"or `/crawl purchases` to pull your own last 90 days of eBay "
            f"purchases into it.")
        return
    if n < 1 or n > cap:
        ack(f"`/crawl {raw}` is out of range — pick N between 1 and {cap}. "
            f"(This launches a paid eBay + CLIP run, so the cap guards a typo.)")
        return

    ack(f"🔎 Starting `/crawl {n}` — searching up to {n} lots; runs in the "
        f"background and hands each lot to buttonmatcher, which runs it as a "
        f"Scout lot in #inventory-bot.")

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


def _start_seller_pull(ack, seller) -> None:
    """`/crawl seller <username>`: ack now, pull inside /internal/seller.

    A few Browse pages and one sheet write — no photos, no Gemini, no CLIP — so
    it needs no cap and no confirmation.  It still runs inside its own request
    through the load balancer, like /crawl, so it never depends on CPU after
    the ack.
    """
    if not seller:
        ack("Usage: `/crawl seller <username>` — e.g. `/crawl seller kling24toys`.")
        return
    ack(f"📋 Pulling *{seller}*'s Penn State listings into the "
        f"`{seller_listings.tab_name(seller)}` tab of the Logger sheet, source "
        f"`seller` (prices only: no photos, no Gemini).")

    def _kick():
        base    = _SERVICE_URL if _SERVICE_URL else "http://localhost:8080"
        headers = {"X-Internal-Secret": _INTERNAL_SECRET}
        try:
            requests.post(f"{base}/internal/seller", params={"seller": seller},
                          headers=headers, timeout=600)
        except Exception as exc:
            print(f"!!! SELLER: internal kick failed: {exc}", flush=True)
            try:
                notifier.send_warning(_slack_token, _channel_id,
                                      f"seller pull for {seller} failed to start: {exc}")
            except Exception:
                pass

    threading.Thread(target=_kick, daemon=True).start()


def _start_purchases_pull(ack, days, error) -> None:
    """`/crawl purchases [days]`: ack now, pull inside /internal/purchases.

    A few Trading API pages and one sheet write, so it needs no cap and no
    confirmation.  Like the seller pull, it runs inside its own request through
    the load balancer, so it never depends on CPU after the ack.
    """
    if error:
        ack(error)
        return
    ack(f"🧾 Pulling your eBay purchases from the last {days} days into the "
        f"`{purchases.TAB}` tab of the Logger sheet (merged with what's there).")

    def _kick():
        base    = _SERVICE_URL if _SERVICE_URL else "http://localhost:8080"
        headers = {"X-Internal-Secret": _INTERNAL_SECRET}
        try:
            requests.post(f"{base}/internal/purchases", params={"days": days},
                          headers=headers, timeout=600)
        except Exception as exc:
            print(f"!!! PURCHASES: internal kick failed: {exc}", flush=True)
            try:
                notifier.send_warning(_slack_token, _channel_id,
                                      f"purchases pull failed to start: {exc}")
            except Exception:
                pass

    threading.Thread(target=_kick, daemon=True).start()


def _start_shipping_backfill(ack) -> None:
    """`/crawl shipping`: ack now, fill inside /internal/shipping.

    One Trading API GetItem per item number whose price_log listing rows have
    no shipping, then one batch of cell writes (shipping_backfill.py).
    """
    ack("🚚 Filling in shipping for `price_log` listing rows that have none — one "
        "eBay lookup per item, a few minutes. A summary posts here when it's done.")

    def _kick():
        base    = _SERVICE_URL if _SERVICE_URL else "http://localhost:8080"
        headers = {"X-Internal-Secret": _INTERNAL_SECRET}
        try:
            requests.post(f"{base}/internal/shipping", headers=headers, timeout=3500)
        except Exception as exc:
            print(f"!!! SHIPPING: internal kick failed: {exc}", flush=True)
            try:
                notifier.send_warning(_slack_token, _channel_id,
                                      f"/crawl shipping failed to start: {exc}")
            except Exception:
                pass

    threading.Thread(target=_kick, daemon=True).start()


@flask_app.route("/internal/seller", methods=["POST"])
def internal_seller():
    """Pull one seller's Penn State listings into price_log in the Logger
    workbook (seller_listings.py), synchronously in this request.
    Auth: per-startup X-Internal-Secret header."""
    if not _internal_request_ok(request):
        return jsonify({"status": "forbidden"}), 403
    _, seller = seller_listings.parse_command("seller " + (request.args.get("seller") or ""))
    if not seller:
        return jsonify({"status": "bad seller"}), 400

    from . import ebay_client
    tab, rows, error = seller_listings.tab_name(seller), [], None
    try:
        items = ebay_client.find_seller_listings(
            _get_secret("EBAY_APP_ID"), _get_secret("EBAY_CERT_ID"), seller)
        pulled_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        rows = [r for r in (seller_listings.summary_row(i, pulled_at, seller) for i in items) if r]
        gclient = sheets_client.get_gspread_client(_get_secret("GOOGLE_SHEETS_JSON"))
        tab = seller_listings.write_tab(gclient, _get_secret("LOGGER_ID"), seller, rows)
    except Exception as exc:
        traceback.print_exc()
        error = f"{type(exc).__name__}: {exc}"
    try:
        notifier.send_text(_slack_token, _channel_id,
                           seller_listings.summary_text(seller, len(rows), tab, error))
    except Exception as exc:
        print(f"!!! SELLER: Slack summary failed: {exc}", flush=True)
    if error:
        return jsonify({"status": "failed", "seller": seller, "error": error}), 500
    return jsonify({"status": "ok", "seller": seller, "listings": len(rows), "tab": tab}), 200


@flask_app.route("/internal/shipping", methods=["POST"])
def internal_shipping():
    """Fill the blank shipping of price_log listing rows from eBay
    (shipping_backfill.py), synchronously in this request.
    Auth: per-startup X-Internal-Secret header."""
    if not _internal_request_ok(request):
        return jsonify({"status": "forbidden"}), 403
    result = _backfill_shipping()
    return jsonify(result), (500 if result["status"] == "failed" else 200)


def _backfill_shipping() -> dict:
    """Ask eBay for each blank item's shipping, write it, post the Slack line.
    Never raises."""
    from . import ebay_client
    sb = shipping_backfill
    items = filled_items = filled_rows = no_quote = unavailable = 0
    error = None
    try:
        token = _get_secret("EBAY_USER_TOKEN")
        gclient = sheets_client.get_gspread_client(_get_secret("GOOGLE_SHEETS_JSON"))
        ws = price_log.open_price_sheet(gclient, _get_secret("LOGGER_ID"))
        if ws is None:
            raise RuntimeError("price_log could not be opened")
        c = price_log.col
        bd, ship = ws.batch_get([f"{c('kind')}:{c('ebay_id')}",
                                 f"{c('shipping')}:{c('shipping')}"])
        blanks = sb.blank_listing_rows(bd, ship)
        items = len(blanks)
        costs = {}
        for n, eid in enumerate(blanks, 1):
            try:
                cost = ebay_client.get_item_shipping(token, eid)
            except sb.ItemUnavailable as exc:
                if "token" in str(exc).lower():
                    raise                     # a bad token fails every item: stop
                unavailable += 1
                continue
            except Exception as exc:
                print(f"!!! SHIPPING: {eid}: {type(exc).__name__}: {exc}", flush=True)
                unavailable += 1
                continue
            if cost is None:
                no_quote += 1
            else:
                costs[eid] = cost
            if n % 100 == 0:
                print(f">>> SHIPPING: {n}/{items} items asked", flush=True)
        updates = sb.cells(blanks, costs)
        for i in range(0, len(updates), sb.WRITE_CHUNK):
            ws.batch_update(updates[i:i + sb.WRITE_CHUNK], value_input_option="RAW")
        filled_items, filled_rows = len(costs), len(updates)
    except Exception as exc:
        traceback.print_exc()
        error = f"{type(exc).__name__}: {exc}"
    print(f">>> SHIPPING: items={items} filled_items={filled_items} rows={filled_rows} "
          f"no_quote={no_quote} unavailable={unavailable} error={error}", flush=True)
    try:
        notifier.send_text(_slack_token, _channel_id, sb.summary_text(
            items, filled_items, filled_rows, no_quote, unavailable, error))
    except Exception as exc:
        print(f"!!! SHIPPING: Slack summary failed: {exc}", flush=True)
    if error:
        return {"status": "failed", "error": error}
    return {"status": "ok", "items": items, "filled_items": filled_items,
            "filled_rows": filled_rows, "no_quote": no_quote, "unavailable": unavailable}


@flask_app.route("/internal/purchases", methods=["POST"])
def internal_purchases():
    """Pull the operator's own eBay purchases into the `purchases` tab of the
    Logger workbook (purchases.py), synchronously in this request.
    Auth: per-startup X-Internal-Secret header."""
    if not _internal_request_ok(request):
        return jsonify({"status": "forbidden"}), 403
    _, days, bad = purchases.parse_command("purchases " + (request.args.get("days") or ""))
    if bad:
        return jsonify({"status": "bad days"}), 400
    result = _pull_purchases(days)
    return jsonify(result), (500 if result["status"] == "failed" else 200)


def _pull_purchases(days, scheduled=False) -> dict:
    """Pull ``days`` of purchases, merge them into the Logger tab, and post the
    Slack line.  Never raises: a failed pull must not fail the scan that calls
    it on Mondays.

    ``scheduled`` is the Monday run inside /run-scan.  Before EBAY_USER_TOKEN
    exists it stays quiet (a log line, no Slack), so the weekly scan doesn't
    nag about a feature that isn't set up yet.  Any other failure, such as an
    expired token, still posts.
    """
    from . import ebay_client
    pulled, added, total, error = 0, 0, 0, None
    try:
        try:
            token = _get_secret("EBAY_USER_TOKEN")
        except Exception as exc:
            if scheduled:
                print(">>> PURCHASES: EBAY_USER_TOKEN not set — Monday pull skipped.",
                      flush=True)
                return {"status": "skipped", "reason": "no EBAY_USER_TOKEN"}
            raise RuntimeError("the EBAY_USER_TOKEN secret is not set up yet "
                               "(DEPLOY.md, \"EBAY_USER_TOKEN\")") from exc
        lines = ebay_client.get_purchases(token, days)
        pulled_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        rows = [purchases.row(line, pulled_at) for line in lines]
        pulled = len(rows)
        gclient = sheets_client.get_gspread_client(_get_secret("GOOGLE_SHEETS_JSON"))
        _, added, total = purchases.write_tab(gclient, _get_secret("LOGGER_ID"), rows)
    except Exception as exc:
        traceback.print_exc()
        error = f"{type(exc).__name__}: {exc}"
    try:
        notifier.send_text(_slack_token, _channel_id,
                           purchases.summary_text(days, pulled, added, total, error))
    except Exception as exc:
        print(f"!!! PURCHASES: Slack summary failed: {exc}", flush=True)
    if error:
        return {"status": "failed", "error": error}
    return {"status": "ok", "line_items": pulled, "added": added, "total": total}


@flask_app.route("/internal/crawl", methods=["POST"])
def internal_crawl():
    """Run the on-demand2 N-lot search synchronously in this request so Cloud Run
    keeps CPU allocated for the whole run. N comes from ?n= (clamped to
    1..CRAWL_MAX_LOTS_CAP). Auth: per-startup X-Internal-Secret header.
    Mirrors buttonmatcher /internal/match."""
    if not _internal_request_ok(request):
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
# Seen marks
# ---------------------------------------------------------------------------
# The Gemini pipeline was removed 2026-10-25 (operator: the pipeline is dead,
# the watchers are off): its routes (/pipeline/notify, which took the watcher's
# shared secret from the internet, /internal/pipeline, /internal/pipelinetest),
# process_pipeline_lot and the detector it ran.  Lots go to buttonmatcher
# (yolo_test.py).


def _mark_item_seen_now(item_id: str) -> None:
    """Mark one item seen the moment buttonmatcher accepts it: load -> add ->
    save under a lock so concurrent writers can't clobber each other.
    seen_items.json on GCS is the source of truth; volume is low (one lot at a
    time), so a full read-modify-write per lot is cheap."""
    try:
        with _seen_lock:
            seen = seen_items.load_seen()
            seen_items.mark_seen(item_id, seen)
            seen_items.save_seen(seen)
    except Exception as exc:
        print(f"!!! SEEN: mark_seen({item_id}) failed: {exc}", flush=True)


def _flush_seen_marks(marks: list[str], flushed: int) -> tuple[int, bool]:
    """Persist the seen-marks a scan has made since ``flushed``, without
    clobbering anyone else's.

    The legacy CLIP scan used to load `seen` once at the top and later upload the
    whole dict back.  A lot marked seen during that window through
    _mark_item_seen_now (then by a Gemini pipeline result) was erased by the
    scan's wholesale save, which wrote a snapshot that predates it.  The lot is
    re-fed and re-alerted on the next run, which is the failure the store
    exists to stop.

    So both writers now do the same thing: reload from GCS, apply only the marks
    they own, save — all under _seen_lock, which is what makes the read and the
    write one operation.  This is not the fast path (the general scan runs at
    most a few times a day and checkpoints every 50 listings), and correctness
    here is worth a download.

    Returns (new flushed index, ok).  On failure the index does not advance, so
    the marks ride along with the next checkpoint rather than being lost.
    """
    pending = marks[flushed:]
    if not pending:
        return flushed, True
    try:
        with _seen_lock:
            seen = seen_items.load_seen()
            for _iid in pending:
                seen_items.mark_seen(_iid, seen)
            ok = seen_items.save_seen(seen)
    except Exception as exc:
        print(f"!!! SCAN: seen checkpoint of {len(pending)} mark(s) failed: {exc}",
              flush=True)
        return flushed, False
    return (len(marks) if ok else flushed), ok


@flask_app.route("/test-clip", methods=["GET"])
def test_clip():
    """
    Debug endpoint: download one image, run detect+match, return raw per-crop
    scores (threshold=0.0 so every crop reports its actual score).

    Usage — pass either an eBay item ID or a direct image URL, with the
    watcher's shared secret (it checked nothing until 2026-09-26, so anyone
    could make the service fetch any URL and run CLIP on it):
      curl -H "X-Pipeline-Secret: $SECRET" "https://<service>/test-clip?item_id=v1|318369928679|0"
      curl -H "X-Pipeline-Secret: $SECRET" "https://<service>/test-clip?url=<image_url>"
    """
    if not _operator_request_ok(request):
        return jsonify({"status": "forbidden"}), 403
    item_id   = request.args.get("item_id")
    image_url = request.args.get("url")
    if not item_id and not image_url:
        return jsonify({"error": "pass ?item_id=<ebay_id> or ?url=<image_url>"}), 400

    # Ensure CLIP is loaded
    if not _ensure_clip_loaded():
        return jsonify({"error": "CLIP init failed"}), 500

    try:
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

        image_bytes = _ip.download_image(image_url, timeout=20)

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
    buttons_detected: int | None = None,
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

    `buttons_detected` is passed by the Gemini pipeline, where `top_matches` is
    only the auto-confirmed crops. It adds `buttons_detected`,
    `price_per_button` (asking / buttons detected) and `buttons` (every
    confirmed button with its count), which the market report averages per
    button (scan_log.button_price_fields).
    """
    title = listing.get("title", "")
    # Year composition of the lot, from each crop's best match.
    year_counts: dict[str, int] = dict(Counter(
        str(m["year"]) for m in top_matches if m.get("year") is not None
    ))
    record = {
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
        "shipping":      listing.get("shipping_cost"),
        "photos_scored": photos_processed,
        "crops_scored":  len(top_matches),          # matched buttons (pipeline: confirmed only)
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
    if buttons_detected is not None:
        record.update(scan_log.button_price_fields(
            record["asking"], buttons_detected, top_matches))
    return record


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


def _lookalike_flag_enabled():
    """Look-alike value flag on deal alerts is on by default;
    EBAYSCOUT_LOOKALIKE_FLAG=0 removes the extra line without a redeploy."""
    return os.environ.get("EBAYSCOUT_LOOKALIKE_FLAG", "1").strip() not in (
        "0", "false", "False", "",
    )


def _lookalike_note(slogan, year, buy_rules) -> dict | None:
    """Curated look-alike warning for a match about to be alerted on, or None.

    Fails OPEN in every failure mode: a missing registry, an unknown slogan or
    any exception returns None and the alert posts exactly as it does today.
    """
    if not _lookalike_flag_enabled() or not CONFUSABLE_REGISTRY:
        return None
    try:
        family = cfs.confusable_family(
            CONFUSABLE_REGISTRY, slogan, normalize.normalize_key)
        if not family:
            return None

        def _priced(y, s):
            price, _, _, amt = sheets_client.get_buy_decision(y, s, buy_rules)
            return sheets_client.parse_price(price), amt

        return pipeline_classify.lookalike_note(
            slogan, year, family, _priced, normalize.normalize_key)
    except Exception as exc:
        print(f"!!! LOOKALIKE: flag failed (alert posts unflagged): {exc}",
              flush=True)
        return None


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
    # Both alert paths (pipeline and the CLIP _evaluate_listing scan) build
    # their needed_buttons through here, so flagging once covers both.
    enriched["lookalike"] = _lookalike_note(
        top.get("slogan"), top.get("year"), buy_rules)
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
    mode    = "crawl500" if command in ("/crawl", "/crawl500", "daily") else "scan"
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
            # Detection-tuning instrumentation (Hough params + rejected radii)
            expected_radius=det_diag.get("expected_radius"),
            hough_dp=det_diag.get("hough_dp"),
            hough_mindist=det_diag.get("hough_mindist"),
            hough_param1=det_diag.get("hough_param1"),
            hough_param2=det_diag.get("hough_param2"),
            hough_minradius=det_diag.get("hough_minradius"),
            hough_maxradius=det_diag.get("hough_maxradius"),
            rej_radius_min=det_diag.get("rej_radius_min"),
            rej_radius_median=det_diag.get("rej_radius_median"),
            rej_radius_max=det_diag.get("rej_radius_max"),
            mask_blobs_raw=det_diag.get("mask_blobs_raw"),
            dt_peaks_total=det_diag.get("dt_peaks_total"),
            mask_coverage=det_diag.get("mask_coverage"),
            satfb_blue_cov=det_diag.get("satfb_blue_cov"),
            satfb_bright_cov=det_diag.get("satfb_bright_cov"),
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

            # Edition-twin guard (edition_twins.py): the winning slogan is a
            # registered multi-edition family (e.g. "Crush the Orange" exists as
            # BOTH 1972 and 1973 football) — text-agreement auto-confirm has no
            # signal for WHICH edition, so a twin-family winner must not silently
            # auto-confirm. Demote to the existing human-review (yellow) lane
            # instead. The slogan identification is still trusted; only the
            # edition choice needs the human. Fail-open: any exception here
            # leaves _cm_confirmed (today's auto-confirm behavior) unchanged.
            if _cm_confirmed and _twin_guard_enabled():
                try:
                    _twin_fam = edt.twin_family(TWIN_REGISTRY, top["slogan"], normalize.normalize_key)
                    if _twin_fam:
                        print(f">>> TWIN GUARD: '{top['slogan']}' has "
                              f"{len(_twin_fam)} editions — auto-confirm "
                              f"demoted to edition picker", flush=True)
                        _cm_confirmed = False
                except Exception as _twin_e:
                    print(f"!!! TWIN GUARD check failed (failing open): "
                          f"{_twin_e}", flush=True)

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
    # The ids THIS scan marked, in order, and how many of them are already on
    # GCS.  The local `seen` dict above stays the in-run dedup view; it is no
    # longer what gets uploaded.  See _flush_seen_marks for why.
    _marked_here: list[str] = []
    _seen_flushed = 0

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
        _marked_here.append(item_id)
        _listings_since_save += 1
        if _listings_since_save >= 50:
            if not dry_run:
                _seen_flushed, _ = _flush_seen_marks(_marked_here, _seen_flushed)
            # Checkpoint the scan-log data every 50 too (both modes) so a 30-min
            # timeout on a big run never loses the records collected so far.
            pending = scan_log_records[_scanlog_flushed:]
            if pending and seen_store.append_scan_log(pending):
                _scanlog_flushed = len(scan_log_records)
            _listings_since_save = 0

    if dry_run:
        print("[DRY RUN] Skipping save_seen().", flush=True)
    else:
        _seen_flushed, _seen_ok = _flush_seen_marks(_marked_here, _seen_flushed)
        if not _seen_ok:
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


def _lot_ctx(listing: dict, key: str, command: str) -> dict:
    """A lot's listing context, as the handoff to buttonmatcher keeps it."""
    item_id = listing["item_id"]
    title   = listing.get("title", "?")
    seller  = listing.get("seller", "")
    return {
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
        # The listing's format and bids, for the scan_log row and price_log:
        # an auction's price when scanned is a bid, not an asking price.
        "buying_options": listing.get("buying_options") or [],
        "bid_count":      listing.get("bid_count"),
        "shipping":       listing.get("shipping_cost"),
        "command":      command,
        "created":      datetime.datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# The handoff (yolo_test.py, from the YOLO week): each lot handed to
# buttonmatcher, which runs its full process on it as an automatic Scout lot.
# ---------------------------------------------------------------------------

def _handoff_lot_to_buttonmatcher(listing: dict, ebay_app_id: str,
                                  ebay_cert_id: str, command: str) -> bool:
    """POST one lot to buttonmatcher's /internal/ebay_scout_lot; mark it seen
    once buttonmatcher has accepted it (200).  False when the lot has no photo
    or buttonmatcher refused it (it stays unseen and comes back next run)."""
    from . import ebay_client
    item_id = listing["item_id"]
    picture_urls = ebay_client.get_item_pictures(ebay_app_id, ebay_cert_id, item_id)
    if not picture_urls and listing.get("gallery_url"):
        picture_urls = [listing["gallery_url"]]
    if not picture_urls:
        print(f">>> YOLO_TEST: {item_id} has no photos — skipping.", flush=True)
        return False
    ctx = _lot_ctx(listing, uuid.uuid4().hex[:12], command)
    resp = requests.post(
        yolo_test.buttonmatcher_url() + yolo_test.ENDPOINT,
        json=yolo_test.handoff_payload(ctx, picture_urls[0]),
        headers={"X-Pipeline-Secret": _PIPELINE_SHARED_SECRET}, timeout=90)
    if resp.status_code != 200:
        print(f"!!! YOLO_TEST: buttonmatcher refused {item_id}: "
              f"{resp.status_code} {resp.text[:200]}", flush=True)
        return False
    _mark_item_seen_now(item_id)
    print(f">>> YOLO_TEST: {item_id} handed to buttonmatcher "
          f"({resp.text[:80]})", flush=True)
    return True


def _collect_ebay_listings(ebay_app_id, ebay_cert_id, max_pages: int = 1):
    """The daily eBay pull (used by the daily CLIP scan and the daily handoff
    feed — NOT by /crawl, which has its own search): the general queries
    (EBAY_SEARCH_QUERIES) plus the PSU queries restricted to Sports-Mem (drops
    Power-Supply-Unit noise), with the standard seller/keyword/category
    safeguards from config. Returns a combined (not-yet-deduped) list; callers
    dedup.  ``max_pages`` reads deeper than the newest window (catch-up only;
    the daily scan reads one page)."""
    from . import ebay_client       # lazy import — matches the other callers
    out = ebay_client.find_all_listings(
        client_id=ebay_app_id, client_secret=ebay_cert_id,
        queries=config.EBAY_SEARCH_QUERIES,
        excluded_sellers=config.EXCLUDED_SELLERS,
        max_results=config.EBAY_MAX_RESULTS,
        max_pages=max_pages,
    )
    out += ebay_client.find_all_listings(
        client_id=ebay_app_id, client_secret=ebay_cert_id,
        queries=config.PSU_SEARCH_QUERIES,
        excluded_sellers=config.EXCLUDED_SELLERS,
        max_results=config.EBAY_MAX_RESULTS,
        category_ids=config.SPORTS_MEMO_CATEGORY_ID,
        max_pages=max_pages,
    )
    return out


def _run_crawl(n: int, source: str = "/crawl", ignore_seen: bool = False,
               dry_run: bool = False) -> int:
    """Hand an eBay pull to buttonmatcher, capped at N lots; each lot runs there
    as an automatic Scout lot (detection, CLIP + OCR, the Scout valuation).

    The daily scan and /crawl keep SEPARATE searches (they serve different
    purposes), selected by ``source``:
      - "daily"  → the daily pull (_collect_ebay_listings: EBAY_SEARCH_QUERIES +
                   PSU restricted to Sports-Mem, with config safeguards).
      - "/crawl" → the fixed on-demand search (CRAWL500_QUERIES, OR-expanded),
                   with the SAME eBay safeguards as the daily scan (excluded
                   sellers + keywords + categories, all from shared config).
    ``source`` also tags the lots and the Slack summary. ``ignore_seen`` feeds
    every fetched lot regardless of seen_items; ``dry_run`` reports the count
    without feeding or mutating state.

    SEEN-aware: each lot is POSTed to buttonmatcher (_handoff_lot_to_buttonmatcher)
    and marked seen once buttonmatcher accepts it, so a lot it refused is retried,
    never lost.  (The Gemini pipeline this fed until 2026-10-25 is gone.)  First
    run (per the GCS marker) — or ignore_seen — feeds already-seen lots too;
    every run after feeds only unseen.  Returns the number of lots fed (or that
    would be fed, for dry_run).
    """
    from . import ebay_client, seen_items as seen_store

    try:
        ebay_app_id  = _get_secret("EBAY_APP_ID")
        ebay_cert_id = _get_secret("EBAY_CERT_ID")
    except Exception as exc:
        print(f"!!! FEED[{source}]: eBay credentials unavailable — aborting: {exc}", flush=True)
        notifier.send_warning(_slack_token, _channel_id, f"{source}: no eBay credentials.")
        return 0

    first_run = not seen_store.ondemand2_first_run_done()
    feed_all = first_run or ignore_seen
    print(f">>> FEED[{source}]: starting (n={n}, first_run={first_run}, "
          f"ignore_seen={ignore_seen}, dry_run={dry_run}).", flush=True)

    try:
        if source == "daily":
            all_listings = _collect_ebay_listings(ebay_app_id, ebay_cert_id)
        else:
            # /crawl: its own fixed on-demand search — deliberately separate from
            # the daily queries. OR-expansion → up to CRAWL_MAX_PAGES newest-first
            # pages of 200 per (bank x type), with the same eBay safeguards as
            # the daily/auto scan (excluded sellers + keywords; category 11450
            # dropped inside find_listings via config.EXCLUDED_CATEGORY_IDS
            # regardless).
            all_listings = ebay_client.find_all_listings(
                client_id=ebay_app_id, client_secret=ebay_cert_id,
                queries=config.CRAWL500_QUERIES,
                excluded_sellers=config.EXCLUDED_SELLERS,
                excluded_keywords=config.EXCLUDED_KEYWORDS,
                max_results=200,
                max_pages=config.CRAWL_MAX_PAGES,
            )
    except Exception as exc:
        print(f"!!! FEED[{source}]: search failed: {exc}", flush=True)
        notifier.send_warning(_slack_token, _channel_id, f"{source} search failed: {exc}")
        return 0

    all_listings = dedup_listings(all_listings)
    seen = seen_store.load_seen()

    # Skip lots already run (in `seen`) unless feed_all. `seen` is marked once
    # buttonmatcher accepts a lot (_handoff_lot_to_buttonmatcher), so a lot it
    # refused stays re-feedable. /crawl uses a double-seen rule for pre-cutoff items
    # (early pipeline era); daily always skips after one seen.
    if feed_all:
        candidate = all_listings
    elif source == "daily":
        candidate = [l for l in all_listings if seen_store.is_new(l["item_id"], seen)]
    else:
        candidate = [l for l in all_listings if seen_store.is_crawl_unseen(l["item_id"], seen)]
    new_listings = candidate[: n]
    print(f">>> FEED[{source}]: {len(all_listings)} unique found; "
          f"{'WOULD feed' if dry_run else 'feeding'} {len(new_listings)} (cap {n}).",
          flush=True)

    if not new_listings:
        notifier.send_warning(_slack_token, _channel_id,
                              f"{source}: no new lots to feed this run.")
        if first_run and not dry_run:
            seen_store.mark_ondemand2_first_run_done()
        return 0

    if dry_run:
        notifier.send_warning(
            _slack_token, _channel_id,
            f"🔎 `{source}` dry run: would feed {len(new_listings)} lot(s) "
            f"(cap {n}). Nothing fed, nothing marked seen.")
        return len(new_listings)

    fed = 0
    for listing in new_listings:
        item_id = listing["item_id"]
        seller  = listing.get("seller", "")
        try:
            if _handoff_lot_to_buttonmatcher(listing, ebay_app_id, ebay_cert_id,
                                             source):
                fed += 1
        except Exception as exc:
            print(f"!!! FEED[{source}]: feed failed for {item_id} [{seller}]: {exc}", flush=True)
            traceback.print_exc()

    if first_run:
        seen_store.mark_ondemand2_first_run_done()

    notifier.send_warning(
        _slack_token, _channel_id,
        f"🔎 `{source}`: handed {fed}/{len(new_listings)} lot(s) to buttonmatcher"
        f"{' (first run — included already-seen lots)' if feed_all else ''}. "
        f"Each runs as an automatic Scout lot in #inventory-bot.")
    print(f">>> FEED[{source}]: handed off={fed}/{len(new_listings)}.", flush=True)
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
        # A Logger row lost for good after the quota retries is posted to the
        # scout channel, the one place the operator reads (match_logging.py).
        _loss_alert = lambda text, attachment=None: notifier.send_logger_loss(
            _slack_token, _channel_id, text, attachment)
        match_logger  = mlog.SheetLogger(match_ws, confirm_ws, service="ebayscout",
                                         on_failure=_loss_alert)
    except Exception as exc:
        print(f"!!! STARTUP: match-logging init failed (logging disabled): {exc}", flush=True)
        match_logger = mlog.SheetLogger(None, None, service="ebayscout")

    # Hydrate CLIP in the background.  On a cold, CPU-throttled container this
    # may not finish until an HTTP request (a scan) provides CPU — those paths
    # call _ensure_clip_loaded() to force it.
    threading.Thread(target=_ensure_clip_loaded, daemon=True).start()


