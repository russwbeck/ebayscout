"""
ebayscout/main.py

Flask + Slack Bolt service for ebayscout.

Handles:
  POST /slack/events  — file_shared events + message replies
  POST /run-scan      — daily eBay scan (called by Cloud Scheduler)
  GET  /health        — startup health check

Two interaction flows:
  1. Automated daily scan  : Cloud Scheduler → POST /run-scan
  2. Manual image upload   : user uploads photo → bot asks for price & source
                             → user replies → bot posts lot analysis in-thread
"""

import os
import re
import threading
import traceback

from flask import Flask, request, jsonify
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from google.cloud import secretmanager

from . import config
from . import sheets_client
from . import notifier
from . import etsy_client
from .utils import parse_price_source, format_manual_result

# clip_matcher and image_proc import torch/clip/cv2 — loaded lazily so
# a missing .so or slow first-import doesn't kill the entire process at startup.
# They are imported inside startup() and the scan/analysis functions.

# ---------------------------------------------------------------------------
# Secrets — fetched at module load so the Slack App can be created immediately
# (mirrors buybot's pattern; gunicorn imports the module in the master process
#  before forking, so these calls happen once)
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
# Slack Bolt app
# ---------------------------------------------------------------------------

bolt_app = App(token=_slack_token, signing_secret=_signing_secret)
handler  = SlackRequestHandler(bolt_app)

# ---------------------------------------------------------------------------
# Global state (populated by startup())
# ---------------------------------------------------------------------------

buy_rules:     dict = {}
vectors_loaded: bool = False

# pending_scans[user_id] = {file_url, channel_id, thread_ts}
# Keyed by user so simultaneous uploads from different people work independently
pending_scans: dict = {}

# ---------------------------------------------------------------------------
# Slack event: file uploaded to the scout channel
# ---------------------------------------------------------------------------

@bolt_app.event("file_shared")
def handle_file_shared(event, client):
    """
    Fires when a user uploads a file.  Store the file URL and ask for
    asking price + source as a single threaded reply.
    """
    file_id    = event.get("file_id")
    user_id    = event.get("user_id")
    channel_id = event.get("channel_id")
    event_ts   = event.get("event_ts")

    if not file_id or not user_id:
        return

    try:
        file_info = client.files_info(file=file_id)
        file_data = file_info["file"]
    except Exception as exc:
        print(f"!!! FILE: Could not fetch file info: {exc}", flush=True)
        return

    # Only process image files
    mimetype = file_data.get("mimetype", "")
    if not mimetype.startswith("image/"):
        return

    file_url = file_data.get("url_private")
    if not file_url:
        return

    pending_scans[user_id] = {
        "file_url":   file_url,
        "channel_id": channel_id,
        "thread_ts":  event_ts,
    }

    if not vectors_loaded:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=event_ts,
            text="⏳ Still loading — try again in about 30 seconds.",
        )
        del pending_scans[user_id]
        return

    client.chat_postMessage(
        channel=channel_id,
        thread_ts=event_ts,
        text=(
            "Got it! Reply in this thread with the asking price and where it's from:\n"
            "`$XX.XX | Source`   e.g. `$25.00 | Facebook Marketplace`"
        ),
    )


# ---------------------------------------------------------------------------
# Slack event: message reply (price | source input)
# ---------------------------------------------------------------------------

