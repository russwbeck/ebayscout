"""
ebayscout/pipeline_classify.py

The Gemini-pipeline autoconfirmation decision tree, factored out of
``main.process_pipeline_lot`` so it is unit-testable without torch/clip/cv2.

Per crop, given CLIP diagnostics (a top-10 candidate list per crop, already
refined by the rerank + always-on reference-photo check) and the Gemini-slogan
resolution, decide one of: auto-confirm, yellow (human review), or ignore.

    Gemini works (``gemini_ok`` true) — autoconfirm-or-ignore, no human prompt:
      1. CLIP green/auto (``is_confirmed`` + the MIN_AUTO_GAP guard)  → confirm
      2. else Gemini's slogan is in the crop's CLIP top-10 AND conf ≥ 0.70 AND
         not flagged (``resolution[i]["auto"]``)                      → confirm
      3. else                                                          → ignore

    Gemini fails (``gemini_ok`` false) — Hough-only / CLIP-only:
      1. CLIP green/auto                                               → confirm
      2. else top-1 ``overall`` ≥ RED_THRESHOLD                        → yellow
      3. else (below RED)                                              → ignore

Pure python (stdlib + config + scoring) — no heavy deps.
"""

from __future__ import annotations

from . import config
from .scoring import is_confirmed

# Lone uncontested candidates near the AUTO line still need a real gap to
# auto-confirm (mirrors main.py / buttonmatcher). Demotes a near-0.85 top-1 that
# has no daylight over #2.
MIN_AUTO_GAP = 0.05


def _clip_confirmed(top: dict | None, gap) -> bool:
    """CLIP green/auto gate with the MIN_AUTO_GAP guard."""
    if top is None:
        return False
    gap_bad = gap is None or gap != gap or gap < MIN_AUTO_GAP   # gap != gap → NaN
    if gap_bad and top["overall"] < config.AUTO_RESOLVE_THRESHOLD + 0.05:
        return False
    return is_confirmed(top["overall"], gap)


def classify_crops(diagnostics, resolution, gemini_ok, job_id):
    """Return ``(auto_confirmed, yellow)``.

    ``auto_confirmed`` items: ``{n, crop_idx, year, slogan, overall, source}``.
    ``yellow`` items (Gemini-fails only): ``{year, slogan, overall, gap, check_id}``.
    """
    resolution = resolution or {}
    auto_confirmed: list[dict] = []
    yellow: list[dict] = []

    for i, d in enumerate(diagnostics):
        cands = d.get("candidates") or []
        gap   = d.get("gap")
        top   = cands[0] if cands else None
        res   = resolution.get(i)

        clip_conf = _clip_confirmed(top, gap)
        is_auto   = bool((res and res.get("auto")) or clip_conf)

        if not is_auto:
            # Gemini-fails fallback: a non-confirmed but above-RED crop goes to
            # human review. When Gemini works, non-confirmed crops are ignored.
            if (not gemini_ok and top is not None
                    and top["overall"] >= config.RED_THRESHOLD):
                yellow.append({
                    "year": top["year"], "slogan": top["slogan"],
                    "overall": top["overall"], "gap": gap,
                    "check_id": f"pipeline:{job_id}:{i}",
                })
            continue

        if res:
            year, slogan, source = res.get("year"), res.get("slogan"), res.get("source")
        else:
            year, slogan, source = top["year"], top["slogan"], "clip_green"
        auto_confirmed.append({
            "n": i + 1, "crop_idx": i, "year": year, "slogan": slogan,
            "overall": (top["overall"] if top else None), "source": source,
        })

    return auto_confirmed, yellow
