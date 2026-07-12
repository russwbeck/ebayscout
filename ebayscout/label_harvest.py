"""
label_harvest — training-label sidecars for the learned-detection track.

WHY THIS EXISTS (AUTOMATION_VISION.md / Logger_10 findings §10)
---------------------------------------------------------------
Every pipeline lot already produces, in memory, exactly what a center-point
detection model needs to train on: the detection-space image, the final circle
set with per-circle provenance, and Gemini's independent count/slogan reading.
Until now all of it was consumed once and discarded — and the pipeline deletes
its input/output blobs after processing, so the labels could not be
reconstructed later. This module persists them: one JSON record plus one
detection-space JPEG per lot under ``pipeline/labels/`` in the shared bucket.

Design rules (mirror the codebase's conventions):
- This module is PURE (stdlib only) so the record builder is unit-testable;
  the GCS upload and cv2 JPEG encode live at the call sites, which already
  have clients in scope. Copied byte-for-byte into buttonmatcher + ebayscout.
- Fail-open: harvesting must never break a lot. Callers wrap in try/except.
- Kill switch: BUTTONMATCHER_LABEL_HARVEST=0 (shared name, same convention as
  BUTTONMATCHER_SHADOW_PASS).
- Coordinates are pixels IN THE STORED IMAGE's space (the ≤800px detection
  image ships alongside, so there is no resize ambiguity), with w/h recorded.
- Confirmation outcomes are NOT duplicated here — they join via confirm_log
  on ``job_id`` (one durable source of truth per fact).

TRAINING-TIME GUIDANCE (for the bot that builds the model)
----------------------------------------------------------
- ``source`` per circle ranks label quality: "hough"/"hough+blob" (detector-
  found, Gemini-corroborated count), "gemini_led" (Gemini x/y — silver),
  "gemini_reconciled" (Gemini x/y for a detector miss — silver),
  "grid" (blind lattice cell — EXCLUDE from training, it is a guess).
- ``gemini.button_count`` is clamped by the caller pipeline but still silver
  truth; prefer lots whose confirm_log rows verify the crops.
- Do NOT train on reference/<sloganid>/ crops (curated best-of, wrong
  distribution) and never on tests/fixtures/lots (the held-out gate).

Cost: ~60-120KB JPEG + ~1-4KB JSON per lot ≈ 20-40MB/day at current volume.
"""

from __future__ import annotations

import datetime
import os

SCHEMA = "detect_labels.v1"
LABELS_PREFIX = "pipeline/labels/"


def harvest_enabled() -> bool:
    """Label sidecars are written for every pipeline lot by default.

    Set BUTTONMATCHER_LABEL_HARVEST=0 to disable (shared-name convention, like
    BUTTONMATCHER_SHADOW_PASS); detection and matching are unaffected either way.
    """
    return os.environ.get("BUTTONMATCHER_LABEL_HARVEST", "1").strip() not in (
        "0", "false", "False", "",
    )


def label_blob_names(job_id: str) -> tuple[str, str]:
    """(json_blob_name, jpeg_blob_name) for a lot's label sidecar pair."""
    safe = str(job_id).replace("/", "_")
    return (f"{LABELS_PREFIX}{safe}.json", f"{LABELS_PREFIX}{safe}.jpg")


def _circle_entry(i, info, source):
    """Serialize one circle_info entry (circle or rect shape) with provenance."""
    e = {"i": int(i), "source": source}
    shape = (info or {}).get("shape")
    if shape == "circle":
        e.update(shape="circle", x=int(info["x"]), y=int(info["y"]),
                 r=int(info["r"]))
    else:
        # Rect cells (projection grid) and synthesized boxes carry a bbox and
        # usually a centre + radius estimate; record whatever is present.
        e["shape"] = "rect"
        for k_src, k_dst in (("x1", "x1"), ("y1", "y1"), ("x2", "x2"),
                             ("y2", "y2"), ("cx", "cx"), ("cy", "cy"),
                             ("r", "r")):
            v = (info or {}).get(k_src)
            if v is not None:
                e[k_dst] = int(v)
    return e


