"""Two-level reference re-ranking: Year Score + SloganID Score.

After the existing CLIP image+text blend produces candidates, two further
reference-image measurements refine the ranking (auto path only — when the
user picks an explicit bank era, that hard filter stays authoritative):

Year Score
    "Does this crop look like year-Y buttons compared to the rest of Y's era?"
    Contrast of the crop's best similarity to year-Y reference images against
    its similarity to the other years of that era.  This is the per-button
    year inference that replaces the human-supplied era filter.

SloganID Score (photo rescue)
    "Does it look like THIS specific button versus similar-slogan buttons of
    other years?"  Contrast of the crop's similarity to the candidate entry's
    own reference photos against its similarity to peer entries (other years,
    similar slogan text).  Entries with no slogan-attributed photos score a
    neutral 0 — never penalized for missing data while attribution is partial.

Both scores land in [-1, 1] and contribute bounded additive deltas (±YEAR_WEIGHT
/ ±SID_WEIGHT) to the overall score — the same order as the existing rarity
tiebreaker cap (0.04), so a wrong inference can demote but never exclude.

Pure-python (no cv2/numpy/torch); similarities are computed by the caller.
"""

import os

# Max additive contribution of each score to `overall`.  Seeds pending offline
# calibration (tools/calibrate_from_logs.py re-ranks logged shadow_top_json
# under candidate weights against confirmed answers).
YEAR_WEIGHT = 0.05
SID_WEIGHT = 0.05

# CLIP image-similarity gaps of ~0.05 are decisive in practice; this scales a
# raw similarity contrast into the [-1, 1] score range.
CONTRAST_SCALE = 0.05

# Absolute fallback for the SloganID score when an entry has reference photos
# but no peers exist to contrast against: same-button photo pairs typically
# clear this similarity, different-button pairs rarely do.
SID_ABS_BASELINE = 0.82


def rerank_enabled():
    """Re-ranking is opt-in until calibrated; EBAYSCOUT_RERANK=1 (or the shared
    BUTTONMATCHER_RERANK=1) enables it.  Default off."""
    for var in ("EBAYSCOUT_RERANK", "BUTTONMATCHER_RERANK"):
        if os.environ.get(var, "0").strip() in ("1", "true", "True"):
            return True
    return False


def _clamp(v, lo=-1.0, hi=1.0):
    return max(lo, min(hi, v))


def year_score(year, year_max_sims, era_years):
    """Contrast year's best ref similarity against the rest of its era.

    year_max_sims : {year: max crop↔ref similarity for that year}
    era_years     : years belonging to the candidate year's era

    Returns a float in [-1, 1]; 0 when the era offers nothing to contrast with.
    """
    try:
        own = year_max_sims[year]
    except (KeyError, TypeError):
        return 0.0
    others = [
        s for y, s in year_max_sims.items()
        if y != year and y in era_years
    ]
    if not others:
        return 0.0
    mean_others = sum(others) / len(others)
    return _clamp((float(own) - mean_others) / CONTRAST_SCALE)


def sloganid_score(entry_sim, peer_sims):
    """Contrast the candidate entry's own photos against similar-slogan peers.

    entry_sim : max crop↔ref similarity over THIS entry's photos, or None when
                the entry has no slogan-attributed photos yet (→ neutral 0).
    peer_sims : max similarities to each peer entry's photos (may be empty).

    Returns a float in [-1, 1].
    """
    if entry_sim is None:
        return 0.0
    entry_sim = float(entry_sim)
    if peer_sims:
        best_peer = max(float(s) for s in peer_sims)
        return _clamp((entry_sim - best_peer) / CONTRAST_SCALE)
    # No peers to contrast: high absolute similarity to this exact button's
    # photos is itself evidence (half-strength — absolute sims are noisier).
    return _clamp((entry_sim - SID_ABS_BASELINE) / CONTRAST_SCALE) * 0.5


def adjustment(yscore, sscore, w_year=YEAR_WEIGHT, w_sid=SID_WEIGHT):
    """Bounded additive delta for `overall` from the two scores."""
    return _clamp(yscore) * w_year + _clamp(sscore) * w_sid


def similar_slogan_peers(phrase, year, phrases, years, tokenize_fn, stopwords,
                         max_peers=10):
    """Indices of entries in OTHER years whose slogan shares a non-stopword
    token with ``phrase``, ranked by shared-token count (desc).

    These are the lookalike entries the SloganID score must beat — e.g. the
    same opponent slogan reissued across Mellon years.
    """
    words = set(tokenize_fn(phrase or "")) - set(stopwords)
    if not words:
        return []
    scored = []
    for i, (p, y) in enumerate(zip(phrases, years)):
        if y == year:
            continue
        shared = words & (set(tokenize_fn(p)) - set(stopwords))
        if shared:
            scored.append((len(shared), i))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [i for _, i in scored[:max_peers]]


def rerank_results(results, score_fn):
    """Re-rank score_slogans output under the two-level reference scores.

    results  : list of dicts with at least year/overall (score_slogans shape).
    score_fn : callable(result) -> (yscore, sscore) — the caller computes the
               similarity lookups; this function only applies bounded deltas
               and re-sorts.

    Returns a NEW list (same dicts, mutated with year_score/sid_score/
    rerank_delta keys and adjusted overall), sorted by the adjusted overall.
    """
    for r in results:
        ys, ss = score_fn(r)
        delta = adjustment(ys, ss)
        r["year_score"] = round(_clamp(ys), 4)
        r["sid_score"] = round(_clamp(ss), 4)
        r["rerank_delta"] = round(delta, 4)
        r["overall"] = min(1.0, r["overall"] + delta)
    return sorted(results, key=lambda x: (x["overall"], x.get("slogan_score", 0)),
                  reverse=True)
