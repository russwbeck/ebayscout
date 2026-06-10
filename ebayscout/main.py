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
from . import gemini_triage
from . import match_logging as mlog
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

# Slack Bolt app — serves the /crawl500 slash command via /slack/events.
app     = App(token=_slack_token, signing_secret=_signing_secret)
handler = SlackRequestHandler(app)

# External URL the slash handler uses to invoke /internal/crawl500 through the
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
# Slack slash command: /crawl500 (on-demand2 search)
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


@app.command("/crawl500")
def handle_crawl500_command(ack, body):
    """on-demand2: search the fixed Citizens/Mellon/Central-Counties button query
    (max 500 lots, no seller exclusion) and log every crop for automation.

    Slack requires an ack within 3s, so we ack immediately and kick the heavy run
    via /internal/crawl500 through the load balancer (fresh CPU-funded request).
    """
    ack("🔎 Starting `/crawl500` — searching up to 500 lots, this runs in the "
        "background and will post results to the channel.")

    def _kick():
        base    = _SERVICE_URL if _SERVICE_URL else "http://localhost:8080"
        headers = {"X-Internal-Secret": _INTERNAL_SECRET}
        try:
            # Long read timeout: the run executes synchronously inside this call
            # so CPU stays allocated for its whole duration.  3500s leaves 100s
            # headroom against Cloud Run's 3600s max request timeout.
            requests.post(f"{base}/internal/crawl500", headers=headers, timeout=3500)
        except Exception as exc:
            print(f"!!! CRAWL500: internal kick failed: {exc}", flush=True)
            try:
                notifier.send_warning(_slack_token, _channel_id,
                                      f"/crawl500 failed to start: {exc}")
            except Exception:
                pass

    threading.Thread(target=_kick, daemon=True).start()


def _is_localhost(remote_addr: str | None) -> bool:
    try:
        return ipaddress.ip_address(remote_addr or "").is_loopback
    except ValueError:
        return False


@flask_app.route("/internal/crawl500", methods=["POST"])
def internal_crawl500():
    """Run the on-demand2 500-lot search synchronously in this request so Cloud
    Run keeps CPU allocated for the whole run. Auth: per-startup X-Internal-Secret
    header, or a localhost caller (local dev). Mirrors buttonmatcher /internal/match."""
    provided     = request.headers.get("X-Internal-Secret", "")
    from_localhost = _is_localhost(request.remote_addr)
    if provided != _INTERNAL_SECRET and not from_localhost:
        return jsonify({"status": "forbidden"}), 403

    if not vectors_loaded and not _ensure_clip_loaded():
        return jsonify({"status": "clip init failed"}), 500

    global buy_rules
    if not buy_rules:
        try:
            buy_rules = sheets_client.load_buy_rules(
                _get_secret("GOOGLE_SHEETS_JSON"), _get_secret("SPREADSHEET_ID"))
        except Exception as exc:
            print(f"!!! CRAWL500: buy_rules reload failed: {exc}", flush=True)

    if not _scan_lock.acquire(blocking=False):
        return jsonify({"status": "already running"}), 409
    try:
        processed = _run_crawl500()
    finally:
        _scan_lock.release()
    return jsonify({"status": "crawl500 complete", "processed": processed}), 200


@app.command("/crawl10")
def handle_crawl10_command(ack, body):
    """Small (10-lot) test crawl over the fixed "Penn State bank button" search.

    Adds a Gemini Flash triage pass per lot on top of the normal green/auto
    gate (see _run_crawl10): logs Gemini's button-count estimate and
    auto-resolves yellow candidates whose slogan Gemini also detected.

    Slack requires an ack within 3s, so we ack immediately and kick the run
    via /internal/crawl10 through the load balancer (fresh CPU-funded request).
    """
    ack("🔎 Starting `/crawl10` — searching up to 10 lots with Gemini triage, "
        "this runs in the background and will post results to the channel.")

    def _kick():
        base    = _SERVICE_URL if _SERVICE_URL else "http://localhost:8080"
        headers = {"X-Internal-Secret": _INTERNAL_SECRET}
        try:
            requests.post(f"{base}/internal/crawl10", headers=headers, timeout=3500)
        except Exception as exc:
            print(f"!!! CRAWL10: internal kick failed: {exc}", flush=True)
            try:
                notifier.send_warning(_slack_token, _channel_id,
                                      f"/crawl10 failed to start: {exc}")
            except Exception:
                pass

    threading.Thread(target=_kick, daemon=True).start()


