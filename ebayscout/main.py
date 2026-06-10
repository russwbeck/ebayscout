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
import json
import math
import os
import re
import threading
import time
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
from . import ebay_client
from . import match_logging as mlog
from .match_logging import SheetLogger
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
_slack_token    = _get_secret("EBAY_BOT_TOKEN")
_signing_secret = _get_secret("SIGNING_SECRET_ES")
_channel_id     = _get_secret("CHANNEL_ID_EBAY")
print(">>> INIT: Slack secrets loaded.", flush=True)

# ---------------------------------------------------------------------------
# Slack Bolt app — powers the interactive /crawl500 human-in-the-loop review.
# Slash command + interactivity payloads are routed through POST /slack/events.
# ---------------------------------------------------------------------------
bolt_app = App(token=_slack_token, signing_secret=_signing_secret)
handler  = SlackRequestHandler(bolt_app)

# ---------------------------------------------------------------------------
# Global state (populated by startup())
# ---------------------------------------------------------------------------

buy_rules:     dict = {}
vectors_loaded: bool = False

# --- STRUCTURED LOGGING (advanced match/detection analytics) ---
# Disabled until startup() attaches the log tabs.  All writes fail-open so
# logging can never break /run-scan.  Shared byte-identical match_logging module.
match_logger = SheetLogger(None, None, service="ebayscout")

# Interactive /crawl500 review state (in-memory; lost on container restart, like
# buybot's pending_* sessions). Keyed by check_id → the context an action handler
# needs to write a confirmation record when the human clicks a button.
#   pending_crawl_reviews[check_id] = {
#       "job_id", "crop_num", "channel_id", "thread_ts", "candidates",
#       "restricted_top", "shadow_top", "listing_url", "title",
#   }
pending_crawl_reviews: dict = {}

# Held for the duration of a /crawl500 run so an overlapping trigger can't start
# a second concurrent crawl.
_crawl_lock = threading.Lock()

# Held for the duration of a daily scan so an overlapping trigger can't start
# a second concurrent run.
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


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    """Single Slack ingress: slash commands + interactivity (button) payloads.

    Configure the /crawl500 slash command Request URL AND the Interactivity
    Request URL in the Slack app to point here.
    """
    return handler.handle(request)


def _self_base_url() -> str:
    """This service's own public base URL (for the in-flight CPU self-request)."""
    return os.environ.get("SERVICE_BASE_URL", config.SERVICE_BASE_URL).rstrip("/")


@bolt_app.command("/crawl500")
def handle_crawl500(ack, command, client):
    """Kick off the interactive crawl. Ack within Slack's 3s window, then fire a
    self-request to /internal/crawl500 so the heavy work runs inside a live HTTP
    request (Cloud Run keeps CPU allocated only during request handling)."""
    ack()
    channel_id = command.get("channel_id") or _channel_id
    user_id    = command.get("user_id", "")
    text       = (command.get("text") or "").strip()
    try:
        max_lots = int(text) if text else config.CRAWL500_MAX_LOTS
    except ValueError:
        max_lots = config.CRAWL500_MAX_LOTS
    max_lots = max(1, min(max_lots, config.CRAWL500_MAX_LOTS))

    try:
        client.chat_postMessage(
            channel=channel_id,
            text=f"🚀 Starting /crawl500 (up to {max_lots} lots). "
                 f"I'll post needed-button candidates here for review as I go.",
        )
    except Exception as exc:
        print(f"!!! CRAWL500: ack post failed: {exc}", flush=True)

    payload = {"channel_id": channel_id, "user_id": user_id, "max_lots": max_lots}
    try:
        requests.post(f"{_self_base_url()}/internal/crawl500", json=payload, timeout=5)
    except requests.exceptions.ReadTimeout:
        # Expected: the internal endpoint runs the long crawl synchronously.
        pass
    except Exception as exc:
        print(f"!!! CRAWL500: failed to dispatch internal crawl: {exc}", flush=True)
        try:
            client.chat_postMessage(
                channel=channel_id,
                text=f"❌ Could not start the crawl: {exc}",
            )
        except Exception:
            pass


