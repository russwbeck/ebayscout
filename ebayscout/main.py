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

import contextlib
import datetime
import json
import os
import re
import secrets
import threading
import time
import traceback

from flask import Flask, request, jsonify
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from slack_sdk import WebClient
from google.cloud import secretmanager

from . import config
from . import sheets_client
from . import notifier
from . import etsy_client
from .utils import (
    parse_price_source,
    format_manual_result,
    extract_years,
    extract_decades,
    needed_years,
    build_year_queries,
    era_year_set,
    parse_confirmation,
    other_era,
    is_non_alerting_slogan,
    extract_lot_count,
    dedup_listings,
)

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

# Held for the duration of a daily scan so an overlapping trigger can't start
# a second concurrent run.
_scan_lock = threading.Lock()

# Guards a wake-up (synchronous CLIP load) so concurrent slash-command /
# file-upload triggers don't each spawn a loader thread. clip_matcher.init()
# is itself lock-guarded and idempotent, but this avoids N redundant threads
# (and N "waking up" log lines) while a load is already in flight.
_wake_lock      = threading.Lock()
_wake_in_flight = False

# Per-instance secret guarding the internal manual-analysis endpoint. The Slack
# event handler fires a self-HTTP-POST to /internal/manual-analysis so the heavy
# CLIP work runs inside an in-flight request (Cloud Run keeps CPU allocated for
# the request's duration) instead of a background thread (throttled to ~0%).
# Caller and handler share this process (workers=1, --max-instances=1), so the
# token always matches for legit self-calls and is unguessable to outsiders.
_INTERNAL_TOKEN = secrets.token_urlsafe(32)


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
    thread.  This is the single load path shared by /run-scan, /test-clip, the
    /scout slash command, and the manual upload flow.
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


def _wake_and_notify(
    client,
    channel_id: str,
    thread_ts:  str | None,
    on_ready_text: str,
) -> None:
    """
    Load CLIP in this (background) thread, deduplicating concurrent wakes, then
    post a confirmation.  Intended as the target of threading.Thread(...).start().

    Only the first concurrent caller actually loads; a second simultaneous wake
    returns quietly (the in-flight load owns the confirmation message) so the
    channel doesn't get duplicate "ready" posts.
    """
    global _wake_in_flight

    if vectors_loaded:
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text="✅ eBay Scout is already awake and ready.",
        )
        return

    with _wake_lock:
        already = _wake_in_flight
        if not already:
            _wake_in_flight = True

    if already:
        print(">>> WAKE: load already in flight — skipping duplicate.", flush=True)
        return

    try:
        ok = _ensure_clip_loaded()
    finally:
        with _wake_lock:
            _wake_in_flight = False

    if ok:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                text=on_ready_text)
    else:
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text="❌ eBay Scout failed to wake up — check the logs and try again.",
        )


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
        "stage":      "await_price",
        "file_url":   file_url,
        "channel_id": channel_id,
        "thread_ts":  event_ts,
    }

    if not vectors_loaded:
        # Self-heal: keep the pending scan and kick off a background wake.  The
        # user's price reply lands in handle_message and runs the analysis once
        # CLIP is ready (_run_manual_analysis force-loads as a backstop).  This
        # replaces the old dead-end that deleted the scan and looped "still
        # loading" forever on a cold, CPU-throttled container.
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=event_ts,
            text=(
                "⏳ Waking up eBay Scout (~60s). I've saved your photo — reply "
                "in this thread with the asking price and source and I'll "
                "analyze it as soon as I'm ready:\n"
                "`$XX.XX | Source`   e.g. `$25.00 | Facebook Marketplace`\n"
                "For lots with many buttons, add the count:\n"
                "`$25.00 | Facebook Marketplace | 35`"
            ),
        )
        threading.Thread(
            target=_wake_and_notify,
            args=(client, channel_id, event_ts,
                  "✅ eBay Scout is awake — reply with the price/source now "
                  "if you haven't already."),
            daemon=True,
        ).start()
        return

    client.chat_postMessage(
        channel=channel_id,
        thread_ts=event_ts,
        text=(
            "Got it! Reply in this thread with the asking price and where it's from:\n"
            "`$XX.XX | Source`   e.g. `$25.00 | Facebook Marketplace`\n"
            "For lots with many buttons, add the count:\n"
            "`$25.00 | Facebook Marketplace | 35`"
        ),
    )