def _gemini_count_inconsistent(button_count, slogans, flagged_count):
    """True when Gemini's claimed total disagrees with what it actually itemised
    (localized slogans + flagged partials) — the measured-Gemini-overcount signal
    (Phase 4c / Stage-B "Gemini as ruler").  None when there is no count to check;
    fail-open (any bad input) also returns None so a logging edge never raises."""
    if not button_count:
        return None
    try:
        itemised = len(slogans or []) + int(flagged_count or 0)
        return bool(int(button_count) != itemised)
    except (TypeError, ValueError):
        return None


def build_label_record(
    *,
    job_id,
    service,
    command,
    image_name=None,
    item_id=None,
    img_w,
    img_h,
    circle_info,
    circle_sources,
    detector_used=None,
    mask_path=None,
    mask_coverage=None,
    ni_selected=None,
    ni_gate=None,
    ni_scale_path=None,
    gemini_button_count=None,
    gemini_flagged_count=None,
    gemini_slogans=None,
    unmatched_crop_indices=None,
):
    """Build one lot's label record (pure dict — caller serializes/uploads).

    ``circle_info``/``circle_sources`` are parallel; sources shorter than the
    circle list are padded with None (never raises — fail-open by shape).
    ``gemini_slogans`` is passed through as-is (the Gem's per-button entries,
    typically {index, text, x, y, size...}) so per-button text/geometry from
    the independent reader is preserved verbatim.

    ``unmatched_crop_indices`` (optional) is the ``unmatched_crop_indices``/
    ``unmatched_crops`` list from the reconcile telemetry: indices (into this
    same ``circle_info``, at the point reconcile ran) of circles no Gemini
    point covered. When given, every circle is tagged ``gemini_backed`` (False
    for those indices, True otherwise — including all Gemini-recovered
    circles, which by construction are always backed). When ``None`` (the
    match couldn't meaningfully run), the key is left ABSENT rather than
    guessed, so "unknown" is never mistaken for "backed" or "unbacked" in
    training data.
    """
    circles = []
    sources = list(circle_sources or [])
    _unmatched = set(unmatched_crop_indices) if unmatched_crop_indices is not None else None
    for i, info in enumerate(circle_info or []):
        src = sources[i] if i < len(sources) else None
        entry = _circle_entry(i, info, src)
        if _unmatched is not None:
            entry["gemini_backed"] = i not in _unmatched
        circles.append(entry)

    return {
        "schema": SCHEMA,
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "job_id": str(job_id),
        "service": service,
        "command": command,
        "image_name": image_name,
        "item_id": item_id,
        "image": {"w": int(img_w), "h": int(img_h),
                  "blob": label_blob_names(job_id)[1]},
        "detection": {
            "detector_used": detector_used,
            "mask_path": mask_path,
            "mask_coverage": mask_coverage,
            "ni_selected": ni_selected,
            "ni_gate": ni_gate,
            "ni_scale_path": ni_scale_path,
        },
        "gemini": {
            "button_count": gemini_button_count,
            "flagged_count": gemini_flagged_count,
            "slogans": gemini_slogans or [],
            # Measured-Gemini-error signal (Phase 4c / Stage-B "Gemini as ruler"):
            # True when Gemini's claimed total disagrees with what it actually
            # itemised (localized slogans + flagged partials) — i.e. it counted
            # buttons it never placed, the busy-background overcount behind the
            # turf/carpet phantoms.  None when there is no count to check.  Derived
            # here so both services log it identically; the detector's independent
            # count is the circles list, for a full three-way if wanted.
            "count_inconsistent": _gemini_count_inconsistent(
                gemini_button_count, gemini_slogans, gemini_flagged_count),
        },
        "circles": circles,
        # Confirmation outcomes intentionally NOT duplicated here — join
        # confirm_log rows on job_id (single source of truth per fact).
        "confirm_join": "confirm_log.job_id",
    }