@flask_app.route("/internal/crawl500", methods=["POST"])
def internal_crawl500():
    """Run the crawl synchronously inside this request (in-flight CPU pattern).
    Called only by this service itself (handle_crawl500)."""
    data       = request.get_json(silent=True) or {}
    channel_id = data.get("channel_id") or _channel_id
    user_id    = data.get("user_id", "")
    max_lots   = int(data.get("max_lots", config.CRAWL500_MAX_LOTS))

    if not vectors_loaded and not _ensure_clip_loaded():
        return jsonify({"status": "clip init failed"}), 500

    if not _crawl_lock.acquire(blocking=False):
        return jsonify({"status": "already running"}), 409
    try:
        with _keep_cpu_hot():
            summary = _run_crawl500(channel_id, user_id, max_lots)
    finally:
        _crawl_lock.release()
    return jsonify({"status": "crawl complete", **summary}), 200


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

    stat_alerted        = 0
    stat_low_confidence = 0
    stat_rejected       = 0
    _listings_since_save = 0
    scan_log_records: list[dict] = []   # one record per processed listing (groundwork data)
    _scanlog_flushed = 0                # how many records already appended to GCS

    from . import image_proc as _ip   # lazy — torch/cv2 imported here if not yet
    from . import clip_matcher as _cm  # lazy — torch/clip imported here if not yet

    ref_years = _cm.reference_years()  # known years; gates single-title-year restriction

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

            # Two-stage photo gating: always score photo 1; only pull the rest
            # when the listing looks promising (title names a year, or photo 1
            # already shows button-like signal). Keeps the scan from N× photo
            # downloads on every junk listing now that we score multiple photos.
            title_years = title_years_all

            best_score_seen   = 0.0
            photos_processed  = 0
            needed_hits:    dict[tuple, dict] = {}  # (year, slogan) → enriched needed match
            strong_matched: dict[tuple, dict] = {}  # (year, slogan) → match >= CONFIDENCE
            log_top_matches: list[dict] = []
            best_needed: dict | None = None         # top needed-mapped candidate,
                                                    # even below bar (for tuning)

            # Per-photo crop ceiling = 100, raised if the title states a bigger
            # lot ("Lot of 250 pins"). Per-listing budget bounds total CLIP cost.
            title_count   = extract_lot_count(title)
            per_photo_cap = max(config.MAX_CROPS_PER_PHOTO, title_count or 0)
            listing_budget = max(config.MAX_CROPS_PER_LISTING, title_count or 0)

            for photo_idx, photo_url in enumerate(picture_urls[: config.MAX_PHOTOS_PER_LISTING]):
                if photo_idx == 1:  # decided after photo 1 is scored
                    promising = bool(title_years) or best_score_seen >= config.REJECTION_THRESHOLD
                    if not promising:
                        break
                if listing_budget <= 0:
                    break

                try:
                    image_bytes = _ip.download_image(photo_url)
                    crops       = _ip.detect_and_crop(image_bytes, max_crops=per_photo_cap)
                except Exception as exc:
                    print(f"!!! SCAN: Photo processing failed: {exc}", flush=True)
                    continue

                if crops:
                    crops = crops[:listing_budget]
                    listing_budget -= len(crops)

                if not crops:
                    continue

                photos_processed += 1

                # One forward pass; keep top-K candidates per crop down to the
                # rejection floor so a needed button that is the 2nd/3rd guess
                # on a blended photo is still visible.
                try:
                    batch_results = _cm.match_crops_batch(
                        crops,
                        threshold=config.REJECTION_THRESHOLD,
                        top_k=config.NEEDED_MATCH_TOP_K,
                        restrict_years=restrict_years,
                    )
                except Exception as exc:
                    print(f"!!! SCAN: match_crops_batch failed: {exc}", flush=True)
                    continue

                for candidates in batch_results:
                    if not candidates:
                        continue
                    best_score_seen = max(best_score_seen, candidates[0]["overall"])
                    log_top_matches.append(candidates[0])

                    for m in candidates:
                        key     = (m["year"], m["slogan"])
                        overall = m["overall"]

                        # Strict matches feed the (optional) undervalued path.
                        if overall >= config.CONFIDENCE_THRESHOLD and (
                            key not in strong_matched or overall > strong_matched[key]["overall"]
                        ):
                            strong_matched[key] = m

                        # Needed-button presence (recall-biased). Reuse the
                        # fuzzy sheet lookup; a title-year match lowers the bar.
                        price_single, _, _, amount_needed = sheets_client.get_buy_decision(
                            m["year"], m["slogan"], buy_rules
                        )
                        if amount_needed <= 0:
                            continue
                        # Placeholder slogans (e.g. "Slogan Unknown N") stay in
                        # the buy logic but must not trigger scan alerts — they
                        # over-match and inflate lot value.
                        if is_non_alerting_slogan(m["slogan"], config.NON_ALERTING_SLOGAN_PATTERNS):
                            continue
                        # Track the best needed-mapped candidate regardless of
                        # whether it clears the bar — this is the distribution
                        # used to tune NEEDED_MATCH_THRESHOLD.
                        if best_needed is None or overall > best_needed["overall"]:
                            best_needed = {"year": m["year"], "slogan": m["slogan"],
                                           "overall": overall}
                        try:
                            title_corroborated = int(m["year"]) in title_years
                        except (ValueError, TypeError):
                            title_corroborated = False
                        bar = (config.REJECTION_THRESHOLD if title_corroborated
                               else config.NEEDED_MATCH_THRESHOLD)
                        if overall < bar:
                            continue
                        if key not in needed_hits or overall > needed_hits[key]["overall"]:
                            enriched = dict(m)
                            enriched["max_price_single"] = price_single
                            enriched["amount_needed"]    = amount_needed
                            needed_hits[key] = enriched

            if photos_processed == 0 or best_score_seen < config.REJECTION_THRESHOLD:
                stat_rejected += 1
                # Greppable title log for tuning EXCLUDED_KEYWORDS over time:
                # filter Cloud Logging for "TITLE: [rejected".
                print(f">>> TITLE: [rejected {best_score_seen:.2f}] [{seller}] {title}", flush=True)
                scan_log_records.append(_scan_log_record(
                    listing, photos_processed, best_score_seen, log_top_matches,
                    needed_hit=False, alerted=False, best_needed=best_needed,
                ))
                seen_store.mark_seen(item_id, seen)
                continue

            needed_buttons  = list(needed_hits.values())
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
                        slack_token=_slack_token,
                        channel=_channel_id,
                        listing=listing,
                        needed_buttons=needed_buttons,
                        asking_price=asking,
                        lot_value=lot_value,
                    )
                listing_alerted = True

            # Optional, off by default: precise undervalued/margin alert. We no
            # longer trust auto-valuation as the headline (see config).
            if config.ENABLE_UNDERVALUED_ALERTS and strong_matched:
                enriched_strong = []
                for (year, slogan), m in strong_matched.items():
                    price_single, _, _, amount_needed = sheets_client.get_buy_decision(
                        year, slogan, buy_rules
                    )
                    e = dict(m)
                    e["max_price_single"] = price_single
                    e["amount_needed"]    = amount_needed
                    enriched_strong.append(e)
                lot_value = sum(sheets_client.parse_price(m["max_price_single"]) for m in enriched_strong)
                margin    = lot_value - asking
                if margin > 0:
                    if dry_run:
                        print(f"    [DRY RUN] Would post undervalued alert for {item_id}", flush=True)
                    else:
                        notifier.send_undervalued_alert(
                            slack_token=_slack_token,
                            channel=_channel_id,
                            listing=listing,
                            matches=enriched_strong,
                            lot_value=lot_value,
                            asking_price=asking,
                            margin=margin,
                            unmatched_count=0,
                        )
                    listing_alerted = True

            if listing_alerted:
                stat_alerted += 1
            else:
                stat_low_confidence += 1
                print(f">>> TITLE: [low-conf {best_score_seen:.2f}] [{seller}] {title}", flush=True)

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
            f"low_conf={stat_low_confidence}, rejected={stat_rejected}",
            flush=True,
        )
    else:
        try:
            notifier.send_scan_summary(
                slack_token=_slack_token,
                channel=_channel_id,
                alerted=stat_alerted,
                low_confidence=stat_low_confidence,
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


# ===========================================================================
# Interactive /crawl500 — human-in-the-loop crawl
#
# A broad eBay crawl whose needed-button candidates are routed to Slack for human
# verification (per-button ✅/❌ + a count bucket), producing the first
# ground-truth labels a scan can generate (human_verify_*, user_count → logged to
# confirm_log).  Every crop is logged to match_log via match_crops_with_diagnostics.
# Confirmed (green/auto, passing the gap guard) candidates auto-record; the
# uncertain band (>= RED) is where human labels are requested.
# ===========================================================================

_COUNT_BUCKETS = ["1", "2-5", "6-20", "20+"]


def _restrict_years_for(listing: dict, ref_years: set[int]) -> set[int] | None:
    """Years the matcher may consider for a listing (mirrors _run_daily_scan)."""
    title = listing.get("title", "?")
    title_years_all = extract_years(title)
    title_decades   = extract_decades(title)
    search_year     = listing.get("search_year")
    search_era      = listing.get("search_era")
    if search_year:
        return {int(search_year)} | title_decades
    if search_era:
        return era_year_set(search_era, config.BUTTON_ERAS) or None
    if title_decades:
        return title_decades
    if len(title_years_all) == 1 and next(iter(title_years_all)) in ref_years:
        return {next(iter(title_years_all))}
    return None


def _log_confirmation(ctx: dict, source: str, *, chosen: bool = True,
                      user_id: str = "", typed_slogan: str | None = None) -> None:
    """Write one confirm_log row joined to a crop's match_log row by check_id."""
    try:
        restricted = ctx.get("restricted_top") or []
        shadow     = ctx.get("shadow_top") or []
        year       = ctx.get("year")
        rec = mlog.build_confirm_record(
            service="ebayscout", command="/crawl500", job_id=ctx.get("job_id"),
            thread_ts=ctx.get("thread_ts"), crop_num=ctx.get("crop_num"),
            check_id=ctx.get("check_id"), user_id=user_id,
            chosen_year=(year if chosen else None),
            chosen_phrase=(ctx.get("slogan") if chosen else None),
            chosen_type="Football",
            source=source,
            rank_restricted=mlog.rank_of(year, restricted) if chosen else None,
            rank_shadow=mlog.rank_of(year, shadow) if chosen else None,
            shadow_leaderboard_size=len(shadow),
            restricted_top=restricted, shadow_top=shadow,
            typed_slogan=typed_slogan,
        )
        match_logger.log_confirmation(ctx.get("check_id"), rec)
    except Exception as exc:
        print(f">>> CRAWL500: confirm log failed: {exc}", flush=True)


def _post_yellow_review(listing: dict, best: dict, channel_id: str) -> None:
    """Post an interactive needed-button review: per-button ✅/❌ + a count bucket.
    Registers the context under best['check_id'] so the action handlers can log."""
    title       = listing.get("title", "?")
    listing_url = listing.get("listing_url") or listing.get("gallery_url") or ""
    asking      = listing.get("current_price", 0.0)
    check_id    = best["check_id"]

    title_txt = title if len(title) <= 80 else title[:77] + "…"
    header = (
        f"🟡 *Needed-button candidate — please verify*\n"
        f"*<{listing_url}|{title_txt}>*\n"
        f"Asking: *${asking:.2f}*  |  Best match: *{best['year']} {best['slogan']}* "
        f"(score {best['overall']:.2f})"
    )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Correct"},
             "style": "primary", "action_id": "scout_verify_yes", "value": check_id},
            {"type": "button", "text": {"type": "plain_text", "text": "❌ Wrong / not this"},
             "action_id": "scout_verify_no", "value": check_id},
        ]},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": "How many buttons are in this lot?"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": b},
             "action_id": f"scout_count_{i}", "value": f"{check_id}|{b}"}
            for i, b in enumerate(_COUNT_BUCKETS)
        ]},
    ]
    try:
        resp = bolt_app.client.chat_postMessage(
            channel=channel_id, blocks=blocks, text=header)
        thread_ts = resp.get("ts")
    except Exception as exc:
        print(f">>> CRAWL500: yellow review post failed: {exc}", flush=True)
        thread_ts = None

    ctx = dict(best)
    ctx["channel_id"] = channel_id
    ctx["thread_ts"]  = thread_ts
    ctx["listing_url"] = listing_url
    ctx["title"] = title
    pending_crawl_reviews[check_id] = ctx