# ---------------------------------------------------------------------------
# Slack event: message reply (price | source input)
# ---------------------------------------------------------------------------

@bolt_app.event("message")
def handle_message(event, client):
    """
    Two-stage manual flow, keyed on pending_scans[user_id]["stage"]:
      await_price   → user sends "$price | source [| count]"; kick off a preview
                      (detect count + guess era) and move to await_confirm.
      previewing    → ignore stray replies while the preview runs.
      await_confirm → user replies `go` / a count / an era / both; run the match
                      with the confirmed count + era restriction.
    """
    user_id = event.get("user")
    text    = (event.get("text") or "").strip()

    if not user_id or event.get("bot_id") or event.get("subtype") == "file_share":
        return
    if user_id not in pending_scans:
        return

    scan  = pending_scans[user_id]
    stage = scan.get("stage", "await_price")

    if stage == "previewing":
        return  # preview in progress; ignore until it posts the confirm prompt

    # ----------------------------------------------------------------- confirm
    if stage == "await_confirm":
        count_override, era_override = parse_confirmation(text)
        final_count = count_override if count_override is not None else scan.get("detected_count")
        era_label   = era_override   if era_override   is not None else scan.get("era_guess")
        restrict    = sorted(era_year_set(era_label, config.BUTTON_ERAS)) if era_label else []
        overrode_era = era_override is not None and era_override != scan.get("era_guess")
        print(
            f">>> ERA: user confirm era={era_label or 'all'}"
            f"{' OVERRIDE' if overrode_era else ''} (guess={scan.get('era_guess')}) "
            f"count={final_count} (detected={scan.get('detected_count')})",
            flush=True,
        )
        del pending_scans[user_id]
        threading.Thread(
            target=_dispatch_internal,
            args=({
                "mode":          "full",
                "file_url":      scan["file_url"],
                "channel_id":    scan["channel_id"],
                "thread_ts":     scan["thread_ts"],
                "asking_price":  scan["asking_price"],
                "source":        scan["source"],
                "button_count":  final_count,
                "restrict_years": restrict,
                "era_label":     era_label,
                "era_ranked":    scan.get("era_ranked", []),
            },),
            daemon=False,
        ).start()
        return

    # --------------------------------------------------------------- await_price
    asking_price, source, button_count = parse_price_source(text)
    if asking_price is None:
        client.chat_postMessage(
            channel=scan["channel_id"],
            thread_ts=scan["thread_ts"],
            text=(
                "Couldn't parse that — please reply with:\n"
                "`$XX.XX | Source`   e.g. `$25.00 | Facebook Marketplace`\n"
                "If it's a lot with many buttons, add the count:\n"
                "`$25.00 | Facebook Marketplace | 35`"
            ),
        )
        return

    # Move to "previewing" so stray replies are ignored until the preview posts
    # its confirm prompt (which overwrites the state with stage=await_confirm).
    pending_scans[user_id] = {**scan, "stage": "previewing"}
    threading.Thread(
        target=_dispatch_internal,
        args=({
            "mode":         "preview",
            "file_url":     scan["file_url"],
            "channel_id":   scan["channel_id"],
            "thread_ts":    scan["thread_ts"],
            "user_id":      user_id,
            "asking_price": asking_price,
            "source":       source,
            "count_hint":   button_count,
        },),
        daemon=False,
    ).start()


