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


def lot_value_and_deal(auto_confirmed, price_of, asking):
    """Total matched lot value + the undervalued-deal flag.

    ``price_of(year, slogan) -> float`` returns a button's max single-sale price
    (0.0 when no buy-rule/price exists for it). Kept pure by injection — main
    wires ``price_of`` to ``sheets_client.get_buy_decision`` + ``parse_price``;
    tests pass a stub.

    Returns ``(lot_value, undervalued, margin)`` where
    ``undervalued = asking > 0 and lot_value > asking`` and
    ``margin = lot_value - asking``.
    """
    lot_value = 0.0
    for b in auto_confirmed:
        try:
            lot_value += float(price_of(b.get("year"), b.get("slogan")) or 0.0)
        except Exception:
            pass
    ask = float(asking or 0.0)
    return lot_value, (ask > 0 and lot_value > ask), lot_value - ask


# Crops auto-staged into the reference DB must be REAL Hough detections — a
# Gemini-synthesised box (count collapsed to a grid, or recovered from x/y) is
# tagged with one of these `source` values; a genuine detected circle/rect has no
# `source` key at all.
_SYNTHETIC_SOURCES = ("gemini_led", "gemini_recovered")


def staging_candidates(auto_confirmed, circle_info, resolution, stage_conf):
    """Subset of ``auto_confirmed`` eligible for auto-staging into
    reference/_staging: the crop is a real Hough detection (not synthetic) AND
    Gemini confirmed its slogan (``resolution[crop_idx]["auto"]`` — the
    gemini_auto/majority/clip_fallback resolution, i.e. loose agreement with a
    CLIP top-N candidate).

    Gemini agreement is the safety signal — NOT the CLIP score (log analysis
    Logger_5: a score threshold is the wrong lever; a 0.968 visual-twin still
    matched wrong). ``stage_conf`` is therefore only a tiny junk floor (default
    ~0.5), not a confidence gate; a Gemini-agreed crop with a modest CLIP score
    is exactly the most valuable new reference. Pure (plain data in, list out)."""
    resolution  = resolution or {}
    circle_info = circle_info or []
    out = []
    for b in auto_confirmed:
        idx = b.get("crop_idx")
        if not (isinstance(idx, int) and 0 <= idx < len(circle_info)):
            continue                                   # no detection entry — can't verify origin
        ci = circle_info[idx] or {}
        if ci.get("source") in _SYNTHETIC_SOURCES:
            continue                                   # synthetic box, not real Hough
        res = resolution.get(idx)
        if not (res and res.get("auto")):
            continue                                   # Gemini did not confirm
        overall = b.get("overall")
        if overall is None or overall < stage_conf:
            continue                                   # junk floor only (sub-~0.5 noise)
        out.append(b)
    return out
