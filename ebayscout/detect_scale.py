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