def _dispatch_internal(payload: dict) -> None:
    """
    POST `payload` to this service's own /internal/manual-analysis endpoint so
    the heavy work runs inside an in-flight HTTP request (full CPU on Cloud Run)
    rather than a throttled background thread. Must use the external HTTPS URL —
    a localhost call bypasses the load balancer and does NOT prevent throttling
    (CLOUD_RUN_CPU_THROTTLE_FIX.md).

    Inline fallback runs ONLY when the work definitely did not start (couldn't
    reach the server, or a non-200 rejection). On a read timeout the server-side
    work is still running, so we must NOT re-run it — that would double-post.
    """
    import requests as req

    # Brief pause so the Slack-ack request releases its gunicorn worker first.
    time.sleep(0.3)

    try:
        resp = req.post(
            f"{config.SERVICE_BASE_URL}/internal/manual-analysis",
            json=payload,
            headers={"X-Internal-Token": _INTERNAL_TOKEN},
            timeout=1790,   # < Cloud Run --timeout=1800, ~10s headroom
        )
    except (req.exceptions.ConnectionError, req.exceptions.ConnectTimeout) as exc:
        print(f"!!! MANUAL: could not reach internal endpoint ({exc}) — running inline.", flush=True)
        _run_internal(payload)
        return
    except Exception as exc:
        print(f"!!! MANUAL: internal dispatch read ended early ({exc}); "
              f"server-side work is still running — not retrying.", flush=True)
        return

    if resp.status_code != 200:
        print(f"!!! MANUAL: internal dispatch returned {resp.status_code} — running inline.", flush=True)
        _run_internal(payload)


def _run_internal(data: dict) -> None:
    """Dispatch an internal payload to the preview or full pipeline."""
    if data.get("mode") == "preview":
        _run_manual_preview(
            file_url=data["file_url"],
            channel_id=data["channel_id"],
            thread_ts=data["thread_ts"],
            user_id=data["user_id"],
            asking_price=float(data["asking_price"]),
            source=data.get("source", "Unknown"),
            count_hint=data.get("count_hint"),
        )
    else:
        restrict = set(data["restrict_years"]) if data.get("restrict_years") else None
        _run_manual_analysis(
            file_url=data["file_url"],
            channel_id=data["channel_id"],
            thread_ts=data["thread_ts"],
            asking_price=float(data["asking_price"]),
            source=data.get("source", "Unknown"),
            button_count=data.get("button_count"),
            restrict_years=restrict,
            era_label=data.get("era_label"),
            era_ranked=data.get("era_ranked", []),
            feedback_round=bool(data.get("feedback_round")),
        )


# ---------------------------------------------------------------------------
# Slash command: /scout — wake the bot for manual mode
# ---------------------------------------------------------------------------

