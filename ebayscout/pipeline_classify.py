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

Also holds the two pure helpers the pipeline needs around that tree:
``gemini_db_candidates`` (the DB-direct agreement tier, which un-shadows a slogan
CLIP's year-folded candidate list can never surface) and ``staging_funnel``
(per-gate drop counts for the crops → reference/_staging path).

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
        if res.get("db_direct"):
            continue                                   # Gemini's word alone — see below
        overall = b.get("overall")
        if overall is None or overall < stage_conf:
            continue                                   # junk floor only (sub-~0.5 noise)
        out.append(b)
    return out


# A DB-direct resolution (``gemini_db_candidates``) is a row appended straight
# from the slogan DB because CLIP's year-folded candidate list never surfaced it.
# It is Gemini's read with NO independent CLIP corroboration — good enough to
# match and price a lot, but NOT good enough to write into the shared reference
# library unattended.  ebayscout stages with no human in the loop (unlike
# buttonmatcher's /inventory, where the operator watches every auto-confirm), so
# a db_direct crop NEVER auto-stages, on any resolution rung.  Net effect:
# ebayscout's staging bar is exactly what it was before the DB-direct tier
# existed — two independent signals (CLIP ranked it, Gemini read it) or nothing.


# ---------------------------------------------------------------------------
# DB-direct agreement tier (buttonmatcher parity — main._gemini_db_candidates)
# ---------------------------------------------------------------------------
# Gemini confidence floor for the DB-direct tier.  Stricter than the resolver's
# 0.70 pool gate: this tier has NO CLIP-rank corroboration at all, so it demands
# more of the reader.
GEMINI_DB_DIRECT_CONF = 0.85


def gemini_db_candidates(gemini_slogan, confidence, db_rows, normalize_fn,
                         existing, conf_min=None):
    """DB-direct agreement tier — the within-year shadowing case (Logger_14).

    ``clip_matcher`` builds a crop's candidate list YEAR-FOLDED: ``_score_slogans``
    emits exactly ONE row per candidate year (that year's text-argmax slogan) for
    at most 8 dual-signal years, and ``match_logging.build_leaderboard`` folds the
    same way (``best_by_year``).  So a slogan shadowed by a sibling of its OWN
    year never appears in the pool at ANY depth — the resolver's Scenario A/B can
    never see it, the crop stays "manual", and it is therefore never auto-confirmed
    and never auto-staged into reference/_staging.  That is the dominant reason a
    big ``/crawl`` yields a handful of reference crops out of hundreds of buttons.

    When Gemini's read is a known DB slogan at high confidence, append that
    slogan's DB rows so the existing Scenario A/B rules (unique year / majority
    era / printed year) can match it.  Downstream gates are unchanged: the
    resolver still requires conf ≥ conf_min, an unflagged index and an anchored
    association before ``auto``, and every staged crop still lands in
    ``reference/_staging`` for human /reference review.

    Returns a list of candidate dicts to APPEND (possibly empty).  ``db_rows`` is
    an iterable of ``(year, phrase, type)`` tuples; ``existing`` is the current
    pool (used for dedup).  Pure — unit-tested.
    """
    conf_min = GEMINI_DB_DIRECT_CONF if conf_min is None else conf_min
    if not gemini_slogan:
        return []
    if confidence is not None and confidence < conf_min:
        return []
    norm_g = normalize_fn(str(gemini_slogan))
    if not norm_g:
        return []
    seen = set()
    for r in existing or []:
        try:
            seen.add((str(r.get("year")), normalize_fn(str(r.get("slogan") or ""))))
        except Exception:
            continue
    out = []
    for row in db_rows or []:
        try:
            year, phrase, ptype = row      # inside the try: a malformed row is skipped
            if normalize_fn(str(phrase)) != norm_g:
                continue
            key = (str(year), norm_g)
            if key in seen:
                continue
            seen.add(key)
            try:
                year = int(str(year).strip())
            except (TypeError, ValueError):
                pass
            out.append({
                "year": year,
                "slogan": phrase,
                "type": ptype,
                "overall": None,
                "slogan_score": 0,
                "db_direct": True,
            })
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Staging funnel telemetry
# ---------------------------------------------------------------------------

def staging_funnel(n_crops, auto_confirmed, circle_info, resolution, stage_conf):
    """Count where crops die on the crops → reference/_staging path.

    ``/crawl`` is fire-and-forget at scale, so without this the only observable
    is "N buttons in, M reference crops out" with no way to tell WHICH gate ate
    the difference.  Returns a flat dict of counts (pure, no logging):

      crops              crops that reached classification
      resolved           crops the Gemini resolver matched to a CLIP candidate
      gemini_auto        of those, the ones that cleared conf/flag/anchor
      auto_confirmed     crops classify_crops confirmed (Gemini auto OR CLIP green)
      stageable          crops that survive every staging gate
      drop_no_resolution auto-confirmed crops with no resolver entry (CLIP-green
                         only — never stageable by design)
      drop_not_auto      auto-confirmed crops whose resolution isn't `auto`
      drop_synthetic     dropped because the crop is a Gemini-synthesised box
      drop_below_conf    dropped by the STAGE_CONF junk floor
      drop_no_geometry   dropped because the crop has no circle_info entry
      drop_db_direct     dropped because the match came from the DB-direct
                         tier — Gemini's read with no CLIP corroboration, which
                         never auto-stages
    """
    resolution  = resolution or {}
    circle_info = circle_info or []
    res_int = {k: v for k, v in resolution.items() if isinstance(k, int)}
    out = {
        "crops": int(n_crops or 0),
        "resolved": len(res_int),
        "gemini_auto": sum(1 for v in res_int.values() if v and v.get("auto")),
        "auto_confirmed": len(auto_confirmed or []),
        "stageable": 0,
        "drop_no_resolution": 0,
        "drop_not_auto": 0,
        "drop_synthetic": 0,
        "drop_below_conf": 0,
        "drop_no_geometry": 0,
        "drop_db_direct": 0,
    }
    for b in auto_confirmed or []:
        idx = b.get("crop_idx")
        if not (isinstance(idx, int) and 0 <= idx < len(circle_info)):
            out["drop_no_geometry"] += 1
            continue
        if ((circle_info[idx] or {}).get("source")) in _SYNTHETIC_SOURCES:
            out["drop_synthetic"] += 1
            continue
        res = res_int.get(idx)
        if not res:
            out["drop_no_resolution"] += 1
            continue
        if not res.get("auto"):
            out["drop_not_auto"] += 1
            continue
        if res.get("db_direct"):
            out["drop_db_direct"] += 1
            continue
        overall = b.get("overall")
        if overall is None or overall < stage_conf:
            out["drop_below_conf"] += 1
            continue
        out["stageable"] += 1
    return out


# ---------------------------------------------------------------------------
# Stop-staging policy (buttonmatcher's /reference STOP list)
# ---------------------------------------------------------------------------
# `stop` in buttonmatcher's /reference review writes the slogan's entry_id into
# reference/_staging_policy.json in the SHARED bucket — the operator's statement
# that this slogan is finished and should stop generating review work.
# buttonmatcher honours it in _stage_confirmed_crop; ebayscout did not honour it
# at all, so it kept writing crops into slogans that had been declared done —
# unattended, which is exactly where an unwanted write is hardest to notice.
# These are the pure halves; seen_items does the GCS read.

def parse_staging_policy(data) -> set:
    """JSON dict -> the set of stopped entry_ids.  Mirrors buttonmatcher's
    reference_review.policy_from_json exactly (the two services read the same
    blob).  Tolerates junk by returning an empty set — the caller distinguishes
    "no stops declared" from "could not read the policy", which are not the same
    thing for an unattended writer."""
    try:
        return set(str(e) for e in (data or {}).get("stopped", []))
    except Exception:
        return set()


def filter_stopped_crops(manifest_crops, stopped):
    """Split staged-crop manifest entries into (kept, dropped) by the STOP list.

    Matches on ``entry_id`` — the same id buttonmatcher stores in the policy and
    the same one clip_matcher.entry_id_for produces, so the two services agree on
    slogan identity.  Pure."""
    stopped = stopped or set()
    kept, dropped = [], []
    for crop in manifest_crops or []:
        (dropped if crop.get("entry_id") in stopped else kept).append(crop)
    return kept, dropped


# --- Look-alike value flag ----------------------------------------------------
#
# ebayscout has no human-review lane: `_post_yellow_review` is defined but never
# called, and the pipeline posts deals only.  So buttonmatcher's confusable
# guard — which DEMOTES an auto-confirm to a human picker — has nowhere to
# demote to here; applying it would silently drop the match instead.
#
# The failure mode still matters, because it lands somewhere different.  A
# confirmed (year, slogan) is what `get_buy_decision` is keyed on, so a wrong
# within-year slogan pick moves BOTH of the numbers an alert exists to report:
# `amount_needed` (whether you need it at all) and `max_price_single` (what the
# lot is worth).  A same-year look-alike can therefore send you to buy a lot for
# a button that isn't in it, priced off the wrong row.
#
# There IS a human in this system — reading the deal post, deciding to spend
# money.  So the fix that fits is to FLAG the alert, not to demote the match:
# keep the deal, name the look-alikes, and say what the value would be if the
# other one is the right read.

def lookalike_note(slogan, year, family, priced_fn, normalize_fn, max_listed=4):
    """Value warning for a confirmed match whose slogan has curated look-alikes.

    Parameters
    ----------
    slogan, year : the confirmed match (the one that will be alerted on).
    family       : the catalog entries of its confusable group, as returned by
                   ``confusable_slogans.confusable_family`` — dicts carrying at
                   least ``slogan`` and ``year``.
    priced_fn    : ``(year, slogan) -> (price: float, amount_needed: int)``.
                   Injected so this stays pure and unit-testable; the caller
                   passes a closure over the already-loaded buy_rules dict, so
                   no Sheet round-trip happens here.
    normalize_fn : slogan-identity normalizer, used to drop the match's own
                   entries from its family.

    Returns None when there is nothing worth saying — no family, or every
    alternative carries the same price AND the same need, so a wrong pick would
    not change the alert.  Otherwise a dict:

        alternatives  [{slogan, year, price, amount_needed}], priciest first,
                      capped at ``max_listed``
        price_here    the confirmed match's own price
        value_swing   the largest |alternative price - price_here|, i.e. how far
                      off the lot value could be
        need_here     the confirmed match's amount_needed
        any_not_needed  True when at least one alternative is NOT needed — the
                      case where the whole alert could be spurious
    """
    if not slogan or not family:
        return None
    own_key = normalize_fn(slogan)
    price_here, need_here = priced_fn(year, slogan)

    alts, seen = [], set()
    for entry in family:
        e_slogan = (entry or {}).get("slogan")
        if not e_slogan:
            continue
        if normalize_fn(e_slogan) == own_key:
            continue                      # the match's own entries, incl. its twins
        e_year = (entry or {}).get("year")
        dedup = (normalize_fn(e_slogan), str(e_year))
        if dedup in seen:
            continue
        seen.add(dedup)
        e_price, e_need = priced_fn(e_year, e_slogan)
        alts.append({"slogan": e_slogan, "year": e_year,
                     "price": e_price, "amount_needed": e_need})
    if not alts:
        return None

    swing = max(abs(a["price"] - price_here) for a in alts)
    any_not_needed = any(a["amount_needed"] <= 0 for a in alts)
    if swing == 0 and not any_not_needed:
        # Same price, same standing need: which one it is doesn't change the
        # decision, so don't spend a line on it.
        return None

    alts.sort(key=lambda a: a["price"], reverse=True)
    return {
        "alternatives":   alts[:max_listed],
        "price_here":     price_here,
        "value_swing":    swing,
        "need_here":      need_here,
        "any_not_needed": any_not_needed,
    }
