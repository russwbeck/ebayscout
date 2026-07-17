"""
gemini_geometry — pure geometry for reconciling Gemini's per-button x/y against
buttonmatcher's independent Hough detection.

Gemini reports each button's CENTER as a percent of the image (top-left origin)
and, when the Gem provides it, a ``size`` (radius as a percent of the image's
smaller dimension).  Hough detection runs first and independently; this module
then:

  1. Decides which Gemini buttons are *covered* by a detected circle and which
     are *missed* (so a crop can be synthesized at the miss location) — Phase 2.
  2. Associates each final crop (detected + recovered) with its nearest Gemini
     slogan, one-to-one, so slogan confirmation is per-button by position —
     shared with Phase 3.

Everything here is PURE (math only) so it is unit-testable without cv2/numpy.
``detect.py`` consumes the synth boxes to slice actual crops; this module never
touches pixels.

SIZE CONVENTION: ``size`` is interpreted as the button RADIUS as a percent of
``min(width, height)``.  It is a calibratable hint — when absent or wrong the
caller falls back to the median detected radius, and every miss is logged.
"""

from __future__ import annotations

import math


def pct_to_px(x_pct, y_pct, w, h):
    """(x%, y%) center, top-left origin → (px, py) float pixels."""
    return (float(x_pct) / 100.0 * w, float(y_pct) / 100.0 * h)


def size_to_radius_px(size_pct, w, h):
    """Gemini ``size`` (radius as % of min dimension) → radius in pixels.

    Returns ``None`` when ``size_pct`` is missing so the caller can fall back to
    the median detected radius.
    """
    if size_pct is None:
        return None
    try:
        r = float(size_pct) / 100.0 * min(w, h)
    except (TypeError, ValueError):
        return None
    return r if r > 0 else None


def radius_from_edge(slogan, cx_px, cy_px, w, h):
    """Per-button radius (px) from a rim point: ``distance(center, edge)``.

    ``slogan`` may carry ``edge_x``/``edge_y`` — a point on the button's edge,
    radially out from its center, in the same percent coordinates as the center.
    Because that rim point is a trusted POSITION (not an absolute size estimate),
    the radius is measured by code: convert it to pixels and take its distance
    from the (already-pixel) center.  Returns ``None`` when the rim point is
    absent or degenerate, so the caller can fall back to size/spacing.
    """
    ex_pct, ey_pct = slogan.get("edge_x"), slogan.get("edge_y")
    if ex_pct is None or ey_pct is None:
        return None
    ex, ey = pct_to_px(ex_pct, ey_pct, w, h)
    r = math.hypot(ex - cx_px, ey - cy_px)
    return r if r > 0 else None


def median_radius(radii):
    """Median of positive radii, or ``None`` if none are usable."""
    rs = sorted(float(r) for r in radii if r and float(r) > 0)
    if not rs:
        return None
    n = len(rs)
    mid = n // 2
    return rs[mid] if n % 2 else (rs[mid - 1] + rs[mid]) / 2.0


def synth_box(cx, cy, r, w, h, pad_frac=0.1):
    """Clamped (x1, y1, x2, y2) box around center (cx, cy) radius r.

    Mirrors detect.py's crop recipe: pad = int(r*pad_frac), clamp to bounds.
    """
    pad = int(r * pad_frac)
    x1 = max(0, int(round(cx - r - pad)))
    x2 = min(int(w), int(round(cx + r + pad)))
    y1 = max(0, int(round(cy - r - pad)))
    y2 = min(int(h), int(round(cy + r + pad)))
    return (x1, y1, x2, y2)