@bolt_app.command("/scout")
def handle_scout_wake(ack, command, client):
    """
    Force a synchronous CLIP load so manual uploads work on a cold (CPU-
    throttled) Cloud Run container.

    Slack requires the command to be acknowledged within 3 s, but CLIP load
    takes 30-60 s.  So: ack immediately with a "waking up" message, then load
    in a background thread (wrapped in _keep_cpu_hot via _wake_and_notify) and
    post a "ready" confirmation when done.
    """
    channel_id = command.get("channel_id")

    if vectors_loaded:
        ack("✅ eBay Scout is already awake — upload a photo any time.")
        return

    ack("⏳ Waking up eBay Scout… this takes about 60 seconds. "
        "I'll post here when it's ready.")

    threading.Thread(
        target=_wake_and_notify,
        args=(client, channel_id, None,
              "✅ eBay Scout is awake. Upload your photo now."),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Manual analysis pipeline
# ---------------------------------------------------------------------------

def _download_slack_image(file_url: str):
    """Download a Slack file_private URL with the bot token. Returns bytes or None."""
    try:
        import requests as req
        resp = req.get(
            file_url,
            headers={"Authorization": f"Bearer {_slack_token}"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        print(f"!!! MANUAL: Failed to download uploaded image: {exc}", flush=True)
        return None


def _run_manual_preview(
    file_url:     str,
    channel_id:   str,
    thread_ts:    str,
    user_id:      str,
    asking_price: float,
    source:       str,
    count_hint:   int | None = None,
) -> None:
    """
    Stage 1 of the manual flow: detect the button count and guess the lot era,
    then post a confirmation prompt and park the request in `await_confirm`.
    Runs inside the internal request (full CPU). Heavily logs era detection.
    """
    client = WebClient(token=_slack_token)

    def _reply(text: str) -> None:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text, mrkdwn=True)

    with _keep_cpu_hot():
        if not _ensure_clip_loaded():
            _reply("❌ eBay Scout is still waking up — try replying again in ~30s.")
            pending_scans.pop(user_id, None)
            return

        image_bytes = _download_slack_image(file_url)
        if image_bytes is None:
            _reply("❌ Couldn't download the image — please try uploading again.")
            pending_scans.pop(user_id, None)
            return

        try:
            from . import image_proc as _ip
            crops = _ip.detect_and_crop(image_bytes, button_count=count_hint)
        except Exception as exc:
            print(f"!!! MANUAL: preview detect_and_crop failed: {exc}", flush=True)
            _reply("❌ Couldn't detect buttons in that image.")
            pending_scans.pop(user_id, None)
            return

        if not crops:
            _reply("No buttons detected in the image. Try a clearer or closer shot.")
            pending_scans.pop(user_id, None)
            return

        count = len(crops)
        from . import clip_matcher as _cm
        try:
            era_guess, detail = _cm.guess_lot_era(crops)
        except Exception as exc:
            print(f"!!! MANUAL: era guess failed: {exc}", flush=True)
            era_guess, detail = None, {}

        # Eras ranked by vote (best first) — used to offer "the other era" later.
        era_ranked = [e for e, _ in sorted(
            detail.get("votes", {}).items(), key=lambda kv: kv[1], reverse=True)]

        # Heavy, greppable era logging — filter Cloud Logging for "ERA:".
        print(
            f">>> ERA: preview guess={era_guess} votes={detail.get('votes')} "
            f"ranked={era_ranked} sampled={detail.get('sampled')}/{detail.get('total')} "
            f"detected_count={count} price=${asking_price:.2f} src={source}",
            flush=True,
        )
        for i, pc in enumerate(detail.get("per_crop", [])):
            print(f">>> ERA: preview crop {i} -> {pc['pick']} {pc['scores']}", flush=True)

        pending_scans[user_id] = {
            "stage":          "await_confirm",
            "file_url":       file_url,
            "channel_id":     channel_id,
            "thread_ts":      thread_ts,
            "asking_price":   asking_price,
            "source":         source,
            "detected_count": count,
            "era_guess":      era_guess,
            "era_ranked":     era_ranked,
        }

        era_txt = f"*{era_guess}*" if era_guess else "_uncertain_"
        _reply(
            f"📸 Found *{count}* buttons. Era looks like {era_txt}.\n"
            f"Reply *`go`* to analyze, or correct it — a count, an era "
            f"(`CCB` / `Mellon` / `Citizens` / `all`), or both (e.g. `mellon 42`)."
        )


def _run_manual_analysis(
    file_url:     str,
    channel_id:   str,
    thread_ts:    str,
    asking_price: float,
    source:       str,
    button_count: int | None = None,
    restrict_years: set[int] | None = None,
    era_label:    str | None = None,
    era_ranked:   list | None = None,
    feedback_round: bool = False,
) -> None:
    """
    Stage 2: download, detect, match (optionally year/era-restricted), post the
    lot analysis. When an era was applied (and this isn't itself a re-run), also
    posts a "Did I identify the era correctly? Yes/No" prompt — No re-runs with
    the other era; Yes just records positive feedback.

    Intended to run inside the /internal/manual-analysis HTTP request (not a
    background thread) so Cloud Run keeps full CPU allocated for the CLIP encode
    — a background thread is throttled to ~0% (see CLAUDE.md / DECISIONS.md #5).
    """
    client = WebClient(token=_slack_token)

    def _reply(text: str) -> None:
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=text, mrkdwn=True
        )

    with _keep_cpu_hot():
        # Backstop: if the user replied before the background wake finished,
        # force-load CLIP here (idempotent) so match_crops_batch never runs
        # against an uninitialized model.
        if not _ensure_clip_loaded():
            _reply("❌ eBay Scout is still waking up — try replying again in ~30s.")
            return

        image_bytes = _download_slack_image(file_url)
        if image_bytes is None:
            _reply("❌ Couldn't download the image — please try uploading again.")
            return

        # Detect button crops
        try:
            from . import image_proc as _ip   # lazy import
            crops = _ip.detect_and_crop(image_bytes, button_count=button_count)
        except Exception as exc:
            print(f"!!! MANUAL: detect_and_crop failed: {exc}", flush=True)
            _reply("❌ Couldn't detect buttons in that image.")
            return

        if not crops:
            _reply("No buttons detected in the image. Try a clearer or closer shot.")
            return

        _restrict_txt = ("none" if not restrict_years
                         else f"{min(restrict_years)}-{max(restrict_years)}")
        print(
            f">>> ERA: manual match era={era_label or 'all'} restrict={_restrict_txt} "
            f"count={button_count} crops={len(crops)}",
            flush=True,
        )

        # Match each crop
        matched: dict[tuple, dict] = {}   # (year, slogan) → enriched match
        unmatched_count = 0

        from . import clip_matcher as _cm   # lazy import (already loaded if CLIP ready)
        try:
            batch_results = _cm.match_crops_batch(crops, restrict_years=restrict_years)
        except Exception as exc:
            print(f"!!! MANUAL: match_crops_batch error: {exc}", flush=True)
            batch_results = [None] * len(crops)

        for match in batch_results:
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

        # Era feedback prompt — only when a specific era was applied and this
        # isn't already a re-run (avoids loops). Posted as a separate message so
        # the (possibly long) analysis text isn't constrained by block limits.
        if era_label and era_label.lower() != "all" and not feedback_round:
            alt_era = other_era(era_label, era_ranked or [], config.BUTTON_ERAS)
            _post_era_feedback(client, channel_id, thread_ts, {
                "file_url":     file_url,
                "channel_id":   channel_id,
                "thread_ts":    thread_ts,
                "asking_price": asking_price,
                "source":       source,
                "button_count": button_count,
                "era_used":     era_label,
                "alt_era":      alt_era,
            })


def _post_era_feedback(client, channel_id: str, thread_ts: str, ctx: dict) -> None:
    """Post the 'Did I identify the era correctly?' Yes/No prompt."""
    value = json.dumps(ctx)
    elements = [{
        "type": "button",
        "text": {"type": "plain_text", "text": "✅ Yes"},
        "action_id": "era_feedback_yes",
        "value": value,
    }]
    if ctx.get("alt_era"):
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": f"❌ No — try {ctx['alt_era']}"},
            "action_id": "era_feedback_no",
            "style": "danger",
            "value": value,
        })
    client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=f"Did I identify the era correctly? (used *{ctx['era_used']}*)",
        blocks=[
            {"type": "section",
             "text": {"type": "mrkdwn",
                      "text": f"Did I identify the era correctly? (used *{ctx['era_used']}*)"}},
            {"type": "actions", "elements": elements},
        ],
    )


