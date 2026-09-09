"""Pure scale-consensus math for the scale-first unguided detector.

The unguided detector's core problem is that Hough needs an expected radius —
exactly what the human's count/grid input supplies.  This module holds the
numpy/cv2-free half of the fix: given per-blob radius measurements from the
hole-filled detection mask (computed in detect.py), turn them into one
consensus radius estimate plus a confidence in that estimate.

Per blob, detect.py measures three radius estimates:
    r_dt    peak of the distance transform inside the blob (inscribed radius)
    r_enc   minimum enclosing circle radius
    r_area  sqrt(area / pi) — the radius the blob's area implies
and a circularity (4*pi*area / perimeter^2).

For a solo button the three estimates agree.  For a blob of touching buttons
r_enc grows with the blob while r_dt stays near the single-button radius (each
button core is a distance-transform peak), so the merged blob still casts a
useful vote — at r_dt, with reduced weight.

Unit-tested with plain floats; no cv2/numpy/torch imports.
"""

# r_enc / r_dt above this ⇒ blob is probably several touching buttons merged.
MERGED_RATIO = 1.35

# Blobs below this circularity are clutter (hands, table edges, straps) and
# get the floor weight rather than being dropped — on a clean image they don't
# exist, on a messy one they shouldn't be allowed to outvote real buttons.
MIN_CIRCULARITY = 0.45

# Weight floor so no accepted vote is ever exactly zero.
MIN_VOTE_WEIGHT = 0.05

# Merged blobs vote at r_dt with this fixed weight (their circularity is
# inherently low, so circularity-weighting would silence them entirely).
MERGED_VOTE_WEIGHT = 0.40

# Minimum scale confidence for the scale-first Hough pass to run at all.
SCALE_CONF_MIN = 0.35


def blob_vote(r_dt, r_enc, r_area, circularity):
    """One mask blob → one radius vote.

    Returns {"radius": float, "weight": float, "merged": bool}, or None when
    the measurements are degenerate (non-positive radii).
    """
    try:
        r_dt = float(r_dt)
        r_enc = float(r_enc)
        r_area = float(r_area)
        circularity = float(circularity)
    except (TypeError, ValueError):
        return None
    if r_dt <= 0 or r_enc <= 0 or r_area <= 0:
        return None

    merged = r_enc > r_dt * MERGED_RATIO
    if merged:
        # Touching buttons: only the distance-transform peak still reflects a
        # single button's radius.
        return {"radius": r_dt, "weight": MERGED_VOTE_WEIGHT, "merged": True}

    # Solo button: r_area sits between the hole-deflated r_dt and the
    # enclosure-inflated r_enc — use it as the vote.
    weight = circularity if circularity >= MIN_CIRCULARITY else MIN_VOTE_WEIGHT
    weight = max(MIN_VOTE_WEIGHT, min(1.0, weight))
    return {"radius": r_area, "weight": weight, "merged": False}


def weighted_median(values, weights):
    """Weighted median of parallel lists.  None on empty/invalid input."""
    pairs = [
        (float(v), float(w))
        for v, w in zip(values, weights)
        if w is not None and float(w) > 0
    ]
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2.0:
            return v
    return pairs[-1][0]


def consensus_radius(votes):
    """Combine blob votes into (r_est, scale_conf, n_merged).

    r_est       weighted median of vote radii (None when there are no votes)
    scale_conf  1 − weighted coefficient-of-variation of the votes, clamped to
                [0, 1].  One single vote can't show agreement, so it is capped
                at a middling confidence rather than a perfect 1.0.
    n_merged    how many votes came from merged (touching-button) blobs —
                detect.py uses this to decide whether the blob-buster matters.
    """
    votes = [v for v in (votes or []) if v]
    if not votes:
        return None, 0.0, 0

    radii = [v["radius"] for v in votes]
    weights = [v["weight"] for v in votes]
    n_merged = sum(1 for v in votes if v.get("merged"))

    r_est = weighted_median(radii, weights)
    if r_est is None or r_est <= 0:
        return None, 0.0, n_merged

    total_w = sum(weights)
    mean = sum(r * w for r, w in zip(radii, weights)) / total_w
    if mean <= 0:
        return None, 0.0, n_merged
    var = sum(w * (r - mean) ** 2 for r, w in zip(radii, weights)) / total_w
    cv = (var ** 0.5) / mean
    conf = max(0.0, min(1.0, 1.0 - cv))

    if len(votes) == 1:
        # A lone blob can't demonstrate agreement; don't let it claim certainty.
        conf = min(conf, 0.60)

    return r_est, round(conf, 4), n_merged