@bolt_app.event("message")
def handle_message(event, client):
    """
    Listens for all messages.  Only acts when:
      - The sender has a pending scan in progress
      - The message is a threaded reply in the correct thread
      - The message is from a user (not the bot)
    """
    user_id   = event.get("user")
    thread_ts = event.get("thread_ts")
    text      = (event.get("text") or "").strip()

    # Ignore bot messages and non-replies
    if not user_id or event.get("bot_id") or not thread_ts:
        return

    if user_id not in pending_scans:
        return

    scan = pending_scans[user_id]

    # Only process replies to our specific thread
    if thread_ts != scan["thread_ts"]:
        return

    # Parse "price | source"
    asking_price, source = parse_price_source(text)

    if asking_price is None:
        client.chat_postMessage(
            channel=scan["channel_id"],
            thread_ts=scan["thread_ts"],
            text=(
                "Couldn't parse that — please reply with:\n"
                "`$XX.XX | Source`   e.g. `$25.00 | Facebook Marketplace`"
            ),
        )
        return

    # Clear pending state before spawning thread (prevent double-processing)
    del pending_scans[user_id]

    # Run analysis in a background thread so Slack doesn't time out
    threading.Thread(
        target=_run_manual_analysis,
        args=(scan["file_url"], scan["channel_id"], scan["thread_ts"],
              asking_price, source, client),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Manual analysis pipeline
# ---------------------------------------------------------------------------

def _run_manual_analysis(
    file_url:    str,
    channel_id:  str,
    thread_ts:   str,
    asking_price: float,
    source:      str,
    client,
) -> None:
    """Download the uploaded image, detect buttons, match, and post results."""

    def _reply(text: str) -> None:
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=text, mrkdwn=True
        )

    try:
        # Download image with Slack auth header
        import requests as req
        resp = req.get(
            file_url,
            headers={"Authorization": f"Bearer {_slack_token}"},
            timeout=20,
        )
        resp.raise_for_status()
        image_bytes = resp.content
    except Exception as exc:
        print(f"!!! MANUAL: Failed to download uploaded image: {exc}", flush=True)
        _reply("❌ Couldn't download the image — please try uploading again.")
        return

    # Detect button crops
    try:
        from . import image_proc as _ip   # lazy import
        crops = _ip.detect_and_crop(image_bytes)
    except Exception as exc:
        print(f"!!! MANUAL: detect_and_crop failed: {exc}", flush=True)
        _reply("❌ Couldn't detect buttons in that image.")
        return

    if not crops:
        _reply("No buttons detected in the image. Try a clearer or closer shot.")
        return

    # Match each crop
    matched: dict[tuple, dict] = {}   # (year, slogan) → enriched match
    unmatched_count = 0

    from . import clip_matcher as _cm   # lazy import (already loaded if CLIP ready)
    for crop in crops:
        try:
            match = _cm.match_crop(crop)
        except Exception as exc:
            print(f"!!! MANUAL: match_crop error: {exc}", flush=True)
            unmatched_count += 1
            continue

        if match is None:
            unmatched_count += 1
        else:
            key = (match["year"], match["slogan"])
            if key not in matched or match["overall"] > matched[key]["overall"]:
                matched[key] = match

    # Enrich with price data
    enriched_matches = []
    for (year, slogan), match in matched.items():
        price_single, price_year, notes, amount_needed = sheets_client.get_buy_decision(
            year, slogan, buy_rules
        )
        enriched = dict(match)
        enriched["max_price_single"] = price_single
        enriched["amount_needed"]    = amount_needed
        enriched_matches.append(enriched)

    # Calculate totals
    lot_value = sum(sheets_client.parse_price(m["max_price_single"]) for m in enriched_matches)
    margin    = lot_value - asking_price
    needed    = [m for m in enriched_matches if m["amount_needed"] > 0]

    # Format and post result
    _reply(format_manual_result(
        source=source,
        asking_price=asking_price,
        matches=enriched_matches,
        lot_value=lot_value,
        margin=margin,
        needed=needed,
        unmatched_count=unmatched_count,
    ))


def _format_manual_result(
    source:        str,
    asking_price:  float,
    matches:       list[dict],
    lot_value:     float,
    margin:        float,
    needed:        list[dict],
    unmatched_count: int,
) -> str:
    lines = [f"📸 *Lot Analysis — {source}*", f"Asking: *${asking_price:.2f}*", ""]

    if matches:
        lines.append("Matched buttons:")
        for m in matches:
            year   = m.get("year", "?")
            slogan = m.get("slogan", "?")
            price  = m.get("max_price_single", "")
            n      = m.get("amount_needed", 0)
            star   = f"  ⭐ need {n}" if n > 0 else ""
            lines.append(f"  • {year} — \"{slogan}\"    max: {price}{star}")
    else:
        lines.append("_No buttons identified with confidence._")

    if unmatched_count > 0:
        lines.append(f"_{unmatched_count} button(s) not identified with confidence._")

    lines.append("")

    if lot_value > 0:
        if margin > 0:
            verdict = f"✅ Good deal — *+${margin:.2f}* below calculated value"
        else:
            verdict = f"⚠️ You'd overpay by *${abs(margin):.2f}*"
        lines.append(
            f"Calculated value: *${lot_value:.2f}*  |  {verdict}"
        )
    else:
        lines.append("_Calculated value: $0.00 (no matched buttons have price rules)_")

    if needed:
        need_list = ", ".join(f"{m['year']} {m['slogan']}" for m in needed)
        lines.append(f"\n⭐ *Needed buttons in this lot:* {need_list}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/run-scan", methods=["POST"])