def _evaluate_listing(listing, restrict_years, ebay_creds, channel_id,
                      command="/crawl500", dry_run=False) -> dict:
    """Detect + match one listing, log every crop, and route the best needed-button
    candidate to auto-confirm / yellow review / logging-only."""
    from . import image_proc as _ip
    from . import clip_matcher as _cm

    ebay_app_id, ebay_cert_id = ebay_creds
    item_id = listing["item_id"]
    title   = listing.get("title", "?")
    asking  = listing.get("current_price", 0.0)
    bank    = listing.get("search_era") or "all"

    # Picture URLs — same sourcing as the daily scan.
    if item_id.startswith("etsy_"):
        picture_urls = [listing["gallery_url"]] if listing.get("gallery_url") else []
    elif ebay_app_id and ebay_cert_id:
        picture_urls = ebay_client.get_item_pictures(ebay_app_id, ebay_cert_id, item_id)
        if not picture_urls and listing.get("gallery_url"):
            picture_urls = [listing["gallery_url"]]
    else:
        picture_urls = [listing["gallery_url"]] if listing.get("gallery_url") else []
    if not picture_urls:
        return {"status": "no_photos"}

    title_years   = extract_years(title)
    title_count   = extract_lot_count(title)
    per_photo_cap = max(config.MAX_CROPS_PER_PHOTO, title_count or 0)
    listing_budget = max(config.MAX_CROPS_PER_LISTING, title_count or 0)

    job_id      = f"crawl500:{item_id}:{int(time.time())}"
    all_records = []
    crop_counter = 0
    best: dict | None = None
    best_score_seen = 0.0
    shadow_on = match_logger.enabled and mlog.shadow_pass_enabled()

    for photo_idx, photo_url in enumerate(picture_urls[: config.MAX_PHOTOS_PER_LISTING]):
        if photo_idx == 1:
            promising = bool(title_years) or best_score_seen >= config.REJECTION_THRESHOLD
            if not promising:
                break
        if listing_budget <= 0:
            break
        try:
            image_bytes = _ip.download_image(photo_url)
            diag: dict = {}
            crops = _ip.detect_and_crop(image_bytes, max_crops=per_photo_cap, diag_out=diag)
        except Exception as exc:
            print(f"!!! CRAWL500: photo processing failed: {exc}", flush=True)
            continue
        if not crops:
            continue
        crops = crops[:listing_budget]
        listing_budget -= len(crops)

        ni_count = ni_diag = None
        if shadow_on:
            try:
                ni_count, ni_diag = _ip.count_circles_unguided_from_bytes(image_bytes)
            except Exception as _de:
                print(f">>> CRAWL500: unguided detect failed: {_de}", flush=True)

        try:
            diags = _cm.match_crops_with_diagnostics(crops, restrict_years=restrict_years)
        except Exception as exc:
            print(f"!!! CRAWL500: match failed: {exc}", flush=True)
            continue

        detection = mlog.build_detection_diag(
            h=diag.get("h", 0), w=diag.get("w", 0),
            bg_brightness=diag.get("bg_brightness", 0.0),
            bg_is_white=diag.get("bg_is_white", False),
            mask_path=diag.get("mask_path", ""),
            hough_pass1_count=len(crops), hough_retry_count=None,
            final_count_user=len(crops), final_count_noinput=ni_count,
            user_count=None, detector_used="hough", n_crops=len(crops),
            bg_saturation=diag.get("bg_saturation"),
            mask_components=diag.get("mask_components"),
            noinput_diag=ni_diag,
        )

        for d in diags:
            crop_counter += 1
            check_id = uuid.uuid4().hex[:12]
            all_records.append(mlog.build_match_record(
                service="ebayscout", command=command, mode="crawl500",
                job_id=job_id, thread_ts=None, channel_id=channel_id, user_id="",
                crop_num=crop_counter, check_id=check_id, detection=detection,
                bank=bank, restricted_top=d["restricted_top"],
                shadow_top=d["shadow_top"], shadow_enabled=d["shadow_enabled"],
            ))
            cands = d["candidates"]
            if not cands:
                continue
            top     = cands[0]
            overall = top["overall"]
            best_score_seen = max(best_score_seen, overall)

            price_single, _, _, amount_needed = sheets_client.get_buy_decision(
                top["year"], top["slogan"], buy_rules)
            if amount_needed <= 0:
                continue
            if is_non_alerting_slogan(top["slogan"], config.NON_ALERTING_SLOGAN_PATTERNS):
                continue
            if best is None or overall > best["overall"]:
                best = {
                    "year": top["year"], "slogan": top["slogan"], "overall": overall,
                    "gap": d["gap"], "crop_num": crop_counter, "check_id": check_id,
                    "job_id": job_id, "price": price_single, "amount": amount_needed,
                    "restricted_top": d["restricted_top"], "shadow_top": d["shadow_top"],
                }

    match_logger.log_image_crops(job_id, all_records)

    if best is None:
        return {"status": "no_needed", "best_score": best_score_seen}

    overall = best["overall"]
    gap     = best["gap"]
    confirmed = _cm.is_confirmed(overall, gap)
    # Gap guard: a None/NaN or thin #1-#2 gap must NOT auto-confirm (the lone
    # uncontested candidate that greened at 0.85 was the documented bug).
    gap_bad = gap is None or (isinstance(gap, float) and math.isnan(gap)) \
        or gap < config.MIN_AUTO_GAP
    if confirmed and gap_bad:
        confirmed = False

    if confirmed:
        # High-confidence + decisive gap → auto-record, no human needed.
        if not dry_run:
            _log_confirmation(best, source="auto", chosen=True)
            try:
                notifier.send_needed_alert(
                    slack_token=_slack_token, channel=channel_id, listing=listing,
                    needed_buttons=[{
                        "year": best["year"], "slogan": best["slogan"],
                        "overall": overall, "max_price_single": best["price"],
                        "amount_needed": best["amount"],
                    }],
                    asking_price=asking,
                    lot_value=sheets_client.parse_price(best["price"]),
                )
            except Exception as exc:
                print(f">>> CRAWL500: auto alert failed: {exc}", flush=True)
        return {"status": "auto_confirmed", "needed": True, "best_score": overall}

    # Guard-demoted (was green/auto) OR uncertain band → ask the human. Any needed
    # candidate at/above RED is worth a human label, regardless of upper band.
    if overall >= config.RED_THRESHOLD:
        if not dry_run:
            _post_yellow_review(listing, best, channel_id)
        return {"status": "yellow_review", "needed": True, "best_score": overall}

    return {"status": "logging_only", "needed": True, "best_score": overall}