def match_points(centers_a, centers_b, max_dist=None):
    """Greedy one-to-one nearest-neighbor matching between two point sets.

    Returns ``(pairs, unmatched_a, unmatched_b)`` where ``pairs`` is a list of
    ``(i, j, dist)`` sorted by increasing distance, and the unmatched lists hold
    the leftover indices.  ``max_dist`` (if given) refuses pairs farther apart.
    """
    candidates = []
    for i, (ax, ay) in enumerate(centers_a):
        for j, (bx, by) in enumerate(centers_b):
            d = math.hypot(ax - bx, ay - by)
            if max_dist is None or d <= max_dist:
                candidates.append((d, i, j))
    candidates.sort()

    used_a, used_b = set(), set()
    pairs = []
    for d, i, j in candidates:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        pairs.append((i, j, d))

    unmatched_a = [i for i in range(len(centers_a)) if i not in used_a]
    unmatched_b = [j for j in range(len(centers_b)) if j not in used_b]
    return pairs, unmatched_a, unmatched_b


# --- Frame fit (1987-front dual incident, 2026-07-17) -------------------------
# 1979-front: Gemini right, detection wrong (phantoms) — fixed by the anchoring
# gate + anchor recovery, both of which TRUST Gemini's coordinates.  1987-front
# is the DUAL: detection found the real buttons, but Gemini's y-frame was
# STRETCHED (rows reported at 19/48/76% of image height vs real 21/38/55% — it
# spread the grid over the full frame though the photo's bottom half is empty
# table).  Position-trust then inverts every guard: deficit/anchor recovery
# synthesize blank-table crops AT the wrong points, which arrive anchored-by-
# construction and auto-confirm.  The structures still agree (same columns,
# same row ORDER) — only the axis scale is off — so fit a per-axis linear map
# from Gemini's row/column centers to detection's, and apply it ONLY when it
# strictly improves physical agreement by FRAME_FIT_MIN_GAIN anchored pairs
# (a healthy lot keeps the identity map — zero risk of jitter re-fits).
FRAME_FIT_MIN_GAIN = 2
FRAME_FIT_SLOPE_RANGE = (0.4, 2.5)
FRAME_FIT_MAX_CLUSTERS = 8


def _cluster_1d(vals, gap):
    """Agglomerative 1-D clustering (the reading_order row recipe): merge the
    closest adjacent clusters until the smallest gap exceeds ``gap``.  Returns
    sorted cluster centers."""
    pts = sorted(float(v) for v in vals)
    if not pts:
        return []
    if gap <= 0:
        return [sum(pts) / len(pts)]
    clusters = [[p] for p in pts]
    while len(clusters) > 1:
        centers = [sum(c) / len(c) for c in clusters]
        g, i = min((centers[j + 1] - centers[j], j)
                   for j in range(len(clusters) - 1))
        if g > gap:
            break
        clusters[i] += clusters[i + 1]
        del clusters[i + 1]
    return [sum(c) / len(c) for c in clusters]


