"""crop_vectors — the embedding of every matched crop, kept instead of discarded.

WHY THIS EXISTS (`REFERENCE_SCORING_REVIEW.md` §10.2, step 1)
------------------------------------------------------------
A reference photo has one job: when a real lot photo of that button arrives, its
year and its slogan should win.  Nothing in the curation loop has ever measured
that.  The composite quality score rates a crop as a *photograph* and its
heaviest term is the cosine to the photos already on the shelf, so a library
selected by it converges on one view of each button — 87% of the references in
``tests/fixtures/reference_decisions_2026-09.json`` sit at a saturated 100 on
that term.  A correlation cannot be read off a sample with no range, which is
why C8's first run could neither confirm nor refute it.

The measurement that *is* aimed at the process needs one thing the service
already computes and throws away: the crop's CLIP vector.  Every confirmation is
a labelled crop; with its embedding kept, the value of a reference set can be
replayed offline as "how many confirmed crops of this slogan does it rank #1,
minus how many crops of other slogans it steals" — the matcher's own answer, in
a dot product.  So:

- the **match** sites hand their per-crop vectors to a sink (``vec_sink``) and
  upload one small sidecar per lot, next to the ``pipeline/labels/`` sidecar
  that has existed since 2026-07-11;
- a **staged crop** carries its lot and crop number in its blob name, which is
  what joins it back to its vector (RS-04, now a prerequisite rather than a
  nicety).

Design rules (same as ``label_harvest``, for the same reasons):
- PURE (stdlib only) so the naming, the join key and the payload shape are
  unit-testable in a web session with no numpy.  The ``numpy.savez`` call and
  the GCS upload live at the call sites, which already have both in scope.
  Copied byte-for-byte into buttonmatcher + ebayscout.
- Fail-open: a sidecar must never break a lot.  Callers wrap in try/except.
- Kill switch: ``BUTTONMATCHER_CROP_VECTORS=0`` (shared name, same convention
  as ``BUTTONMATCHER_LABEL_HARVEST``).
- Outcomes are NOT duplicated here — they join via confirm_log on
  ``job_id`` + ``crop_num``, one durable source of truth per fact.

THE SIDECAR
-----------
``pipeline/embeddings/<lot_key>.npz``, written with ``numpy.savez_compressed``:

    crop_num  int32   [N]        the crop's 1-based number, as confirm_log logs it
    vec       float32 [N, D]     L2-normed ViT-B/32 image embedding, crop order
    meta      str     (0-d)      json.dumps(build_meta(...))

float32 and not float16: the value function ranks 3,000+ near-identical cosines
against each other, and half precision carries ~3 decimal digits — enough to
reorder a shelf.  At 512 floats per crop a 20-button lot costs ~40KB, well under
the label sidecar's JPEG.

``lot_key`` is the lot, not the service: ``job_id`` for a pipeline lot and the
Slack ``channel-thread`` id for a slash lot (the same id the staged blob name
carries, see ``lot_key_from_thread``), so one namespace joins both lanes.
"""

from __future__ import annotations

import datetime
import os

SCHEMA = "crop_vectors.v1"
EMBEDDINGS_PREFIX = "pipeline/embeddings/"

#: Staged-crop name markers.  ``<ms timestamp>__lot-<lot id>__crop-<n>.jpg``:
#: the timestamp stays first so existing newest-first name sorting is unchanged,
#: and each marker is introduced by a double underscore that a sanitized id can
#: never contain (see ``safe_key``), so the fields parse back unambiguously.
LOT_MARKER = "__lot-"
CROP_MARKER = "__crop-"

_IMAGE_EXTS = (".jpg", ".jpeg", ".png")


def vectors_enabled() -> bool:
    """Crop-vector sidecars are written for every matched lot by default.

    Set ``BUTTONMATCHER_CROP_VECTORS=0`` to disable; matching and confirming are
    unaffected either way (the sink is filled, the upload is skipped).
    """
    return os.environ.get("BUTTONMATCHER_CROP_VECTORS", "1").strip() not in (
        "0", "false", "False", "",
    )


def safe_key(raw) -> str:
    """A lot key safe to put inside a blob name, and inside a staged crop name.

    Anything outside ``[A-Za-z0-9-]`` is folded to ``-`` so the key can never
    introduce a path separator, disturb a name's extension, or produce the
    ``__`` that separates the fields of a staged name.  Truncated to 64 chars,
    which is what the Slack ``channel-thread`` ids have always fit in.
    """
    return "".join(
        c if (c.isalnum() or c == "-") else "-" for c in str(raw or "")
    )[:64]