def _run_crawl500(channel_id: str, user_id: str, max_lots: int) -> dict:
    """Source a broad set of listings, evaluate each, and post a summary.
    Returns counters for the JSON response."""
    from . import clip_matcher as _cm

    try:
        ebay_app_id  = _get_secret("EBAY_APP_ID")
        ebay_cert_id = _get_secret("EBAY_CERT_ID")
    except Exception as exc:
        print(f"!!! CRAWL500: eBay credentials unavailable: {exc}", flush=True)
        try:
            bolt_app.client.chat_postMessage(
                channel=channel_id, text="❌ /crawl500 needs eBay credentials.")
        except Exception:
            pass
        return {"sourced": 0, "evaluated": 0}

    listings: list[dict] = []
    try:
        listings.extend(ebay_client.find_all_listings(
            client_id=ebay_app_id, client_secret=ebay_cert_id,
            queries=config.CRAWL500_QUERIES,
            excluded_sellers=config.EXCLUDED_SELLERS,
            max_results=config.EBAY_MAX_RESULTS,
        ))
        listings.extend(ebay_client.find_all_listings(
            client_id=ebay_app_id, client_secret=ebay_cert_id,
            queries=config.PSU_SEARCH_QUERIES,
            excluded_sellers=config.EXCLUDED_SELLERS,
            max_results=config.EBAY_MAX_RESULTS,
            category_ids=config.SPORTS_MEMO_CATEGORY_ID,
        ))
    except Exception as exc:
        print(f"!!! CRAWL500: eBay query failed: {exc}", flush=True)

    listings = dedup_listings(listings)[:max_lots]
    print(f">>> CRAWL500: evaluating {len(listings)} deduped listings "
          f"(cap {max_lots}).", flush=True)

    ref_years = _cm.reference_years()
    counters = Counter()
    posted = 0
    for listing in listings:
        try:
            restrict_years = _restrict_years_for(listing, ref_years)
            res = _evaluate_listing(
                listing, restrict_years, (ebay_app_id, ebay_cert_id), channel_id)
            counters[res.get("status", "error")] += 1
            if res.get("status") == "yellow_review":
                posted += 1
        except Exception as exc:
            counters["error"] += 1
            print(f"!!! CRAWL500: error on {listing.get('item_id')}: {exc}", flush=True)
            traceback.print_exc()

    summary = (
        f"✅ /crawl500 done — evaluated *{len(listings)}* lots.\n"
        f"• 🟡 posted for review: *{posted}*\n"
        f"• 🟢 auto-confirmed: *{counters.get('auto_confirmed', 0)}*\n"
        f"• logged-only (below review bar): *{counters.get('logging_only', 0)}*\n"
        f"• no needed candidate: *{counters.get('no_needed', 0)}*"
    )
    try:
        bolt_app.client.chat_postMessage(channel=channel_id, text=summary)
    except Exception as exc:
        print(f">>> CRAWL500: summary post failed: {exc}", flush=True)

    return {"sourced": len(listings), "evaluated": len(listings),
            "posted_for_review": posted, **dict(counters)}


