"""
ebayscout/seen_items.py

GCS-backed deduplication store.

seen_items.json lives at SEEN_ITEMS_BLOB in the shared GCS bucket.
Structure: {"item_id": "YYYY-MM-DD", ...}

All functions mutate / read the in-memory `seen` dict; call save_seen()
once at the end of a successful job run.
"""

import json
from datetime import date

from google.cloud import storage

from . import config


def load_seen(bucket_name: str = config.BUCKET_NAME) -> dict[str, str]:
    """
    Download and parse seen_items.json from GCS.
    Returns {} if the blob does not exist yet (first run).
    """
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob   = bucket.blob(config.SEEN_ITEMS_BLOB)

        if not blob.exists():
            print(">>> SEEN: seen_items.json not found — starting fresh.", flush=True)
            return {}

        data = json.loads(blob.download_as_text())
        print(f">>> SEEN: Loaded {len(data)} previously seen item IDs.", flush=True)
        return data

    except Exception as exc:
        print(f"!!! SEEN: Failed to load seen_items.json: {exc}", flush=True)
        print("!!! SEEN: Proceeding with empty seen set (may re-alert on old listings).", flush=True)
        return {}


def save_seen(seen: dict[str, str], bucket_name: str = config.BUCKET_NAME) -> bool:
    """
    Upload the updated seen dict to GCS as JSON.
    Returns True on success, False on failure.
    """
    try:
        client  = storage.Client()
        bucket  = client.bucket(bucket_name)
        blob    = bucket.blob(config.SEEN_ITEMS_BLOB)
        payload = json.dumps(seen, indent=2)
        blob.upload_from_string(payload, content_type="application/json")
        print(f">>> SEEN: Saved {len(seen)} item IDs to GCS.", flush=True)
        return True

    except Exception as exc:
        print(f"!!! SEEN: Failed to save seen_items.json: {exc}", flush=True)
        return False


def load_hunt_ids(bucket_name: str = config.BUCKET_NAME) -> list[str]:
    """
    Load the ID-hunt list (a JSON array of eBay item_ids) from GCS.

    Returns [] if the blob does not exist (hunting is simply skipped then).
    These are specific known IDs — e.g. recovered from a prior run's logs — that
    the scan fetches directly by ID to rebuild full market data for each.
    """
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob   = bucket.blob(config.HUNT_IDS_BLOB)

        if not blob.exists():
            print(">>> HUNT: hunt_ids.json not found — nothing to hunt.", flush=True)
            return []

        data = json.loads(blob.download_as_text())
        ids  = [str(i) for i in data if i]
        print(f">>> HUNT: Loaded {len(ids)} item IDs to hunt.", flush=True)
        return ids

    except Exception as exc:
        print(f"!!! HUNT: Failed to load hunt_ids.json: {exc}", flush=True)
        return []


def append_scan_log(
    records: list[dict],
    bucket_name: str = config.BUCKET_NAME,
) -> bool:
    """
    Append per-listing scan records (one JSON object per line) to the scan-log
    blob in GCS. GCS has no native append, so we read the existing blob and
    re-upload it with the new lines added. Called once at the end of a scan.

    This is groundwork data for a future automated undervalued-lot valuer;
    a failure here is non-fatal to the scan. Returns True on success.
    """
    if not records:
        return True
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob   = bucket.blob(config.SCAN_LOG_BLOB)

        existing = blob.download_as_text() if blob.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        new_lines = "".join(json.dumps(r) + "\n" for r in records)
        blob.upload_from_string(existing + new_lines, content_type="application/x-ndjson")
        print(f">>> SCAN LOG: Appended {len(records)} records to {config.SCAN_LOG_BLOB}.", flush=True)
        return True
    except Exception as exc:
        print(f"!!! SCAN LOG: Failed to append {len(records)} records: {exc}", flush=True)
        return False


def ondemand2_first_run_done(bucket_name: str = config.BUCKET_NAME) -> bool:
    """Return True if /crawl500 has already completed its first run.

    On the first run /crawl500 may re-scan already-seen lots to reach its 500
    cap; every run after processes only unseen lots. State lives in a tiny GCS
    marker (ONDEMAND2_STATE_BLOB). Missing/unreadable marker → treat as first run.
    """
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob   = bucket.blob(config.ONDEMAND2_STATE_BLOB)
        if not blob.exists():
            return False
        data = json.loads(blob.download_as_text())
        return bool(data.get("first_run_done", False))
    except Exception as exc:
        print(f"!!! OD2: Failed to read {config.ONDEMAND2_STATE_BLOB}: {exc}", flush=True)
        return False


def mark_ondemand2_first_run_done(bucket_name: str = config.BUCKET_NAME) -> bool:
    """Persist that /crawl500 has completed its first run (idempotent)."""
    try:
        client  = storage.Client()
        bucket  = client.bucket(bucket_name)
        blob    = bucket.blob(config.ONDEMAND2_STATE_BLOB)
        payload = json.dumps({"first_run_done": True,
                              "updated": date.today().isoformat()}, indent=2)
        blob.upload_from_string(payload, content_type="application/json")
        print(f">>> OD2: Marked first run done in {config.ONDEMAND2_STATE_BLOB}.", flush=True)
        return True
    except Exception as exc:
        print(f"!!! OD2: Failed to write {config.ONDEMAND2_STATE_BLOB}: {exc}", flush=True)
        return False


def is_new(item_id: str, seen: dict[str, str]) -> bool:
    """Return True if item_id has not been processed before."""
    return item_id not in seen


def mark_seen(
    item_id: str,
    seen: dict[str, str],
    date_str: str | None = None,
) -> None:
    """
    Record item_id in the seen dict (in-place).
    Uses today's ISO date string if date_str is not provided.
    """
    seen[item_id] = date_str or date.today().isoformat()