# --- Tiny-circle guard ------------------------------------------------------
#
# "Tiny circle syndrome": the multi-scale Hough sweep searches radii down to
# ~0.3x the base radius, so on a lot photo it also fires on every round thing
# PRINTED ON a button — the letter bowls of o/e/g/0/9, the Mellon/Citizens/cb
# logo roundels, the two-digit year — plus the classic half-radius accumulator
# ghost inside a real rim.  Those sub-button circles then survive every filter
# downstream:
#
#   * the fill-ratio filter PASSES them at ~1.0 (a small disc inside a blue
#     button is entirely "blue"), and _score_solution's fill_mean term is
#     therefore biased in their favour;
#   * the overlap dedup rejects on 0.7 x min(r_candidate, r_accepted), which
#     for a small circle inside a big one is 0.7 x its own tiny radius — so a
#     letter bowl half a button away from the centre is never suppressed;
#   * the inner-circle removal needs the centre within 0.3 x r_big, which an
#     off-centre logo or year misses;
#   * ebayscout's scan mode skips the radius-consistency filter outright.
#
# Two pure rules fix it, both keyed on the same fact: buttons in a lot lie flat
# and are all one size.
#
#   (a) CONTAINMENT — a candidate whose centre lies inside an already-accepted
#       (larger) circle's disc is a feature ON that button, not another button.
#       Same geometry as detect.py's _fill_veto_reason check (a), which was
#       measured vetoing 5/5 phantoms; this applies it to the PRIMARY Hough set,
#       which the veto deliberately never touches.
#   (b) COHORT BAND — once a clear majority of the accepted circles agree on a
#       radius, circles far outside that band are sub-features (or giants), not
#       buttons.  Deliberately looser than the guided path's 0.7-1.3 x median:
#       it is a syndrome guard, not a uniformity filter, so a genuinely
#       odd-sized button (up to 1.8x) still survives — that recall worry is
#       exactly why scan mode had no radius filter at all.

# A candidate centre closer than this fraction of an accepted circle's radius
# is inside that button.
CONTAINED_FRAC = 0.85

# Cohort band around the dominant radius.  Asymmetric on purpose: the syndrome
# produces circles at <= ~0.5x the true radius, while real size variety in one
# lot tops out well under 1.8x.
BAND_LO = 0.55
BAND_HI = 1.80

# The band only applies when the cohort is big enough to be believed ...
BAND_MIN_CIRCLES = 4
# ... and when this fraction of it actually agrees on the dominant radius.
BAND_MIN_SUPPORT = 0.60

# Radii within this ratio of each other count as the same size when looking for
# the dominant radius.
BAND_CLUSTER_RATIO = 1.35


def is_contained(cx, cy, cr, accepted, frac=CONTAINED_FRAC):
    """True when (cx, cy) lies inside one of ``accepted``'s discs.

    ``accepted`` is an iterable of (x, y, r).  Only circles at least as large
    as the candidate can contain it, so a same-size neighbour whose centre
    happens to be close is judged by the caller's ordinary overlap rule, not
    by this one.
    """
    try:
        cx = float(cx)
        cy = float(cy)
        cr = float(cr)
        frac = float(frac)
    except (TypeError, ValueError):
        return False
    for a in accepted or ():
        try:
            ax, ay, ar = float(a[0]), float(a[1]), float(a[2])
        except (TypeError, ValueError, IndexError):
            continue
        if ar <= 0 or ar < cr:
            continue
        if ((cx - ax) ** 2 + (cy - ay) ** 2) ** 0.5 < frac * ar:
            return True
    return False


def dominant_radius(radii, cluster_ratio=BAND_CLUSTER_RATIO):
    """The best-supported radius in ``radii`` and how much of the set backs it.

    For each radius, counts how many others sit within ``cluster_ratio`` of it
    (either way) and returns the radius with the most support:
    ``(r_star, support_count)``.  ``(None, 0)`` on empty/invalid input.

    A plain median would be dragged down by a heavily contaminated set (24 real
    buttons + 25 letter bowls medians to a letter); the modal radius does not.
    """
    vals = []
    for r in radii or ():
        try:
            r = float(r)
        except (TypeError, ValueError):
            continue
        if r > 0:
            vals.append(r)
    if not vals:
        return None, 0
    try:
        ratio = float(cluster_ratio)
    except (TypeError, ValueError):
        ratio = BAND_CLUSTER_RATIO
    if ratio < 1.0:
        ratio = BAND_CLUSTER_RATIO

    best_r, best_n = None, 0
    for r in vals:
        lo, hi = r / ratio, r * ratio
        n = sum(1 for v in vals if lo <= v <= hi)
        # Ties go to the LARGER radius: a contaminated set's sub-features are
        # always the smaller cluster, and a real button is never a sub-feature.
        if n > best_n or (n == best_n and best_r is not None and r > best_r):
            best_r, best_n = r, n
    return best_r, best_n


def cohort_band(radii,
                *,
                lo=BAND_LO,
                hi=BAND_HI,
                min_circles=BAND_MIN_CIRCLES,
                min_support=BAND_MIN_SUPPORT,
                cluster_ratio=BAND_CLUSTER_RATIO):
    """Acceptable radius band for a circle set, or None when it can't be judged.

    Returns ``(lo_r, hi_r, r_star, support_fraction)``.  None means "leave the
    set alone": too few circles, or no radius a clear majority agrees on — the
    ambiguous case where dropping a size outlier would cost a real button.
    """
    vals = [r for r in (radii or ()) if isinstance(r, (int, float)) and r > 0]
    if len(vals) < int(min_circles):
        return None
    r_star, support = dominant_radius(vals, cluster_ratio=cluster_ratio)
    if not r_star:
        return None
    frac = support / float(len(vals))
    if frac < float(min_support):
        return None
    return r_star * float(lo), r_star * float(hi), r_star, round(frac, 4)