def _lsq_line(xs, ys):
    """Least-squares (slope, intercept) for y = a*x + b; None when degenerate."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return None
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return a, my - a * mx


def _anchored_count(det_centers, det_radii, pts, median_r, max_frac=0.75):
    """How many one-to-one NN pairs between detected crops and (mapped) Gemini
    points are physically anchored — the frame-fit objective."""
    pairs, _ua, _ub = match_points(det_centers, pts)
    n = 0
    for ci, _pj, d in pairs:
        r = None
        if det_radii and ci < len(det_radii) and det_radii[ci]:
            r = det_radii[ci]
        r = r or median_r
        if r and d <= max_frac * r:
            n += 1
    return n


def fit_frame_map(detected_centers, detected_radii, gemini_pts, median_r):
    """Per-axis linear correction of Gemini's coordinate frame.

    Clusters each axis into row/column centers on both sides, enumerates every
    order-preserving assignment of Gemini clusters to detected clusters, fits a
    line per assignment (slope sanity: FRAME_FIT_SLOPE_RANGE), and scores every
    x-candidate × y-candidate combination by the number of ANCHORED one-to-one
    pairs it produces.  Returns ``{ax, bx, ay, by, applied, anchored_identity,
    anchored_fit}`` — identity with ``applied=False`` unless the best fit beats
    identity by >= FRAME_FIT_MIN_GAIN anchored pairs.  Pure; fail-closed to
    identity on any doubt (degenerate clusters, too few points, wild slopes).
    """
    from itertools import combinations

    ident = {"ax": 1.0, "bx": 0.0, "ay": 1.0, "by": 0.0, "applied": False,
             "anchored_identity": None, "anchored_fit": None}
    pts = [p for p in gemini_pts if p is not None]
    if not median_r or median_r <= 0 or len(pts) < 4 or len(detected_centers) < 4:
        return ident
    base = _anchored_count(detected_centers, detected_radii, pts, median_r)
    ident["anchored_identity"] = ident["anchored_fit"] = base
    if base >= len(pts):
        return ident                      # already fully anchored — nothing to fix

    gap = 1.3 * median_r                  # the reading_order row threshold
    axis_candidates = []
    for axis in (0, 1):
        cands = [(1.0, 0.0)]
        d_cent = _cluster_1d([c[axis] for c in detected_centers], gap)
        g_cent = _cluster_1d([p[axis] for p in pts], gap)
        if (2 <= len(g_cent) <= len(d_cent)
                and len(d_cent) <= FRAME_FIT_MAX_CLUSTERS):
            for combo in combinations(range(len(d_cent)), len(g_cent)):
                fit = _lsq_line(g_cent, [d_cent[i] for i in combo])
                if fit and FRAME_FIT_SLOPE_RANGE[0] <= fit[0] <= FRAME_FIT_SLOPE_RANGE[1]:
                    cands.append(fit)
        axis_candidates.append(cands)

    best = ident
    best_n = base
    for ax, bx in axis_candidates[0]:
        for ay, by in axis_candidates[1]:
            if ax == 1.0 and bx == 0.0 and ay == 1.0 and by == 0.0:
                continue
            mapped = [(ax * x + bx, ay * y + by) for x, y in pts]
            n = _anchored_count(detected_centers, detected_radii, mapped, median_r)
            if n > best_n:
                best_n = n
                best = {"ax": ax, "bx": bx, "ay": ay, "by": by, "applied": True,
                        "anchored_identity": base, "anchored_fit": n}
    if best["applied"] and best_n < base + FRAME_FIT_MIN_GAIN:
        return ident                      # not a decisive improvement — keep raw
    return best


# Reconcile swap (see plan_reconciliation): the RISKY action is DROPPING a Hough
# circle, so it takes two independent signals — the circle must be BOTH unbacked
# (coverage geometry) AND off the button mask (fill < SWAP_OFF_MASK_MAX, the
# photometric signal), which together confirm a phantom and protect a real button
# Hough found off-Gemini.  RECOVERY of the swapped-in button is gated on Gemini's
# own confidence (>= SWAP_MIN_CONFIDENCE, i.e. "high"), consistent with the rest
# of the pipeline trusting Gemini's x/y — because mask-fill CAN'T confirm the
# missed button: the mask that missed it (e.g. blue-blind on a bluish carpet) also
# reads ~0 fill there.  Measured: carpet phantom fill 0.0, a real blue button 0.2
# on the same (blue-blind) mask — so fill only reliably flags phantoms, not misses.
SWAP_OFF_MASK_MAX = 0.50
SWAP_MIN_CONFIDENCE = 0.70


def plan_reconciliation(detected_centers, detected_radii, gemini_slogans, w, h,
                        cover_factor=1.0, median_r=None,
                        detected_fills=None, frame_fit=True):
    """Decide which Gemini buttons Hough missed and where to synthesize crops.

    Parameters
    ----------
    detected_centers : list[(x, y)]   pixel centers of Hough crops (crop order)
    detected_radii   : list[r]        pixel radii of those crops (same order)
    gemini_slogans   : list[dict]     {index, slogan, x, y, size, confidence}
                                      with x/y as percent (top-left origin center)
    w, h             : int            post-resize image dims detection ran on
    cover_factor     : float          a Gemini point is "covered" if a detected
                                      center is within cover_factor*median_r
    median_r         : float | None   override the computed median radius

    Returns a pure dict::

        {
          "median_r": float | None,
          "gemini_px": [(gx, gy), ...],            # per gemini slogan, px
          "misses":  [ {gemini_idx, index, slogan, confidence, gx, gy,
                        r_px, box} ],              # uncovered → synthesize crop
          "covered": [ {gemini_idx, crop_idx, dist} ],
          "unmatched_crops": [crop_idx, ...] | None,  # detected circles no
                        # Gemini point covered (placement/non-button blind
                        # spot); None when the match couldn't meaningfully run
                        # (no median radius, or zero Gemini points with valid
                        # coords) — unknown, never "all circles are unbacked".
          "telemetry": {gemini_count, hough_count, n_recovered,
                        n_unmatched_crops, covered_distances, misses:[...]},
        }

    ``gemini_idx`` is the 0-based position in ``gemini_slogans``.
    """
    if median_r is None:
        median_r = median_radius(detected_radii)

    gemini_px = []
    valid_idx = []
    for gi, s in enumerate(gemini_slogans):
        if s.get("x") is None or s.get("y") is None:
            gemini_px.append(None)
            continue
        gemini_px.append(pct_to_px(s["x"], s["y"], w, h))
        valid_idx.append(gi)

    # Frame fit (1987-front): correct a stretched/offset Gemini coordinate
    # frame BEFORE any position is trusted — coverage, deficit recovery, swap,
    # association, anchoring, and anchor recovery all consume gemini_px, so
    # this one insertion heals them together.  Identity unless the fit
    # decisively improves anchored agreement (see fit_frame_map).
    frame = None
    if frame_fit and median_r:
        frame = fit_frame_map(detected_centers, detected_radii,
                              [gemini_px[gi] for gi in valid_idx], median_r)
        if frame["applied"]:
            for gi in valid_idx:
                x, y = gemini_px[gi]
                gemini_px[gi] = (frame["ax"] * x + frame["bx"],
                                 frame["ay"] * y + frame["by"])

    # Coverage matching: only Gemini points with valid coordinates participate.
    g_points = [gemini_px[gi] for gi in valid_idx]
    cover_dist = (cover_factor * median_r) if median_r else None
    pairs, unmatched_crops, unmatched_g_local = match_points(
        detected_centers, g_points, max_dist=cover_dist
    )
    if cover_dist is None or not g_points:
        # Match couldn't meaningfully run (no median radius, or Gemini gave no
        # usable coords) — reporting "every circle is unbacked" here would be a
        # false alarm, not a real placement signal. Unknown, not zero/all.
        unmatched_crops = None

    covered = []
    for crop_idx, local_j, dist in pairs:
        covered.append({
            "gemini_idx": valid_idx[local_j],
            "crop_idx": crop_idx,
            "dist": round(dist, 2),
        })

    # Recover only the COUNT DEFICIT (Gemini buttons − detected crops) of the
    # uncovered points — never synthesize more crops than the number of buttons we
    # are actually short.  This is the robust guard against double-counting when
    # the coverage test mis-aligns (e.g. the projection-grid fallback path, whose
    # crop coords don't match the cover threshold).  Among the uncovered points we
    # recover the ones FARTHEST from any detected center (most likely real misses).
    deficit = max(0, len(gemini_slogans) - len(detected_centers))

    def _nearest_det_dist(p):
        if not detected_centers:
            return float("inf")
        return min(math.hypot(p[0] - cx, p[1] - cy) for cx, cy in detected_centers)

    uncovered = sorted(
        ((_nearest_det_dist(gemini_px[valid_idx[lj]]), valid_idx[lj])
         for lj in unmatched_g_local),
        reverse=True,
    )
    recover_gis = [gi for _d, gi in uncovered[:deficit]]

    # --- Two-signal swap: recover a suppressed miss, drop the phantom ---------
    # The deficit cap above silently drops a real Gemini miss whenever a HOUGH
    # FALSE-POSITIVE fills the count (measured: a carpet phantom kept the count at
    # 5 and suppressed a Gemini-located blue button).  Trading them is safe only
    # when the circle we DROP is confidently a phantom, so gate the drop on TWO
    # independent signals: it is unbacked (in ``unmatched_crops`` — coverage
    # geometry) AND sits OFF the button mask (fill < SWAP_OFF_MASK_MAX — the
    # photometric signal; a real button Hough found off-Gemini would score HIGH
    # fill and be kept).  RECOVER the swapped-in button only when Gemini was highly
    # confident (>= SWAP_MIN_CONFIDENCE) — mask-fill can't vouch for the miss (the
    # mask that missed it reads ~0 there), but the whole pipeline already trusts
    # Gemini's positions, so its own confidence is the recovery gate.  1 in, 1 out
    # keeps the count invariant (the deficit cap's double-count guard holds), and
    # on a flooded mask the phantom scores HIGH fill so nothing is dropped.
    dropped_crop_indices = []
    swaps = []
    if detected_fills is not None and unmatched_crops:
        _phantoms = sorted(
            (ci for ci in unmatched_crops
             if ci < len(detected_fills) and detected_fills[ci] is not None
             and detected_fills[ci] < SWAP_OFF_MASK_MAX),
            key=lambda ci: detected_fills[ci],          # emptiest first
        )

        def _conf(gi):
            c = gemini_slogans[gi].get("confidence")
            return c if isinstance(c, (int, float)) else 0.0

        _recoverable = [
            gi for _d, gi in uncovered                  # already farthest-first
            if gi not in recover_gis and _conf(gi) >= SWAP_MIN_CONFIDENCE
        ]
        # DROP every off-mask phantom (unbacked AND off the button mask = a Hough
        # false-positive, whether or not a Gemini button can replace it: Gemini
        # located all its buttons, so an unbacked off-mask circle is not one it
        # merely missed).  PAIR each with an uncovered high-confidence miss where
        # one exists (a true swap, count invariant); UNPAIRED phantoms are dropped
        # outright — they only inflated the count (e.g. a lone real button + one
        # carpet phantom → drop the phantom, keep the 1).  Every drop is a labeled
        # phantom example; `recovered` flags whether a button took its place.
        for _k, ci in enumerate(_phantoms):
            gi = _recoverable[_k] if _k < len(_recoverable) else None
            if gi is not None:
                recover_gis.append(gi)
            dropped_crop_indices.append(ci)
            _pcx, _pcy = detected_centers[ci]
            swaps.append({
                "slogan": (gemini_slogans[gi].get("slogan") if gi is not None else None),
                "confidence": (gemini_slogans[gi].get("confidence") if gi is not None else None),
                "recovered": gi is not None,
                "phantom_x": int(round(_pcx)),
                "phantom_y": int(round(_pcy)),
                "phantom_r": int(round(detected_radii[ci])) if detected_radii[ci] else None,
                "phantom_fill": round(detected_fills[ci], 3),
            })

    misses = []
    fallback_r = median_r if median_r else max(1.0, 0.04 * min(w, h))
    for gi in recover_gis:
        s = gemini_slogans[gi]
        gx, gy = gemini_px[gi]
        # Prefer a rim-point radius (two trusted positions); clamp to 0.25–3× the
        # base so a stray edge point can't blow up the crop.  Fall back to the
        # numeric size, then the base radius.
        # The rim point shares Gemini's (possibly corrected) frame — when a
        # frame map was applied, map the edge point too before measuring, or
        # the mixed-frame distance inflates/deflates the radius.
        if frame is not None and frame.get("applied"):
            edge_r = None
            if s.get("edge_x") is not None and s.get("edge_y") is not None:
                ex, ey = pct_to_px(s["edge_x"], s["edge_y"], w, h)
                _er = math.hypot(frame["ax"] * ex + frame["bx"] - gx,
                                 frame["ay"] * ey + frame["by"] - gy)
                edge_r = _er if _er > 0 else None
        else:
            edge_r = radius_from_edge(s, gx, gy, w, h)
        if edge_r is not None:
            r_px = min(3.0 * fallback_r, max(0.25 * fallback_r, edge_r))
        else:
            r_px = size_to_radius_px(s.get("size"), w, h) or fallback_r
        box = synth_box(gx, gy, r_px, w, h)
        misses.append({
            "gemini_idx": gi,
            "index": s.get("index"),
            "slogan": s.get("slogan"),
            "confidence": s.get("confidence"),
            "gx": round(gx, 2),
            "gy": round(gy, 2),
            "r_px": round(r_px, 2),
            "box": box,
        })

    # --- Gemini-anchored A/B shadow (measurement only; changes no output) -----
    # The reconcile match already IS the comparison "would anchoring crops on
    # Gemini's x/y beat Hough?": every COVERED button is one both agree on, and the
    # match distance is how far Gemini's centre is from Hough's — the precision
    # signal that decides whether Gemini could anchor the crop.  n_gemini_only =
    # buttons Hough missed (anchoring would ADD); n_hough_only = Hough circles no
    # Gemini point backs (anchoring would DROP — the phantoms).  Join per-lot to
    # confirm_log over time to learn when to default to Gemini x/y (tested_hyp §4.8).
    _snap = sorted(c["dist"] for c in covered)
    _snap_med = _snap[len(_snap) // 2] if _snap else None
    gemini_anchored = {
        "n_gemini": len(gemini_slogans),
        "n_agree": len(covered),
        "snap_px_median": _snap_med,
        "snap_px_max": (_snap[-1] if _snap else None),
        "snap_frac_median": (round(_snap_med / median_r, 3)
                             if (_snap_med is not None and median_r) else None),
        "n_gemini_only": len(unmatched_g_local),
        "n_hough_only": (len(unmatched_crops) if unmatched_crops is not None else None),
    }

    telemetry = {
        "gemini_count": len(gemini_slogans),
        "hough_count": len(detected_centers),
        "frame_fit": ({k: (round(v, 4) if isinstance(v, float) else v)
                       for k, v in frame.items()} if frame is not None else None),
        "deficit": deficit,
        "n_uncovered": len(unmatched_g_local),
        "n_recovered": len(misses),
        "n_swapped": len(dropped_crop_indices),
        "swaps": swaps,
        "gemini_anchored": gemini_anchored,
        "n_unmatched_crops": len(unmatched_crops) if unmatched_crops is not None else None,
        "median_r": round(median_r, 2) if median_r else None,
        "covered_distances": [c["dist"] for c in covered],
        "misses": [
            {"slogan": m["slogan"], "gx": m["gx"], "gy": m["gy"], "r_px": m["r_px"]}
            for m in misses
        ],
    }

    return {
        "median_r": median_r,
        "gemini_px": gemini_px,
        "misses": misses,
        "covered": covered,
        "unmatched_crops": unmatched_crops,
        "dropped_crop_indices": dropped_crop_indices,
        "telemetry": telemetry,
    }


def assoc_anchored(dist, r_px, max_frac=0.75):
    """Is a crop→slogan association PHYSICALLY ANCHORED — Gemini's point on
    the crop it would confirm?

    The 2026-07-16 shifted-lot incident: a detection grid displaced by one
    position still nearest-neighbor-pairs every slogan with SOME crop, and
    agreement auto-confirm then commits neighbors' slogans (and blank
    crops).  Correct associations measure ~0.07×radius (snap_frac_median,
    det_gemini_anchored_json); wrong-neighbor pairs ~2×radius — so a
    dist ≤ ``max_frac``×radius gate separates them with a wide margin.
    Fail-open: an unknown dist or radius counts as anchored (the gate must
    never break lots that predate the telemetry)."""
    if dist is None or r_px is None:
        return True
    try:
        return float(dist) <= float(max_frac) * float(r_px)
    except (TypeError, ValueError):
        return True


def plan_anchor_recovery(final_centers, final_radii, crop_to_slogan, gemini_px,
                         gemini_slogans, median_r, max_frac=0.75):
    """Gemini indices whose slogans should get a SYNTHESIZED crop because their
    association is unanchored and their point sits clear of every crop.

    The 1979-front incident's second half: the anchoring gate stops the wrong
    AUTOs, but the real buttons the phantoms displaced stay silently absent
    (deficit = 0, and on a flooded mask the fill-gated swap can't fire).  An
    UNANCHORED pair is itself the recovery evidence the swap lacks there: a
    crop no Gemini point explains + a slogan no crop explains.  When that
    slogan's point is ALSO clear of every final crop center (> ``median_r`` —
    same coverage notion as plan_reconciliation, so a merely sloppy-but-correct
    pair can never double-count) and Gemini was confident
    (>= SWAP_MIN_CONFIDENCE, the same trust gate as the swap), synthesize a
    crop at the point.  No crop is dropped — the phantom stays, demoted to a
    manual card by the anchoring gate.  Pure; the caller slices pixels.
    """
    if not median_r or median_r <= 0:
        return []
    out = []
    for crop_idx, assoc in crop_to_slogan.items():
        r = None
        if final_radii is not None and 0 <= crop_idx < len(final_radii):
            r = final_radii[crop_idx]
        if assoc_anchored(assoc.get("dist"), r or median_r, max_frac):
            continue
        gi = assoc.get("gemini_idx")
        if gi is None or not (0 <= gi < len(gemini_px)) or gemini_px[gi] is None:
            continue
        conf = gemini_slogans[gi].get("confidence")
        if not isinstance(conf, (int, float)) or conf < SWAP_MIN_CONFIDENCE:
            continue
        gx, gy = gemini_px[gi]
        if final_centers and min(
                math.hypot(gx - cx, gy - cy) for cx, cy in final_centers
        ) <= median_r:
            continue  # point overlaps an existing crop — would double-count
        out.append(gi)
    return out


def associate_slogans(final_centers, gemini_px, gemini_slogans, max_dist=None):
    """Link each final crop (detected + recovered) to its nearest Gemini slogan.

    One-to-one by position, so button 5's text can't confirm button 2.

    Parameters
    ----------
    final_centers  : list[(x, y)]   pixel centers of ALL final crops (crop order)
    gemini_px      : list[(x,y)|None]  pixel centers per Gemini slogan (from
                                       plan_reconciliation), None when no coords
    gemini_slogans : list[dict]     the Gemini slogan dicts (same order as gemini_px)
    max_dist       : float | None   refuse associations farther than this

    Returns ``(crop_to_slogan, unmatched_gemini_idx)`` where ``crop_to_slogan``
    maps ``crop_idx`` → ``{gemini_idx, index, slogan, confidence, dist}`` for the
    crops that got an association.
    """
    valid_idx = [gi for gi, p in enumerate(gemini_px) if p is not None]
    g_points = [gemini_px[gi] for gi in valid_idx]

    pairs, _unmatched_crops, unmatched_g_local = match_points(
        final_centers, g_points, max_dist=max_dist
    )

    crop_to_slogan = {}
    for crop_idx, local_j, dist in pairs:
        gi = valid_idx[local_j]
        s = gemini_slogans[gi]
        crop_to_slogan[crop_idx] = {
            "gemini_idx": gi,
            "index": s.get("index"),
            "slogan": s.get("slogan"),
            "confidence": s.get("confidence"),
            "printed_year": s.get("printed_year"),
            "dist": round(dist, 2),
        }

    unmatched_gemini_idx = [valid_idx[j] for j in unmatched_g_local]
    return crop_to_slogan, unmatched_gemini_idx
