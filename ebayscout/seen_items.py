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

from . import config, scan_log as scan_log_util


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
    Append per-listing scan records (one JSON object per line) to the scan log
    in GCS.  GCS has no native append, so each partition is read and re-uploaded
    with its new lines added — which is why the log is partitioned by month
    (scan_log.py, config.SCAN_LOG_PREFIX): the read-modify-write then costs one
    month rather than the whole history, and a year of daily feeds stops making
    every lot pay for every lot before it.

    An ordinary write is one lot's record and touches exactly one partition.  A
    checkpointed backfill can span months; each month is written separately, and
    a month that fails does not stop the others — a partial log is worth more
    than none.

    This is groundwork data for a future automated undervalued-lot valuer; a
    failure here is non-fatal to the scan. Returns True only if every partition
    was written.
    """
    if not records:
        return True
    ok = True
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
    except Exception as exc:
        print(f"!!! SCAN LOG: Failed to append {len(records)} records: {exc}", flush=True)
        return False
    for month, month_records in scan_log_util.partition(records).items():
        name = scan_log_util.blob_name(month, config.SCAN_LOG_PREFIX)
        try:
            blob     = bucket.blob(name)
            existing = blob.download_as_text() if blob.exists() else ""
            blob.upload_from_string(
                scan_log_util.appended_text(existing, month_records),
                content_type="application/x-ndjson")
            print(f">>> SCAN LOG: Appended {len(month_records)} records to {name}.",
                  flush=True)
        except Exception as exc:
            ok = False
            print(f"!!! SCAN LOG: Failed to append {len(month_records)} records "
                  f"to {name}: {exc}", flush=True)
    return ok


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


def is_new(item_id: str, seen: dict) -> bool:
    """Return True if item_id has not been processed before."""
    return item_id not in seen


def mark_seen(
    item_id: str,
    seen: dict,
    date_str: str | None = None,
) -> None:
    """Record item_id in the seen dict (in-place), accumulating dates.

    First call for an item stores a plain date string (backward compatible).
    Second call promotes the value to a list of dates. Subsequent calls append.
    """
    ds = date_str or date.today().isoformat()
    prev = seen.get(item_id)
    if prev is None:
        seen[item_id] = ds
    elif isinstance(prev, str):
        seen[item_id] = [prev, ds]
    else:
        prev.append(ds)


def seen_count(item_id: str, seen: dict) -> int:
    """How many times item_id has been marked seen (0, 1, or N)."""
    val = seen.get(item_id)
    if val is None:
        return 0
    if isinstance(val, str):
        return 1
    return len(val)


def first_seen_date(item_id: str, seen: dict) -> str | None:
    """Return the ISO date string of the item's first SEEN mark, or None."""
    val = seen.get(item_id)
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return val[0] if val else None


def is_crawl_unseen(item_id: str, seen: dict,
                    cutoff: str = config.CRAWL_DOUBLE_SEEN_CUTOFF) -> bool:
    """Return True if item_id should be re-fed by /crawl.

    Items first seen before ``cutoff`` were processed with early pipeline code;
    they need two SEEN marks before /crawl skips them. Items first seen on or
    after the cutoff are trusted and skipped after one.
    """
    if item_id not in seen:
        return True
    first = first_seen_date(item_id, seen)
    if first is not None and first < cutoff:
        return seen_count(item_id, seen) < 2
    return False