def lot_key_from_thread(channel_id, thread_ts) -> str:
    """The lot key for a slash-flow lot: one Slack thread is one lot photo.

    Identical to the id the staged blob name has carried since the review queue
    learned to collapse several crops of one photo, so a staged crop and its
    vector sidecar agree on the key without a second convention.
    """
    return safe_key(f"{channel_id}-{thread_ts}")


def vectors_blob_name(lot_key) -> str:
    """The sidecar blob for one lot's crop vectors."""
    return f"{EMBEDDINGS_PREFIX}{safe_key(lot_key)}.npz"


def staged_crop_name(ts_ms, lot=None, crop_num=None) -> str:
    """The basename of a staged crop: ``<ms>__lot-<lot>__crop-<n>.jpg``.

    Both fields are optional and each is omitted when absent rather than
    written empty, so a name never claims provenance it does not have — a crop
    with no lot id must be treated as its own lot (see ``source_lot``) and one
    with no crop number simply does not join a vector.
    """
    name = str(int(ts_ms))
    if lot:
        name += f"{LOT_MARKER}{safe_key(lot)}"
    if crop_num is not None:
        name += f"{CROP_MARKER}{int(crop_num)}"
    return name + ".jpg"


def _tail(blob_name) -> str:
    """The basename, with any image extension removed."""
    tail = str(blob_name or "").rsplit("/", 1)[-1]
    for ext in _IMAGE_EXTS:
        if tail.lower().endswith(ext):
            return tail[: -len(ext)]
    return tail


def source_lot(blob_name):
    """The lot id in a staged blob name, or None when it carries none.

    None means "provenance unknown" — a crop staged before names carried a lot
    id.  Callers must treat each such crop as its own lot rather than lumping
    the unknowns together, or one pre-existing crop per entry would suppress
    the others for no reason.

    The field ends at the next ``__``, so the crop number that RS-04 appended
    is not read as part of the lot.  Before that stop existed, adding
    ``__crop-7`` to a name would have made every crop of one photo a distinct
    "lot" and silently switched off the same-lot collapse it was added to feed.
    """
    tail = _tail(blob_name)
    if LOT_MARKER not in tail:
        return None
    lot = tail.split(LOT_MARKER, 1)[1].split("__", 1)[0]
    return lot or None


def source_crop(blob_name):
    """The 1-based crop number in a staged blob name, or None when absent.

    With ``source_lot`` this is the join key onto the lot's vector sidecar:
    ``(lot, crop_num)`` identifies the exact crop that was matched, so a staged
    candidate arrives with the embedding the matcher already computed for it and
    the value function needs no CLIP.  Unparseable digits read as absent — a
    bad name loses a join, never a crop.
    """
    tail = _tail(blob_name)
    if CROP_MARKER not in tail:
        return None
    raw = tail.split(CROP_MARKER, 1)[1].split("__", 1)[0]
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def build_meta(*, lot_key, service, command, job_id=None, thread_ts=None,
               channel_id=None, item_id=None, count=None, dim=None,
               source="match") -> dict:
    """The sidecar's ``meta`` record (pure dict — the caller json-dumps it).

    ``source`` says how the vectors were produced: ``"match"`` for a live lot
    and ``"backfill"`` for one re-cut from its ``pipeline/labels/`` sidecar
    (§10.2 step 2).  A backfilled lot's crops are re-encoded from the stored
    detection image rather than the ≤2200px working image, so the two are not
    bit-identical and the value function is entitled to know which it has.
    """
    return {
        "schema": SCHEMA,
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "lot_key": safe_key(lot_key),
        "service": service,
        "command": command,
        "job_id": (str(job_id) if job_id is not None else None),
        "thread_ts": (str(thread_ts) if thread_ts is not None else None),
        "channel_id": (str(channel_id) if channel_id is not None else None),
        "item_id": (str(item_id) if item_id is not None else None),
        "count": (int(count) if count is not None else None),
        "dim": (int(dim) if dim is not None else None),
        "source": source,
        # Outcomes are not duplicated here (label_harvest's rule): a crop's
        # confirmed slogan, year and rank live in confirm_log and join on
        # job_id + crop_num.
        "confirm_join": "confirm_log.job_id+crop_num",
    }