@bolt_app.action("era_feedback_yes")
def handle_era_feedback_yes(ack, body, action, client):
    """User confirmed the era — record positive labelled feedback, no re-run."""
    ack()
    try:
        ctx = json.loads(action.get("value") or "{}")
    except Exception:
        ctx = {}
    print(f">>> ERA: feedback CORRECT era={ctx.get('era_used')} "
          f"src={ctx.get('source')} count={ctx.get('button_count')}", flush=True)
    _resolve_feedback_message(body, client, f"✅ Era confirmed: *{ctx.get('era_used')}* — thanks!")


@bolt_app.action("era_feedback_no")
def handle_era_feedback_no(ack, body, action, client):
    """User said the era was wrong — re-run the analysis with the other era."""
    ack()
    try:
        ctx = json.loads(action.get("value") or "{}")
    except Exception:
        ctx = {}
    alt = ctx.get("alt_era")
    print(f">>> ERA: feedback WRONG era={ctx.get('era_used')} -> re-running as {alt} "
          f"src={ctx.get('source')} count={ctx.get('button_count')}", flush=True)
    if not alt:
        _resolve_feedback_message(body, client, "⚠️ No other era to try — reply with one (`mellon` / `citizens` / `all`).")
        return
    _resolve_feedback_message(body, client, f"🔁 Re-running as *{alt}*…")
    threading.Thread(
        target=_dispatch_internal,
        args=({
            "mode":          "full",
            "file_url":      ctx["file_url"],
            "channel_id":    ctx["channel_id"],
            "thread_ts":     ctx["thread_ts"],
            "asking_price":  ctx["asking_price"],
            "source":        ctx.get("source", "Unknown"),
            "button_count":  ctx.get("button_count"),
            "restrict_years": sorted(era_year_set(alt, config.BUTTON_ERAS)),
            "era_label":     alt,
            "era_ranked":    [],
            "feedback_round": True,   # the re-run won't post another Yes/No
        },),
        daemon=False,
    ).start()


