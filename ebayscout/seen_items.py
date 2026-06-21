"""
ebayscout/seen_items.py

GCS-backed deduplication store.

seen_items.json lives at SEEN_ITEMS_BLOB in the shared GCS bucket.
Structure: {"item_id": "YYYY-MM-DD", ...}

All functions mutate / read the in-memory `seen` dict; call save_seen()
once at the end of a successful job run.
"""

import json
import time
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


# ---------------------------------------------------------------------------
# Gemini pipeline correlation + crop staging (Drive watcher → Gem → GCS)
# ---------------------------------------------------------------------------
# /crawl10 + /crawl500 upload each lot's primary photo to the GCS input prefix
# under a random correlation `key`; the watcher's Gem writes the result to
# pipeline/output/ and POSTs it back to /pipeline/notify. The listing context is
# persisted here (keyed by the same `key`) so the async result can be correlated
# even after a cold start. The surest auto-confirmed crops are written to a temp
# prefix and immediately promoted into the shared reference/_staging/ area
# (auto-staging) for buttonmatcher's /reference review — no Yes/No vote.

def save_pending_context(key: str, ctx: dict,
                         bucket_name: str = config.BUCKET_NAME) -> bool:
    """Persist a per-lot correlation context blob at PENDING_CONTEXT_PREFIX<key>.json."""
    try:
        client = storage.Client()
        blob   = client.bucket(bucket_name).blob(f"{config.PENDING_CONTEXT_PREFIX}{key}.json")
        blob.upload_from_string(json.dumps(ctx, indent=2), content_type="application/json")
        return True
    except Exception as exc:
        print(f"!!! PIPELINE: save_pending_context({key}) failed: {exc}", flush=True)
        return False


def load_pending_context(key: str,
                         bucket_name: str = config.BUCKET_NAME) -> dict | None:
    """Read back a pending-context blob; None if missing/unreadable."""
    try:
        client = storage.Client()
        blob   = client.bucket(bucket_name).blob(f"{config.PENDING_CONTEXT_PREFIX}{key}.json")
        if not blob.exists():
            return None
        return json.loads(blob.download_as_text())
    except Exception as exc:
        print(f"!!! PIPELINE: load_pending_context({key}) failed: {exc}", flush=True)
        return None


def delete_pending_context(key: str,
                           bucket_name: str = config.BUCKET_NAME) -> None:
    """Delete a pending-context blob (best-effort)."""
    try:
        client = storage.Client()
        blob   = client.bucket(bucket_name).blob(f"{config.PENDING_CONTEXT_PREFIX}{key}.json")
        if blob.exists():
            blob.delete()
    except Exception as exc:
        print(f"!!! PIPELINE: delete_pending_context({key}) failed: {exc}", flush=True)


def upload_pipeline_input(key: str, image_bytes: bytes,
                          bucket_name: str = config.BUCKET_NAME) -> str | None:
    """Drop one lot's primary photo into the GCS pipeline-input prefix for the
    watcher to pick up: pipeline/input/ebayscout__<key>.png.

    We upload to GCS (not Drive) because a service account has no Drive storage
    quota on a personal Google account ("storageQuotaExceeded"); GCS uses the
    project's quota, so the same compute SA can write here freely. The watcher
    polls this prefix, runs the Gem, and writes the result to pipeline/output/.
    Returns the object name, or None on failure (caller logs + continues)."""
    try:
        name   = f"{config.PIPELINE_INPUT_PREFIX}{config.PIPELINE_OBJECT_PREFIX}{key}.png"
        client = storage.Client()
        client.bucket(bucket_name).blob(name).upload_from_string(
            image_bytes, content_type="image/png")
        return name
    except Exception as exc:
        print(f"!!! CRAWL10: upload_pipeline_input({key}) failed: {exc}", flush=True)
        return None


def stage_pipeline_crop(job_id: str, n: int, jpg_bytes: bytes,
                        bucket_name: str = config.BUCKET_NAME) -> str | None:
    """Write one auto-confirmed crop to the temp holding area; return its GCS name."""
    try:
        name = f"{config.PIPELINE_CROPS_PREFIX}{job_id}/{n}.jpg"
        client = storage.Client()
        client.bucket(bucket_name).blob(name).upload_from_string(
            jpg_bytes, content_type="image/jpeg")
        return name
    except Exception as exc:
        print(f"!!! PIPELINE: stage_pipeline_crop({job_id},{n}) failed: {exc}", flush=True)
        return None


def promote_crops_to_reference_staging(job_id: str, manifest: dict,
                                       bucket_name: str = config.BUCKET_NAME) -> int:
    """YES vote: copy each temp crop into reference/_staging/<entry_id>/<ts>.jpg
    (the shared area buttonmatcher's /reference flow consumes), then remove the
    temp crops + manifest. Returns the number of crops staged.

    ebayscout writes only image FILES here — it never encodes or writes vectors.pt.
    """
    staged = 0
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        for crop in manifest.get("crops", []):
            src_name  = crop.get("gcs_name")
            entry_id  = crop.get("entry_id")
            if not src_name or not entry_id:
                continue
            src = bucket.blob(src_name)
            if not src.exists():
                continue
            ts   = int(time.time() * 1000) + staged   # unique ms timestamp
            dest = f"{config.REFERENCE_STAGING_PREFIX}{entry_id}/{ts}.jpg"
            bucket.copy_blob(src, bucket, dest)
            staged += 1
    except Exception as exc:
        print(f"!!! PIPELINE: promote_crops({job_id}) failed: {exc}", flush=True)
    finally:
        delete_pipeline_crops(job_id, bucket_name=bucket_name)
    return staged


def delete_pipeline_crops(job_id: str,
                          bucket_name: str = config.BUCKET_NAME) -> int:
    """NO vote (or post-promotion cleanup): delete all temp objects + manifest
    under the job's crop prefix. Returns the number of objects deleted."""
    deleted = 0
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        for blob in bucket.list_blobs(prefix=f"{config.PIPELINE_CROPS_PREFIX}{job_id}/"):
            try:
                blob.delete()
                deleted += 1
            except Exception:
                pass
    except Exception as exc:
        print(f"!!! PIPELINE: delete_pipeline_crops({job_id}) failed: {exc}", flush=True)
    return deleted


def sweep_pipeline_temp(ttl_days: int = config.PIPELINE_TTL_DAYS,
                        bucket_name: str = config.BUCKET_NAME) -> int:
    """Delete pending-context + crop blobs older than ttl_days (never-returned
    Gem reads, abandoned Yes/No prompts). Best-effort; returns objects deleted."""
    cutoff = time.time() - ttl_days * 86400
    deleted = 0
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        for prefix in (config.PENDING_CONTEXT_PREFIX, config.PIPELINE_CROPS_PREFIX):
            for blob in bucket.list_blobs(prefix=prefix):
                created = blob.time_created
                if created and created.timestamp() < cutoff:
                    try:
                        blob.delete()
                        deleted += 1
                    except Exception:
                        pass
    except Exception as exc:
        print(f"!!! PIPELINE: sweep_pipeline_temp failed: {exc}", flush=True)
    return deleted


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