# --- Interactive action handlers (per-button ✅/❌ + count bucket) ----------

@bolt_app.action("scout_verify_yes")
def handle_verify_yes(ack, body, action, client):
    ack()
    check_id = action.get("value")
    ctx = pending_crawl_reviews.get(check_id)
    user_id = (body.get("user") or {}).get("id", "")
    if not ctx:
        return
    _log_confirmation(ctx, source="human_verify_yes", chosen=True, user_id=user_id)
    try:
        client.chat_postMessage(
            channel=ctx["channel_id"], thread_ts=ctx.get("thread_ts"),
            text=f"✅ Recorded: *{ctx['year']} {ctx['slogan']}* confirmed by <@{user_id}>. Thanks!")
    except Exception as exc:
        print(f">>> CRAWL500: verify_yes ack failed: {exc}", flush=True)


@bolt_app.action("scout_verify_no")
def handle_verify_no(ack, body, action, client):
    ack()
    check_id = action.get("value")
    ctx = pending_crawl_reviews.get(check_id)
    user_id = (body.get("user") or {}).get("id", "")
    if not ctx:
        return
    _log_confirmation(ctx, source="human_verify_no", chosen=False, user_id=user_id)
    try:
        client.chat_postMessage(
            channel=ctx["channel_id"], thread_ts=ctx.get("thread_ts"),
            text=f"❌ Recorded: not a *{ctx['year']} {ctx['slogan']}* (per <@{user_id}>). Thanks!")
    except Exception as exc:
        print(f">>> CRAWL500: verify_no ack failed: {exc}", flush=True)