@flask_app.route("/internal/crawl10", methods=["POST"])
def internal_crawl10():
    """Run the /crawl10 search synchronously in this request so Cloud Run keeps
    CPU allocated for the whole run. Auth: per-startup X-Internal-Secret header,
    or a localhost caller (local dev). Mirrors /internal/crawl500."""
    provided     = request.headers.get("X-Internal-Secret", "")
    from_localhost = _is_localhost(request.remote_addr)
    if provided != _INTERNAL_SECRET and not from_localhost:
        return jsonify({"status": "forbidden"}), 403

    if not vectors_loaded and not _ensure_clip_loaded():
        return jsonify({"status": "clip init failed"}), 500

    global buy_rules
    if not buy_rules:
        try:
            buy_rules = sheets_client.load_buy_rules(
                _get_secret("GOOGLE_SHEETS_JSON"), _get_secret("SPREADSHEET_ID"))
        except Exception as exc:
            print(f"!!! CRAWL10: buy_rules reload failed: {exc}", flush=True)

    try:
        gemini_api_key = _get_secret("GEMINI_API")
    except Exception as exc:
        print(f"!!! CRAWL10: GEMINI_API secret unavailable — aborting: {exc}", flush=True)
        notifier.send_warning(_slack_token, _channel_id, "/crawl10: no GEMINI_API secret.")
        return jsonify({"status": "no gemini secret"}), 500

    if not _scan_lock.acquire(blocking=False):
        return jsonify({"status": "already running"}), 409
    try:
        processed = _run_crawl10(gemini_api_key)
    finally:
        _scan_lock.release()
    return jsonify({"status": "crawl10 complete", "processed": processed}), 200


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
    mode    = "crawl500" if command == "/crawl500" else "scan"
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
                        confirmed_buttons: list, command: str,
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
        count_elements = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": label},
                "action_id": f"scout_count_{label.replace('+', 'plus')}",
                "value": json.dumps({"job_id": job_id, "item_id": item_id,
                                     "bucket": label, "command": command}),
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
                "command":  command,
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
        command  = val.get("command", "/crawl500")
        source   = "human_verify_yes" if verified else "human_verify_no"

        rec = mlog.build_confirm_record(
            service="ebayscout", command=command,
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
        command = val.get("command", "/crawl500")

        # Log as a synthetic confirm_log row so it's queryable alongside
        # det_count_noinput.  chosen_phrase carries the bucket string;
        # source='user_count' identifies the row type.
        rec = mlog.build_confirm_record(
            service="ebayscout", command=command,
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
                ebay_listings = ebay_client.find_all_listings(
                    client_id=ebay_app_id,
                    client_secret=ebay_cert_id,
                    queries=config.EBAY_SEARCH_QUERIES,
                    excluded_sellers=config.EXCLUDED_SELLERS,
                    max_results=config.EBAY_MAX_RESULTS,
                )
                all_listings.extend(ebay_listings)
                # PSU queries restricted to Sports Memorabilia to avoid electronics
                psu_listings = ebay_client.find_all_listings(
                    client_id=ebay_app_id,
                    client_secret=ebay_cert_id,
                    queries=config.PSU_SEARCH_QUERIES,
                    excluded_sellers=config.EXCLUDED_SELLERS,
                    max_results=config.EBAY_MAX_RESULTS,
                    category_ids=config.SPORTS_MEMO_CATEGORY_ID,
                )
                all_listings.extend(psu_listings)
                print(f">>> SCAN: eBay returned {len(ebay_listings) + len(psu_listings)} listings "
                      f"({len(ebay_listings)} main + {len(psu_listings)} PSU/sports).", flush=True)
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
                    lot_value = sum(
                        sheets_client.parse_price(m["max_price_single"]) for m in needed_buttons
                    )
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
# on-demand2 / crawl500 (called from /internal/crawl500)
# ---------------------------------------------------------------------------

def _run_crawl500() -> int:
    """Run the fixed on-demand2 search (config.CRAWL500_QUERIES), cap at 500 lots,
    NO seller exclusion (apparel-keyword + Clothing-category filters stay on).

    First run (per the GCS marker) may re-scan already-seen lots to reach 500;
    every run after processes only unseen lots. Every lot's crops are logged per
    event for automation. Returns the number of lots processed.
    """
    from . import ebay_client, seen_items as seen_store

    try:
        ebay_app_id  = _get_secret("EBAY_APP_ID")
        ebay_cert_id = _get_secret("EBAY_CERT_ID")
    except Exception as exc:
        print(f"!!! CRAWL500: eBay credentials unavailable — aborting: {exc}", flush=True)
        notifier.send_warning(_slack_token, _channel_id, "/crawl500: no eBay credentials.")
        return 0

    first_run = not seen_store.ondemand2_first_run_done()
    print(f">>> CRAWL500: starting (first_run={first_run}) over "
          f"{len(config.CRAWL500_QUERIES)} OR-expanded queries.", flush=True)

    # OR-expansion → one <=200 window per (bank x type); NO seller exclusion.
    try:
        all_listings = ebay_client.find_all_listings(
            client_id=ebay_app_id, client_secret=ebay_cert_id,
            queries=config.CRAWL500_QUERIES,
            excluded_sellers=[],                       # do not exclude any seller
            excluded_keywords=config.EXCLUDED_KEYWORDS,  # keep apparel/noise filter
            max_results=200,
        )
    except Exception as exc:
        print(f"!!! CRAWL500: search failed: {exc}", flush=True)
        notifier.send_warning(_slack_token, _channel_id, f"/crawl500 search failed: {exc}")
        return 0

    all_listings = dedup_listings(all_listings)
    seen = seen_store.load_seen()

    if first_run:
        candidate = all_listings                       # may re-scan seen to reach 500
    else:
        candidate = [l for l in all_listings if seen_store.is_new(l["item_id"], seen)]
    new_listings = candidate[: config.CRAWL500_MAX_LOTS]
    print(f">>> CRAWL500: {len(all_listings)} unique found; processing "
          f"{len(new_listings)} (cap {config.CRAWL500_MAX_LOTS}).", flush=True)

    if not new_listings:
        notifier.send_warning(_slack_token, _channel_id,
                              "/crawl500: no lots to process this run.")
        if first_run:
            seen_store.mark_ondemand2_first_run_done()
        return 0

def _post_logging_only(listing: dict, result: dict) -> None:
    """Post a text-only lot summary to Slack — no interactive buttons.

    Used to fill the minimum-50-posts guarantee in the final 50 lots of a
    crawl500 run.  Deliberately minimal: one mrkdwn message, no blocks, so
    it doesn't clutter the channel with review requests for lots that don't
    need them.
    """
    try:
        title     = listing.get("title", "?")[:60]
        url       = listing.get("url") or listing.get("listing_url") or ""
        asking    = listing.get("current_price")
        price_str = f" · ${asking:.2f}" if asking else ""

        confirmed = result.get("confirmed", [])
        yellow    = result.get("yellow", [])

        lines = [f"📋 *Scout log* · <{url}|{title}>{price_str}"]
        if confirmed:
            lines.append("✅ " + "  ·  ".join(
                f"{b['year']} — {b['slogan']}" for b in confirmed[:6]
            ))
        if yellow:
            lines.append("🟡 " + "  ·  ".join(
                f"{b['year']} ({int(b['overall'] * 100)}%)" for b in yellow[:6]
            ))
        if not confirmed and not yellow:
            lines.append("_(no matches above threshold)_")

        app.client.chat_postMessage(
            token=_slack_token,
            channel=_channel_id,
            text="\n".join(lines),
        )
    except Exception as exc:
        print(f"!!! CRAWL500: logging-only post failed: {exc}", flush=True)


    job_id   = str(uuid.uuid4())
    stat_alerted = stat_confirmed_not_needed = stat_rejected = 0
    _since_save  = 0
    slack_posts_count = 0          # lots that generated ANY Slack output this run
    MIN_SLACK_POSTS   = 50         # guarantee at least this many posts per crawl
    total_listings    = len(new_listings)

    from . import clip_matcher as _cm
    ref_years = _cm.reference_years()

    for lot_idx, listing in enumerate(new_listings):
        item_id = listing["item_id"]
        asking  = listing.get("current_price", 0.0)
        title   = listing.get("title", "?")
        seller  = listing.get("seller", "")

        title_years_all = extract_years(title)
        title_decades   = extract_decades(title)
        if title_decades:
            restrict_years: set[int] | None = title_decades
        elif len(title_years_all) == 1 and next(iter(title_years_all)) in ref_years:
            restrict_years = {next(iter(title_years_all))}
        else:
            restrict_years = None

        try:
            picture_urls = ebay_client.get_item_pictures(ebay_app_id, ebay_cert_id, item_id)
            if not picture_urls and listing.get("gallery_url"):
                picture_urls = [listing["gallery_url"]]

            # _keep_cpu_hot spins a lightweight thread so Cloud Run's scheduler
            # sees continuous activity during CLIP inference and cv2 work.
            # Mirrors the same pattern used in buttonmatcher's process_grid.
            # Without it, the gap between lot downloads gets throttled to ~5 %
            # CPU when _SERVICE_URL routes through localhost instead of the LB.
            with _keep_cpu_hot():
                result = _evaluate_listing(
                    listing, picture_urls, restrict_years,
                    command="/crawl500", job_id=job_id,
                    title_years=title_years_all, title_count=extract_lot_count(title),
                )

            # --- Minimum-50-posts guarantee --------------------------------------
            # Track whether this lot generates a natural Slack post (yellow review
            # or needed-button alert).  As we enter the final 50 lots, any deficit
            # against the 50-post minimum is filled with logging-only posts — a
            # plain text message with no interactive buttons.  Natural posts (those
            # with real content) are never suppressed and always count toward the
            # minimum, so the crawl is allowed to exceed 50 posts freely.
            in_final_50     = lot_idx >= total_listings - 50
            deficit         = max(0, MIN_SLACK_POSTS - slack_posts_count)
            lots_remaining  = total_listings - lot_idx   # including this one

            will_post_full  = bool(result.get("yellow") or result.get("needed"))

            if will_post_full:
                # Natural full post — always send regardless of where we are.
                if result.get("yellow"):
                    _post_yellow_review(
                        listing=listing,
                        yellow_buttons=result["yellow"],
                        job_id=job_id,
                        confirmed_buttons=result.get("confirmed", []),
                        command="/crawl500",
                    )
                slack_posts_count += 1
            elif in_final_50 and deficit > 0:
                # No natural content but we're in the final 50 and still below
                # the minimum.  Post a logging-only summary to fill the deficit.
                # Cap: only do this for the first `deficit` lots in the final 50
                # so we don't spam if the deficit is large relative to lots left.
                _post_logging_only(listing, result)
                slack_posts_count += 1
                print(
                    f">>> CRAWL500: logging-only post #{slack_posts_count} "
                    f"(deficit={deficit}, lot {lot_idx+1}/{total_listings})",
                    flush=True,
                )

            if not result["confirmed"]:
                stat_rejected += 1
            elif result["needed"]:
                needed_buttons = result["needed"]
                lot_value = sum(
                    sheets_client.parse_price(m["max_price_single"]) for m in needed_buttons
                )
                notifier.send_needed_alert(
                    slack_token=_slack_token, channel=_channel_id, listing=listing,
                    needed_buttons=needed_buttons, asking_price=asking, lot_value=lot_value,
                )
                stat_alerted     += 1
                slack_posts_count += 1   # needed alert is also a Slack post
            else:
                stat_confirmed_not_needed += 1
        except Exception as exc:
            print(f"!!! CRAWL500: error processing {item_id} [{seller}]: {exc}", flush=True)
            traceback.print_exc()

        seen_store.mark_seen(item_id, seen)
        _since_save += 1
        if _since_save >= 50:
            seen_store.save_seen(seen)
            _since_save = 0

    seen_store.save_seen(seen)
    if first_run:
        seen_store.mark_ondemand2_first_run_done()

    notifier.send_crawl500_summary(
        slack_token=_slack_token, channel=_channel_id,
        processed=len(new_listings), alerted=stat_alerted,
        confirmed_not_needed=stat_confirmed_not_needed, rejected=stat_rejected,
        first_run=first_run,
    )
    print(f">>> CRAWL500: complete — processed={len(new_listings)} alerted={stat_alerted} "
          f"confirmed_not_needed={stat_confirmed_not_needed} rejected={stat_rejected} "
          f"slack_posts={slack_posts_count}.", flush=True)
    return len(new_listings)


def _log_gemini_count(job_id: str, item_id: str, total_button_count: int) -> None:
    """Log Gemini's button-count estimate as its own confirm_log row.

    source='gemini_count' — distinct from the human 'user_count' bucket
    (scout_count_* handlers) so the two stay separately queryable.
    """
    if match_logger is None:
        return
    check_id = f"gemini_count:{item_id}"
    try:
        rec = mlog.build_confirm_record(
            service="ebayscout", command="/crawl10", job_id=job_id, thread_ts=None,
            crop_num=None, check_id=check_id, user_id=None,
            chosen_year=None, chosen_phrase=str(total_button_count),
            chosen_type=None, source="gemini_count",
            rank_restricted=None, rank_shadow=None, shadow_leaderboard_size=None,
        )
        match_logger.log_confirmation(check_id, rec)
    except Exception as exc:
        print(f"!!! CRAWL10: gemini_count log failed for {item_id}: {exc}", flush=True)


def _gemini_resolve_yellow(job_id: str, item_id: str, result: dict,
                           gemini_res: dict) -> list[dict]:
    """Promote yellow candidates whose slogan Gemini also detected.

    Mutates result['yellow']/['confirmed']/['needed'] in place. Each promoted
    candidate is logged to confirm_log as source='gemini_verify_yes' — kept
    distinct from the human 'human_verify_yes' (scout_verify_yes handler) so
    Gemini-vs-human accuracy can be compared and the calibration ground-truth
    stays uncorrupted. Returns the list of promoted candidates.
    """
    detected = gemini_res.get("detected_slogans") or []
    if not detected:
        return []

    remaining_yellow: list[dict] = []
    promoted: list[dict] = []
    for btn in result["yellow"]:
        if any(gemini_triage.slogans_match(btn["slogan"], s) for s in detected):
            promoted.append(btn)
        else:
            remaining_yellow.append(btn)

    if not promoted:
        return []

    result["yellow"] = remaining_yellow
    for btn in promoted:
        result["confirmed"].append(btn)
        if match_logger is not None:
            try:
                rec = mlog.build_confirm_record(
                    service="ebayscout", command="/crawl10", job_id=job_id, thread_ts=None,
                    crop_num=None, check_id=btn.get("check_id"), user_id=None,
                    chosen_year=btn["year"], chosen_phrase=btn["slogan"],
                    chosen_type="Football", source="gemini_verify_yes",
                    rank_restricted=None, rank_shadow=None, shadow_leaderboard_size=None,
                )
                match_logger.log_confirmation(btn.get("check_id"), rec)
            except Exception as exc:
                print(f"!!! CRAWL10: gemini_verify_yes log failed for {item_id}: {exc}",
                      flush=True)

        enriched = _check_needed_hit(btn, buy_rules)
        if enriched is not None:
            result["needed"].append(enriched)

    return promoted


def _build_gemini_summary(gemini_res: dict, resolved: list[dict],
                           confirmed_count: int = 0,
                           remaining_yellow_count: int = 0) -> str:
    """Build the '🤖 Gemini triage' summary line prepended to the Slack
    yellow-review post (see _post_yellow_review's gemini_summary param)."""
    total   = gemini_res.get("total_button_count", 0)
    blue    = gemini_res.get("blue_background_count", 0)
    white   = gemini_res.get("white_background_count", 0)
    flagged = gemini_res.get("flagged_problem_slogans") or []

    if not total and not resolved and not flagged:
        return ""

    lines = []
    if total:
        lines.append(f"🤖 *Gemini triage*: {total} button(s) ({blue} blue, {white} white)")
    if resolved:
        resolved_str = "  ·  ".join(f"{b['year']} — {b['slogan']}" for b in resolved)
        lines.append(f"✅ Auto-resolved via Gemini: {resolved_str}")
    if total and remaining_yellow_count and confirmed_count >= total:
        lines.append(
            f"⚠️ Gemini counted only {total} button(s) total and {confirmed_count} "
            f"already auto-confirmed — the candidate(s) below may be a duplicate "
            f"detection of the same physical button rather than an additional one."
        )
    if flagged:
        lines.append("⚠️ Gemini flagged as hard to match: " + "  ·  ".join(flagged))
    return "\n".join(lines)


def _run_crawl10(gemini_api_key: str) -> int:
    """Run the fixed /crawl10 search (config.CRAWL10_QUERY), cap at 10 lots,
    NO seller exclusion (apparel-keyword + Clothing-category filters stay on).

    Adds a Gemini Flash triage pass per lot on the primary photo: logs the
    button-count estimate (source='gemini_count') and auto-resolves yellow
    candidates whose slogan Gemini also detected (source='gemini_verify_yes').

    Does NOT touch seen_items.json — /crawl10 is a repeatable test harness
    over the same ~10 lots across iterations of the Gemini prompt. Returns the
    number of lots processed.
    """
    from . import ebay_client, clip_matcher as _cm

    try:
        ebay_app_id  = _get_secret("EBAY_APP_ID")
        ebay_cert_id = _get_secret("EBAY_CERT_ID")
    except Exception as exc:
        print(f"!!! CRAWL10: eBay credentials unavailable — aborting: {exc}", flush=True)
        notifier.send_warning(_slack_token, _channel_id, "/crawl10: no eBay credentials.")
        return 0

    print(f">>> CRAWL10: starting — query={config.CRAWL10_QUERY!r}.", flush=True)

    try:
        all_listings = ebay_client.find_all_listings(
            client_id=ebay_app_id, client_secret=ebay_cert_id,
            queries=[config.CRAWL10_QUERY],
            excluded_sellers=[],                       # do not exclude any seller
            excluded_keywords=config.EXCLUDED_KEYWORDS,  # keep apparel/noise filter
            max_results=200,
        )
    except Exception as exc:
        print(f"!!! CRAWL10: search failed: {exc}", flush=True)
        notifier.send_warning(_slack_token, _channel_id, f"/crawl10 search failed: {exc}")
        return 0

    new_listings = dedup_listings(all_listings)[: config.CRAWL10_MAX_LOTS]
    print(f">>> CRAWL10: {len(all_listings)} unique found; processing "
          f"{len(new_listings)} (cap {config.CRAWL10_MAX_LOTS}).", flush=True)

    if not new_listings:
        notifier.send_warning(_slack_token, _channel_id,
                              "/crawl10: no lots to process this run.")
        return 0

    job_id = str(uuid.uuid4())
    stat_alerted = stat_confirmed_not_needed = stat_rejected = stat_gemini_resolved = 0
    ref_years = _cm.reference_years()

    for listing in new_listings:
        item_id = listing["item_id"]
        asking  = listing.get("current_price", 0.0)
        title   = listing.get("title", "?")
        seller  = listing.get("seller", "")

        title_years_all = extract_years(title)
        title_decades   = extract_decades(title)
        if title_decades:
            restrict_years: set[int] | None = title_decades
        elif len(title_years_all) == 1 and next(iter(title_years_all)) in ref_years:
            restrict_years = {next(iter(title_years_all))}
        else:
            restrict_years = None

        try:
            picture_urls = ebay_client.get_item_pictures(ebay_app_id, ebay_cert_id, item_id)
            if not picture_urls and listing.get("gallery_url"):
                picture_urls = [listing["gallery_url"]]

            with _keep_cpu_hot():
                result = _evaluate_listing(
                    listing, picture_urls, restrict_years,
                    command="/crawl10", job_id=job_id,
                    title_years=title_years_all, title_count=extract_lot_count(title),
                    return_first_image=True,
                )

            gemini_res = dict(gemini_triage.EMPTY_RESULT)
            if result.get("first_image_bytes"):
                print(">>> CRAWL10: running Gemini triage on primary photo...", flush=True)
                gemini_res = gemini_triage.analyze_lot_with_gemini(
                    result["first_image_bytes"], gemini_api_key)

            _log_gemini_count(job_id, item_id, gemini_res["total_button_count"])

            resolved = _gemini_resolve_yellow(job_id, item_id, result, gemini_res)
            stat_gemini_resolved += len(resolved)

            gemini_summary = _build_gemini_summary(
                gemini_res, resolved,
                confirmed_count=len(result["confirmed"]),
                remaining_yellow_count=len(result["yellow"]),
            )

            if result.get("yellow") or gemini_summary:
                _post_yellow_review(
                    listing=listing,
                    yellow_buttons=result["yellow"],
                    job_id=job_id,
                    confirmed_buttons=result.get("confirmed", []),
                    command="/crawl10",
                    gemini_summary=gemini_summary,
                )

            if not result["confirmed"]:
                stat_rejected += 1
            elif result["needed"]:
                needed_buttons = result["needed"]
                lot_value = sum(
                    sheets_client.parse_price(m["max_price_single"]) for m in needed_buttons
                )
                notifier.send_needed_alert(
                    slack_token=_slack_token, channel=_channel_id, listing=listing,
                    needed_buttons=needed_buttons, asking_price=asking, lot_value=lot_value,
                )
                stat_alerted += 1
            else:
                stat_confirmed_not_needed += 1
        except Exception as exc:
            print(f"!!! CRAWL10: error processing {item_id} [{seller}]: {exc}", flush=True)
            traceback.print_exc()

    notifier.send_crawl10_summary(
        slack_token=_slack_token, channel=_channel_id,
        processed=len(new_listings), alerted=stat_alerted,
        confirmed_not_needed=stat_confirmed_not_needed, rejected=stat_rejected,
        gemini_resolved=stat_gemini_resolved,
    )
    print(f">>> CRAWL10: complete — processed={len(new_listings)} alerted={stat_alerted} "
          f"confirmed_not_needed={stat_confirmed_not_needed} rejected={stat_rejected} "
          f"gemini_resolved={stat_gemini_resolved}.", flush=True)
    return len(new_listings)


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