def _resolve_feedback_message(body, client, text: str) -> None:
    """Replace the Yes/No prompt with a resolution note (removes the buttons)."""
    try:
        container = body.get("container", {}) or {}
        channel = (body.get("channel", {}) or {}).get("id") or container.get("channel_id")
        ts = container.get("message_ts")
        if channel and ts:
            client.chat_update(channel=channel, ts=ts, text=text, blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": text}}])
    except Exception as exc:
        print(f"!!! ERA: could not update feedback message: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/internal/manual-analysis", methods=["POST"])
def internal_manual_analysis():
    """
    Run a manual lot analysis synchronously, inside this request, so Cloud Run
    keeps CPU allocated for the full CLIP encode (the fix for the ~minutes-long
    throttled analyses we saw from the old background-thread approach).

    Called only by this service itself (_dispatch_manual_analysis) and guarded
    by a per-instance token so the publicly-reachable route can't be abused.
    """
    if request.headers.get("X-Internal-Token") != _INTERNAL_TOKEN:
        return "forbidden", 403

    data = request.get_json(silent=True) or {}
    try:
        _run_internal(data)
    except Exception as exc:
        print(f"!!! MANUAL: internal {data.get('mode', 'full')} failed: {exc}", flush=True)
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(exc)}), 500

    return jsonify({"status": "ok"}), 200


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
    """
    global buy_rules

    def _truthy(v: str | None) -> bool:
        return (v or "").strip().lower() in ("1", "true", "yes", "on")

    ignore_seen   = _truthy(request.args.get("ignore_seen"))
    year_crawl    = _truthy(request.args.get("year_crawl"))
    era_crawl     = _truthy(request.args.get("era_crawl"))
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
                                    limit=limit)
    finally:
        _scan_lock.release()

    return jsonify({
        "status":      "scan complete",
        "ignore_seen": ignore_seen,
        "year_crawl":  year_crawl,
        "era_crawl":   era_crawl,
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
    """Build one JSONL scan-log record for a processed listing."""
    return {
        "ts":            datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "item_id":       listing.get("item_id", ""),
        "title":         listing.get("title", ""),
        "listing_url":   listing.get("listing_url", ""),
        "seller":        listing.get("seller", ""),
        "asking":        listing.get("current_price", 0.0),
        "photos_scored": photos_processed,
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
    else:
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
                        # the buy logic / /scout valuation but must not trigger
                        # scan alerts — they over-match and inflate lot value.
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


# ---------------------------------------------------------------------------
# Startup (called from gunicorn post_fork hook)
# ---------------------------------------------------------------------------

def startup() -> None:
    """Load Google Sheets and CLIP model in the background."""
    global buy_rules

    print(">>> STARTUP: Loading buy rules...", flush=True)
    try:
        sheets_json    = _get_secret("GOOGLE_SHEETS_JSON")
        spreadsheet_id = _get_secret("SPREADSHEET_ID")
        buy_rules      = sheets_client.load_buy_rules(sheets_json, spreadsheet_id)
    except Exception as exc:
        print(f"!!! STARTUP: Sheets error: {exc}", flush=True)

    # Hydrate CLIP in the background.  On a cold, CPU-throttled container this
    # may not finish until an HTTP request (a scan, /scout, or an upload)
    # provides CPU — those paths all call _ensure_clip_loaded() to force it.
    threading.Thread(target=_ensure_clip_loaded, daemon=True).start()


