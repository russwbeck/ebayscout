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