def run_scan():
    """
    Triggered by Cloud Scheduler for the daily eBay scan.
    Runs the scan in a background thread so Cloud Scheduler gets a quick 200.
    """
    threading.Thread(target=_run_daily_scan, daemon=True).start()
    return jsonify({"status": "scan started"}), 200


@flask_app.route("/health", methods=["GET"])
def health():
    # Always return 200 so Cloud Run health probes don't kill the container
    # during the 30-60s CLIP hydration window.
    if not vectors_loaded:
        return "OK - hydrating", 200
    return "OK - ready", 200


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
    # Account deletion notification — acknowledge immediately, then log.
    # ebayscout does not persist eBay user personal data, so no deletion is
    # needed.  We log the userId for audit purposes only.
    try:
        payload  = request.get_json(silent=True) or {}
        notif    = payload.get("notification", {})
        data     = notif.get("data", {})
        user_id  = data.get("userId", "unknown")
        notif_id = notif.get("notificationId", "unknown")
        print(
            f">>> EBAY DELETION: Notification received — "
            f"notificationId={notif_id} userId={user_id}",
            flush=True,
        )
    except Exception as exc:
        print(f"!!! EBAY DELETION: Error parsing notification body: {exc}", flush=True)

    return "", 200


# ---------------------------------------------------------------------------
# Daily scan (called from /run-scan endpoint)
# ---------------------------------------------------------------------------