@bolt_app.action(re.compile(r"scout_count_\d+"))
def handle_count_bucket(ack, body, action, client):
    ack()
    raw = action.get("value", "")
    check_id, _, bucket = raw.partition("|")
    ctx = pending_crawl_reviews.get(check_id)
    user_id = (body.get("user") or {}).get("id", "")
    if not ctx:
        return
    # Log the human button count as a confirmation row (source carries the count;
    # joinable to the crop's match_log row by check_id).
    _log_confirmation(ctx, source=f"human_count:{bucket}", chosen=True,
                      user_id=user_id, typed_slogan=bucket)
    try:
        client.chat_postMessage(
            channel=ctx["channel_id"], thread_ts=ctx.get("thread_ts"),
            text=f"🔢 Recorded count *{bucket}* (per <@{user_id}>). Thanks!")
    except Exception as exc:
        print(f">>> CRAWL500: count ack failed: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Startup (called from gunicorn post_fork hook)
# ---------------------------------------------------------------------------

def startup() -> None:
    """Load Google Sheets and CLIP model in the background."""
    global buy_rules, match_logger

    print(">>> STARTUP: Loading buy rules...", flush=True)
    try:
        sheets_json    = _get_secret("GOOGLE_SHEETS_JSON")
        spreadsheet_id = _get_secret("SPREADSHEET_ID")
        buy_rules      = sheets_client.load_buy_rules(sheets_json, spreadsheet_id)
    except Exception as exc:
        print(f"!!! STARTUP: Sheets error: {exc}", flush=True)

    # Attach the structured-logging tabs to the DEDICATED logging workbook
    # (LOGGER_ID secret), separate from the buy-rules sheet.  Fail-open: if this
    # errors, match_logger stays disabled and the scan / crawl run normally.
    try:
        import gspread
        from google.oauth2 import service_account
        _creds_info = json.loads(_get_secret("GOOGLE_SHEETS_JSON"))
        _creds = service_account.Credentials.from_service_account_info(
            _creds_info
        ).with_scopes(["https://www.googleapis.com/auth/spreadsheets"])
        _gc = gspread.authorize(_creds)
        print(f">>> STARTUP: logging service account = "
              f"{_creds_info.get('client_email', 'UNKNOWN')}", flush=True)
        _mws, _cws = mlog.open_log_sheets(_gc, _get_secret("LOGGER_ID"))
        match_logger = SheetLogger(_mws, _cws, service="ebayscout")
        print(f">>> STARTUP: match logging "
              f"{'enabled' if match_logger.enabled else 'DISABLED'} "
              f"(LOGGER_ID workbook).", flush=True)
    except Exception as exc:
        print(f"!!! STARTUP: match logging setup failed: {exc}", flush=True)

    # Hydrate CLIP in the background.  On a cold, CPU-throttled container this
    # may not finish until an HTTP request (a scan) provides CPU — those paths
    # call _ensure_clip_loaded() to force it.
    threading.Thread(target=_ensure_clip_loaded, daemon=True).start()


