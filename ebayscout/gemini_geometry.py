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
                        detected_fills=None):
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

        _swap_gis = [
            gi for _d, gi in uncovered                  # already farthest-first
            if gi not in recover_gis and _conf(gi) >= SWAP_MIN_CONFIDENCE
        ]
        for gi, ci in zip(_swap_gis, _phantoms):
            recover_gis.append(gi)
            dropped_crop_indices.append(ci)
            # One labeled Hough-phantom example per swap: the dropped circle's
            # position/size/fill (what fooled Hough) + the button it stood in for.
            _pcx, _pcy = detected_centers[ci]
            swaps.append({
                "slogan": gemini_slogans[gi].get("slogan"),
                "confidence": gemini_slogans[gi].get("confidence"),
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

    telemetry = {
        "gemini_count": len(gemini_slogans),
        "hough_count": len(detected_centers),
        "deficit": deficit,
        "n_uncovered": len(unmatched_g_local),
        "n_recovered": len(misses),
        "n_swapped": len(dropped_crop_indices),
        "swaps": swaps,
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
            "dist": round(dist, 2),
        }

    unmatched_gemini_idx = [valid_idx[j] for j in unmatched_g_local]
    return crop_to_slogan, unmatched_gemini_idx