def _run_daily_scan() -> None:
    """
    Runs the full eBay + Etsy scan pipeline, reusing the already-loaded
    buy_rules and clip_matcher state rather than re-initialising.
    """
    from . import ebay_client, seen_items as seen_store

    print(">>> SCAN: Daily scan starting (eBay + Etsy)...", flush=True)

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
            print(f">>> SCAN: eBay returned {len(ebay_listings)} listings.", flush=True)
        except Exception as exc:
            print(f"!!! SCAN: eBay query failed: {exc}", flush=True)
    else:
        print(">>> SCAN: Skipping eBay (no EBAY_APP_ID / EBAY_CERT_ID).", flush=True)

    # Etsy listings
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

    if not all_listings:
        print(">>> SCAN: No listings retrieved from any source — exiting.", flush=True)
        return

    new_listings = [l for l in all_listings if seen_store.is_new(l["item_id"], seen)]
    ebay_new     = sum(1 for l in new_listings if not l["item_id"].startswith("etsy_"))
    etsy_new     = sum(1 for l in new_listings if     l["item_id"].startswith("etsy_"))
    print(f">>> SCAN: {len(new_listings)} new listings to process.", flush=True)

    stat_alerted        = 0
    stat_low_confidence = 0
    stat_rejected       = 0

    from . import image_proc as _ip   # lazy — torch/cv2 imported here if not yet
    from . import clip_matcher as _cm  # lazy — torch/clip imported here if not yet

    for listing in new_listings:
        item_id = listing["item_id"]
        asking  = listing.get("current_price", 0.0)

        try:
            if item_id.startswith("etsy_"):
                picture_urls = [listing["gallery_url"]] if listing.get("gallery_url") else []
            elif ebay_app_id and ebay_cert_id:
                picture_urls = ebay_client.get_item_pictures(ebay_app_id, ebay_cert_id, item_id)
                if not picture_urls and listing.get("gallery_url"):
                    picture_urls = [listing["gallery_url"]]
            else:
                picture_urls = [listing["gallery_url"]] if listing.get("gallery_url") else []

            matched: dict[tuple, dict] = {}
            best_score_seen = 0.0
            photos_processed = 0

            for photo_url in picture_urls[: config.MAX_PHOTOS_PER_LISTING]:
                try:
                    image_bytes = _ip.download_image(photo_url)
                    crops       = _ip.detect_and_crop(image_bytes)
                except Exception as exc:
                    print(f"!!! SCAN: Photo processing failed: {exc}", flush=True)
                    continue

                photos_processed += 1

                for crop in crops:
                    try:
                        match = _cm.match_crop(
                            crop, threshold=config.REJECTION_THRESHOLD
                        )
                    except Exception:
                        continue
                    if match is None:
                        continue
                    best_score_seen = max(best_score_seen, match["overall"])
                    if match["overall"] >= config.CONFIDENCE_THRESHOLD:
                        key = (match["year"], match["slogan"])
                        if key not in matched or match["overall"] > matched[key]["overall"]:
                            matched[key] = match

            if photos_processed == 0 or best_score_seen < config.REJECTION_THRESHOLD:
                stat_rejected += 1
                seen_store.mark_seen(item_id, seen)
                continue

            if not matched:
                stat_low_confidence += 1
                seen_store.mark_seen(item_id, seen)
                continue

            enriched_matches = []
            for (year, slogan), match in matched.items():
                price_single, _, _, amount_needed = sheets_client.get_buy_decision(
                    year, slogan, buy_rules
                )
                enriched = dict(match)
                enriched["max_price_single"] = price_single
                enriched["amount_needed"]    = amount_needed
                enriched_matches.append(enriched)

            lot_value    = sum(sheets_client.parse_price(m["max_price_single"]) for m in enriched_matches)
            margin       = lot_value - asking
            needed_found = [m for m in enriched_matches if m["amount_needed"] > 0]
            listing_alerted = False

            if margin > 0:
                if config.DRY_RUN:
                    print(f"    [DRY RUN] Would post undervalued alert for {item_id}", flush=True)
                else:
                    notifier.send_undervalued_alert(
                        slack_token=_slack_token,
                        channel=_channel_id,
                        listing=listing,
                        matches=enriched_matches,
                        lot_value=lot_value,
                        asking_price=asking,
                        margin=margin,
                        unmatched_count=0,
                    )
                listing_alerted = True

            if needed_found:
                if config.DRY_RUN:
                    print(f"    [DRY RUN] Would post needed-buttons alert for {item_id}", flush=True)
                else:
                    notifier.send_needed_alert(
                        slack_token=_slack_token,
                        channel=_channel_id,
                        listing=listing,
                        needed_buttons=needed_found,
                        asking_price=asking,
                        lot_value=lot_value,
                    )
                listing_alerted = True

            if listing_alerted:
                stat_alerted += 1
            else:
                stat_low_confidence += 1

        except Exception as exc:
            print(f"!!! SCAN: Error processing {item_id}: {exc}", flush=True)
            traceback.print_exc()

        seen_store.mark_seen(item_id, seen)

    if config.DRY_RUN:
        print("[DRY RUN] Skipping save_seen().", flush=True)
    elif not seen_store.save_seen(seen):
        notifier.send_warning(_slack_token, _channel_id,
                              "Failed to save seen_items.json — next scan may re-alert.")

    if config.DRY_RUN:
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

    print(">>> SCAN: Daily scan complete.", flush=True)


# ---------------------------------------------------------------------------
# Startup (called from gunicorn post_fork hook)
# ---------------------------------------------------------------------------

def startup() -> None:
    """Load Google Sheets and CLIP model in the background."""
    global buy_rules, vectors_loaded

    print(">>> STARTUP: Loading buy rules...", flush=True)
    try:
        sheets_json    = _get_secret("GOOGLE_SHEETS_JSON")
        spreadsheet_id = _get_secret("SPREADSHEET_ID")
        buy_rules      = sheets_client.load_buy_rules(sheets_json, spreadsheet_id)
    except Exception as exc:
        print(f"!!! STARTUP: Sheets error: {exc}", flush=True)

    def _hydrate():
        global vectors_loaded
        try:
            from . import clip_matcher as cm   # lazy: torch+clip imported here
            cm.init(config.BUCKET_NAME)
            vectors_loaded = True
            print(">>> STARTUP: CLIP ready.", flush=True)
        except Exception as exc:
            print(f"!!! STARTUP: CLIP init failed: {exc}", flush=True)
            traceback.print_exc()

    threading.Thread(target=_hydrate, daemon=True).start()


