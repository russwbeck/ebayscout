"""Button detection (OpenCV) — split out of main.py.

Holds the full detection pipeline: shared image preparation (resize, glare
inpaint, background sampling, HSV + colour-vs-background masking), the guided
detector (detect_buttons — drives cropping when the user supplies count/grid),
and the unguided detector (detect_unguided / count_circles_unguided — the
automation path, scale-first with a multi-pass sweep fallback).

Lives in its own module so the offline eval harness (tools/eval_replay.py) can
import detection without main.py's import-time secret fetches, Slack client,
and CLIP model load.  This module imports only cv2/numpy plus the pure-python
helpers (detect_mask, detect_scale, detect_gate).
"""

import json
import math
import os
import statistics

import cv2
import numpy as np

from . import detect_mask as dmask
from . import detect_scale as dscale
from . import detect_gate as dgate
from . import gemini_geometry as ggeo


# --- ENV FLAGS (instant rollback without redeploy) ---------------------------

def _flag_on(*names, default="1"):
    """True unless any listed env var is explicitly set to a falsey token.
    ebayscout's own EBAYSCOUT_* names win; the shared BUTTONMATCHER_* names are
    honored too so a single rollback toggle can cover both services."""
    falsey = ("0", "false", "False", "")
    for n in names:
        v = os.environ.get(n)
        if v is not None:
            return v.strip() not in falsey
    return default not in falsey


def _bg_diff_enabled():
    """Colour-vs-background mask is on by default; EBAYSCOUT_BG_DIFF=0 (or the
    shared BUTTONMATCHER_BG_DIFF=0) disables it without a redeploy."""
    return _flag_on("EBAYSCOUT_BG_DIFF", "BUTTONMATCHER_BG_DIFF")


def _blob_buster_enabled():
    """Distance-transform splitting of touching buttons is on by default;
    EBAYSCOUT_BLOB_BUSTER=0 (or BUTTONMATCHER_BLOB_BUSTER=0) disables it."""
    return _flag_on("EBAYSCOUT_BLOB_BUSTER", "BUTTONMATCHER_BLOB_BUSTER")


def _auto_detect_enabled():
    """Auto detection (no count prompt) — default OFF until the gate is
    calibrated; BUTTONMATCHER_AUTO_DETECT=1 enables."""
    return os.environ.get("BUTTONMATCHER_AUTO_DETECT", "0").strip() in (
        "1", "true", "True",
    )


# --- SHARED IMAGE PREP --------------------------------------------------------

def _prepare_detection_image(image_bgr, diag_out=None):
    """Shared preprocessing for BOTH the guided and unguided detectors.

    Resize (≤800px), glare inpaint, border background sample, HSV mask
    (blue-only on white backgrounds, blue+white otherwise), colour-vs-background
    union mask (uniform backgrounds only, gated by BUTTONMATCHER_BG_DIFF),
    morphology cleanup, Gaussian blur, and the mask connected-component count.

    Historically detect_buttons had all of this while count_circles_unguided
    had only the blue/white HSV mask — so the unguided detector was blind to
    maroon/green buttons and anything bg-diff rescues.  Sharing the prep closes
    that gap (telemetry column ni_bgdiff marks rows produced after the fix).

    Returns a dict:
        img, img_noglare, h, w, mask, gray, fill_threshold,
        white_bg, bg_mean_s, bg_mean_v, bg_diff_used, mask_components
    """
    img = image_bgr
    h, w = img.shape[:2]

    max_dim = 800
    scale = min(max_dim / w, max_dim / h, 1.0)
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
        h, w = img.shape[:2]

    # Glare suppression
    gray_orig = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    thresh_val = np.percentile(gray_orig, 98)
    _, glare_mask = cv2.threshold(gray_orig, thresh_val, 255, cv2.THRESH_BINARY)
    glare_fraction = np.count_nonzero(glare_mask) / glare_mask.size
    if glare_fraction > 0.10:
        img_noglare = img
    else:
        img_noglare = cv2.inpaint(img, glare_mask, 5, cv2.INPAINT_TELEA)

    # Background detection — sample an 8% border strip
    _bw = max(1, int(min(h, w) * 0.08))
    _border = np.concatenate([
        img_noglare[:_bw, :].reshape(-1, 3),
        img_noglare[h - _bw:, :].reshape(-1, 3),
        img_noglare[:, :_bw].reshape(-1, 3),
        img_noglare[:, w - _bw:].reshape(-1, 3),
    ])
    _hsv_b = cv2.cvtColor(_border.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    bg_mean_s = float(np.mean(_hsv_b[:, 1]))
    bg_mean_v = float(np.mean(_hsv_b[:, 2]))
    white_bg = bg_mean_s < 65 and bg_mean_v > 170

    if diag_out is not None:
        diag_out["h"] = int(h)
        diag_out["w"] = int(w)
        diag_out["bg_brightness"] = bg_mean_v
        diag_out["bg_saturation"] = bg_mean_s
        diag_out["bg_is_white"] = bool(white_bg)
        diag_out["mask_path"] = "blue_only" if white_bg else "blue_or_white"

        # Priority 4 — whole-image quality (computed only when logging so
        # cropping-only calls pay nothing).
        try:
            _p4_gray = cv2.cvtColor(img_noglare, cv2.COLOR_BGR2GRAY)
            _p4_canny = cv2.Canny(_p4_gray, 50, 150)
            diag_out["edge_density"] = (
                float(np.count_nonzero(_p4_canny)) / max(1, _p4_canny.size)
            )
            _p4_hsv = cv2.cvtColor(img_noglare, cv2.COLOR_BGR2HSV)
            diag_out["brightness_std"] = float(np.std(_p4_hsv[:, :, 2]))
        except Exception as _p4_err:
            print(f">>> DETECT: Priority-4 metrics failed ({_p4_err}).", flush=True)

    # HSV colour mask — adapt to background
    hsv = cv2.cvtColor(img_noglare, cv2.COLOR_BGR2HSV)
    lower_blue = np.array([90, 70, 40])
    upper_blue = np.array([140, 255, 255])
    if white_bg:
        mask = cv2.inRange(hsv, lower_blue, upper_blue)
        fill_threshold = 0.30   # only the blue ring is in mask on white paper
    else:
        mask = cv2.bitwise_or(
            cv2.inRange(hsv, lower_blue, upper_blue),
            cv2.inRange(hsv, np.array([0, 0, 140]), np.array([180, 70, 255])),
        )
        fill_threshold = 0.55

    # Colour-vs-background mask (catches non-blue buttons on uniform backgrounds)
    bg_diff_used = False
    if _bg_diff_enabled():
        try:
            _lab = cv2.cvtColor(img_noglare, cv2.COLOR_BGR2LAB)
            _lab_border = np.concatenate([
                _lab[:_bw, :].reshape(-1, 3),
                _lab[h - _bw:, :].reshape(-1, 3),
                _lab[:, :_bw].reshape(-1, 3),
                _lab[:, w - _bw:].reshape(-1, 3),
            ]).astype(np.float32)
            _bg_lab = np.median(_lab_border, axis=0)
            _bg_spread = float(np.mean(np.std(_lab_border, axis=0)))
            if dmask.should_use_bg_diff(_bg_spread):
                _thr = dmask.bg_diff_threshold(_bg_spread)
                _dist = np.linalg.norm(_lab.astype(np.float32) - _bg_lab, axis=2)
                _bgm = (_dist > _thr).astype(np.uint8) * 255
                mask = cv2.bitwise_or(mask, _bgm)
                bg_diff_used = True
            print(f">>> DETECT: bg-diff spread={_bg_spread:.1f} "
                  f"(<= {dmask.BG_DIFF_MAX_SPREAD} ⇒ uniform) → "
                  f"{'APPLIED' if bg_diff_used else 'skipped (textured bg)'}",
                  flush=True)
        except Exception as _bd_err:
            print(f">>> DETECT: bg-diff mask failed ({_bd_err}); colour mask only.",
                  flush=True)
    if diag_out is not None:
        diag_out["mask_path"] = dmask.mask_path_label(
            diag_out.get("mask_path", ""), bg_diff_used
        )

    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    gray = cv2.GaussianBlur(mask, (9, 9), 2)

    # Mask connected-component count (cheap localization-quality signal):
    #   components >> count → mask fragments buttons; << count → buttons merged.
    mask_components = None
    try:
        _n_lbl, _, _stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        _min_area = max(1, int(h * w * 0.001))
        mask_components = int(sum(
            1 for _i in range(1, _n_lbl) if _stats[_i, cv2.CC_STAT_AREA] >= _min_area
        ))
    except Exception as _cc_err:
        print(f">>> DETECT: connectedComponents failed: {_cc_err}", flush=True)
    if diag_out is not None:
        diag_out["mask_components"] = mask_components

    return {
        "img": img,
        "img_noglare": img_noglare,
        "h": h,
        "w": w,
        "mask": mask,
        "gray": gray,
        "fill_threshold": fill_threshold,
        "white_bg": white_bg,
        "bg_mean_s": bg_mean_s,
        "bg_mean_v": bg_mean_v,
        "bg_diff_used": bg_diff_used,
        "mask_components": mask_components,
    }


# --- GRID HELPERS -------------------------------------------------------------

def _infer_grid_from_count(count, h, w):
    """Return (rows, cols) whose cell aspect ratio best matches the image."""
    best, best_score = (1, count), float('inf')
    for r in range(1, count + 1):
        if count % r != 0:
            continue
        c = count // r
        cell_ar = (w / c) / (h / r) if r > 0 and c > 0 else 1.0
        score = abs(math.log(max(cell_ar, 1e-9)))
        if score < best_score:
            best_score, best = score, (r, c)
    return best


def _find_band_centres(coords, r_median):
    """Split sorted 1-D coordinates into bands at gaps larger than 1.5× the
    median gap (and at least 1.5 button radii); return each band's centre.
    Shared by _estimate_layout and the outlier-demotion step.

    The radius floor is 1.5×r, NOT 2×r: adjacent rows of touching buttons sit
    exactly one diameter (2r) apart, so a 2r floor could never split them —
    tight grids collapsed to 1×1 with layout_conf 0.  Within-row centre jitter
    is a small fraction of r, so 1.5r still never splits a genuine row.
    """
    coords = sorted(coords)
    if len(coords) <= 1:
        return [float(coords[0])] if coords else []
    gaps = [coords[i + 1] - coords[i] for i in range(len(coords) - 1)]
    med_gap = float(np.median(gaps))
    threshold = max(med_gap * 1.5, r_median * 1.5)
    bands, current = [], [coords[0]]
    for i, g in enumerate(gaps):
        if g > threshold:
            bands.append(current)
            current = [coords[i + 1]]
        else:
            current.append(coords[i + 1])
    bands.append(current)
    return [float(np.mean(b)) for b in bands]


# --- BUTTON DETECTION (unguided helpers) ------------------------------------

def _score_solution(circles, mask, fill_threshold, h, w):
    """Score a candidate circle set on four internal-quality criteria.

    Returns a float in [0, 1].  Higher = more likely to be the correct count.

    Criteria (weighted sum)
    -----------------------
    fill_mean (0.40)
        Average blue-area fill ratio.  Genuine button circles are mostly blue;
        noise circles score poorly here.

    spacing_cv (0.30)
        1 - CV of nearest-neighbour distances.  A real grid has tight, uniform
        spacing; scattered false positives have high spacing variance.

    radius_cv (0.20)
        1 - CV of radii.  A genuine button grid has near-identical button sizes.

    coverage (0.10)
        Tent function peaking at ~40 % image coverage — neither nearly empty
        nor implausibly dense.
    """
    if not circles:
        return 0.0

    n = len(circles)
    xs = np.array([c[0] for c in circles], dtype=float)
    ys = np.array([c[1] for c in circles], dtype=float)
    rs = np.array([c[2] for c in circles], dtype=float)

    # fill_mean
    fills = []
    for (cx, cy, cr) in circles:
        cm = np.zeros(mask.shape, dtype=np.uint8)
        cv2.circle(cm, (int(cx), int(cy)), int(cr), 255, -1)
        blue = cv2.countNonZero(cv2.bitwise_and(mask, mask, mask=cm))
        fills.append(blue / max(1.0, math.pi * cr * cr))
    fill_mean = float(np.mean(fills))

    # radius_cv
    r_mean = float(np.mean(rs))
    r_cv   = float(np.std(rs)) / r_mean if r_mean > 0 else 1.0
    radius_score = max(0.0, 1.0 - r_cv)

    # spacing_cv
    if n >= 2:
        centres  = np.stack([xs, ys], axis=1)
        diffs    = centres[:, None, :] - centres[None, :, :]
        dists    = np.sqrt((diffs ** 2).sum(axis=2))
        np.fill_diagonal(dists, np.inf)
        nn_dists = dists.min(axis=1)
        sp_mean  = float(np.mean(nn_dists))
        sp_cv    = float(np.std(nn_dists)) / sp_mean if sp_mean > 0 else 1.0
        spacing_score = max(0.0, 1.0 - sp_cv)
    else:
        spacing_score = 0.5   # neutral — can't judge with a single circle

    # coverage (tent, peaks at 0.40)
    r_median = float(np.median(rs))
    coverage = (n * math.pi * r_median ** 2) / (h * w)
    if coverage <= 0.40:
        cov_score = coverage / 0.40
    else:
        cov_score = max(0.0, 1.0 - (coverage - 0.40) / 0.60)

    composite = (
        0.40 * fill_mean     +
        0.30 * spacing_score +
        0.20 * radius_score  +
        0.10 * cov_score
    )
    return round(float(composite), 4)


def _estimate_layout(circles, h, w):
    """Infer row/column structure from circle centres using 1-D gap analysis.

    Returns
    -------
    est_rows    int    inferred row count
    est_cols    int    inferred column count
    layout_conf float  fraction of circles within 1.2 radii of a grid point
    outliers    int    circles that don't fit any grid intersection
    """
    if not circles:
        return 1, 1, 0.0, 0

    r_median = float(np.median([c[2] for c in circles]))
    row_centres = _find_band_centres([c[1] for c in circles], r_median)
    col_centres = _find_band_centres([c[0] for c in circles], r_median)

    snap_radius = r_median * 1.2
    inliers = sum(
        1 for (cx, cy, _) in circles
        if any(
            math.hypot(cx - gc, cy - gr) <= snap_radius
            for gr in row_centres
            for gc in col_centres
        )
    )
    n = len(circles)
    layout_conf = round(inliers / n, 4) if n > 0 else 0.0
    return len(row_centres), len(col_centres), layout_conf, n - inliers


def _run_hough_pass(gray, mask, h, w, base_r, fill_threshold, param2,
                    scales=(2.2, 1.6, 1.1, 0.8, 0.55)):
    """Run one Hough pass at the given radius scales and return filtered circles.

    The blind multi-scale sweep uses the default five scales; the scale-first
    path passes scales=(1.0,) with base_r set to the estimated button radius —
    which makes this exactly the guided detect_buttons Hough geometry.

    Applies the same fill-ratio filter and overlap/inner-circle dedup used in
    detect_buttons so pass counts are directly comparable to the guided result.
    """
    all_raw = []
    for s in scales:
        r = int(base_r * s)
        if r < 8:
            continue
        c = cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT, dp=1.3,
            minDist=max(8, int(r * 1.7)),
            param1=120, param2=param2,
            minRadius=int(r * 0.7),
            maxRadius=int(r * 1.3),
        )
        if c is not None:
            all_raw.extend(np.around(c[0]).astype(int).tolist())

    if not all_raw:
        return []

    margin = int(min(h, w) * 0.05)
    cand = [
        (x, y, r) for (x, y, r) in all_raw
        if margin < x < w - margin and margin < y < h - margin
    ]

    filtered = []
    for (x, y, r) in sorted(cand, key=lambda c: c[2], reverse=True):
        cm = np.zeros(mask.shape, dtype=np.uint8)
        cv2.circle(cm, (x, y), r, 255, -1)
        blue = cv2.countNonZero(cv2.bitwise_and(mask, mask, mask=cm))
        if blue / max(1.0, math.pi * r * r) < fill_threshold:
            continue
        if not any(
            math.hypot(x - fx, y - fy) < min(r, fr) * 0.7
            for fx, fy, fr in filtered
        ):
            filtered.append((x, y, r))

    # Remove inner circles (same button detected at two different scales)
    cleaned = [
        (x1, y1, r1) for i, (x1, y1, r1) in enumerate(filtered)
        if not any(
            i != j
            and math.hypot(x1 - x2, y1 - y2) < r2 * 0.3
            and r1 < r2 * 0.6
            for j, (x2, y2, r2) in enumerate(filtered)
        )
    ]
    return cleaned


def _contour_circle_proposals(mask, min_r, max_r, margin, fill_threshold, h, w):
    """Propose circle candidates from HSV mask contours.

    Finds external contours in the mask, fits a minimum enclosing circle to
    each, and keeps those that pass three filters:
        - radius within [min_r, max_r]
        - circularity (4π·area / perimeter²) ≥ 0.45 — rejects elongated blobs
        - centre outside the image margin

    These candidates are in the same (x, y, r) format as Hough output and are
    merged with the Hough winner set in detect_unguided.  Contours complement
    Hough specifically on low-contrast images where the gradient accumulator is
    weak but the blue region boundary is still detectable.

    Returns a list of (x, y, r) tuples (may be empty).
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    proposals = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < math.pi * min_r * min_r * 0.5:   # too small even at min radius
            continue
        perim = cv2.arcLength(cnt, closed=True)
        if perim < 1.0:
            continue
        circularity = (4.0 * math.pi * area) / (perim * perim)
        if circularity < 0.45:
            continue
        (cx, cy), cr = cv2.minEnclosingCircle(cnt)
        cx, cy, cr = int(round(cx)), int(round(cy)), int(round(cr))
        if cr < min_r or cr > max_r:
            continue
        if not (margin < cx < w - margin and margin < cy < h - margin):
            continue
        proposals.append((cx, cy, cr))
    return proposals


def _merge_circle_sets(primary, secondary, fill_threshold, mask):
    """Merge two (x, y, r) circle lists, keeping all primary circles and adding
    secondary circles that are not already covered by a primary circle.

    A secondary circle is considered covered if its centre is within
    0.7 × min(r_primary, r_secondary) of any primary circle — the same overlap
    rule used throughout the detection pipeline.  Secondary circles that pass
    the merge are also required to pass the fill-ratio check so noise contour
    proposals don't inflate the count.

    Returns the merged list (primary circles first, then accepted secondary).
    """
    merged = list(primary)
    for (sx, sy, sr) in secondary:
        # Skip if overlapping with any already-accepted circle
        if any(
            math.hypot(sx - mx, sy - my) < min(sr, mr) * 0.7
            for mx, my, mr in merged
        ):
            continue
        # Fill-ratio check on the candidate
        cm = np.zeros(mask.shape, dtype=np.uint8)
        cv2.circle(cm, (sx, sy), sr, 255, -1)
        blue = cv2.countNonZero(cv2.bitwise_and(mask, mask, mask=cm))
        if blue / max(1.0, math.pi * sr * sr) < fill_threshold:
            continue
        merged.append((sx, sy, sr))
    return merged


def _distance_peak_proposals(mask, expected_r, min_r, max_r, margin, h, w):
    """Propose a circle at each distance-transform peak of the mask (blob-buster).

    Separates touching buttons that have merged into one mask blob: every button
    centre is a local maximum of the distance-to-background transform, while the
    neck where two buttons touch is a saddle with a smaller distance.  Keeping
    only mask pixels whose distance clears a large fraction of a button radius
    isolates each centre "core"; that core's centroid + distance give (centre,
    radius).  De-duplication of cores is the pure-python detect_mask.select_peaks.

    Returns a list of (x, y, r) tuples (may be empty), same format as Hough.
    """
    try:
        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    except Exception as _dt_err:
        print(f">>> DETECT: distanceTransform failed ({_dt_err}); no blob-buster.",
              flush=True)
        return []
    # The neck between two touching buttons has distance < their radius, so a
    # threshold near the radius splits the merged blob into one core per button.
    core_thresh = max(1.0, 0.55 * expected_r)
    cores = (dist >= core_thresh).astype(np.uint8)
    if cv2.countNonZero(cores) == 0:
        return []
    _n, _labels, _stats, _centroids = cv2.connectedComponentsWithStats(cores, connectivity=8)
    candidates = []
    for i in range(1, _n):
        cx, cy = _centroids[i]
        cx, cy = int(round(cx)), int(round(cy))
        if not (0 <= cy < h and 0 <= cx < w):
            continue
        r = dmask.clamp_radius(float(dist[cy, cx]), min_r, max_r)
        candidates.append((cx, cy, r))
    # NMS: collapse cores that are the same button; a button-diameter separation
    # keeps genuinely-touching neighbours distinct.
    kept = dmask.select_peaks(candidates, min_separation=expected_r * 1.5)
    return [(x, y, r) for (x, y, r) in kept
            if margin < x < w - margin and margin < y < h - margin]


def _build_clahe_mask(img_noglare, white_bg):
    """Build an alternative HSV mask from a CLAHE-enhanced image.

    Converts the image to LAB colour space, applies CLAHE (Contrast Limited
    Adaptive Histogram Equalization) to the L channel, then converts back to
    BGR and runs the standard HSV masking pipeline.  CLAHE boosts local
    contrast without shifting hue, making buttons stand out more clearly
    against similar-coloured or low-contrast backgrounds.

    Returns (mask, gray) ready to feed directly into _run_hough_pass.
    """
    lab = cv2.cvtColor(img_noglare, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe        = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l_enhanced   = clahe.apply(l_ch)
    img_enhanced = cv2.cvtColor(
        cv2.merge([l_enhanced, a_ch, b_ch]), cv2.COLOR_LAB2BGR
    )

    hsv        = cv2.cvtColor(img_enhanced, cv2.COLOR_BGR2HSV)
    lower_blue = np.array([90,  70,  40])
    upper_blue = np.array([140, 255, 255])
    if white_bg:
        mask = cv2.inRange(hsv, lower_blue, upper_blue)
    else:
        mask = cv2.bitwise_or(
            cv2.inRange(hsv, lower_blue, upper_blue),
            cv2.inRange(hsv, np.array([0, 0, 140]), np.array([180, 70, 255])),
        )

    kernel = np.ones((5, 5), np.uint8)
    mask   = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask   = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  kernel)
    gray   = cv2.GaussianBlur(mask, (9, 9), 2)
    return mask, gray


# --- SCALE-FIRST RADIUS ESTIMATION --------------------------------------------

def estimate_button_scale(mask, h, w):
    """Estimate the button radius directly from the detection mask.

    The user's count/grid input is valuable only because it implies the
    expected Hough radius; this synthesizes that input from the image instead:

    1. Fill mask holes (white slogan text / bank band punch holes in the blue
       mask, which deflate any blob-based radius read) by redrawing external
       contours filled.
    2. Per filled blob, measure three radius estimates — distance-transform
       peak, min-enclosing circle, sqrt(area/π) — plus circularity.
    3. detect_scale turns the per-blob measurements into weighted votes
       (merged blobs of touching buttons vote at their DT peak) and a
       consensus radius + confidence.

    Returns (r_est, scale_conf, filled_mask, sdiag) where r_est is None when
    no usable blobs exist.  filled_mask is reused for blob-buster proposals.
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(mask)
    if contours:
        cv2.drawContours(filled, contours, -1, 255, thickness=cv2.FILLED)

    sdiag = {"blobs": 0, "votes": 0, "merged_blobs": 0}
    if not contours:
        return None, 0.0, filled, sdiag

    try:
        dist = cv2.distanceTransform(filled, cv2.DIST_L2, 5)
    except Exception as _dt_err:
        print(f">>> DETECT: scale DT failed ({_dt_err}).", flush=True)
        return None, 0.0, filled, sdiag

    n_lbl, labels, stats, _cents = cv2.connectedComponentsWithStats(
        filled, connectivity=8
    )
    min_area = math.pi * (0.02 * min(h, w)) ** 2
    votes = []
    n_blobs = 0
    for i in range(1, n_lbl):
        area_px = stats[i, cv2.CC_STAT_AREA]
        if area_px < min_area:
            continue
        n_blobs += 1
        blob = (labels == i).astype(np.uint8)
        bc, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not bc:
            continue
        cnt = max(bc, key=cv2.contourArea)
        area = cv2.contourArea(cnt)
        perim = cv2.arcLength(cnt, closed=True)
        if area <= 0 or perim < 1.0:
            continue
        circularity = (4.0 * math.pi * area) / (perim * perim)
        (_cx, _cy), r_enc = cv2.minEnclosingCircle(cnt)
        r_dt = float(dist[labels == i].max())
        r_area = math.sqrt(area / math.pi)
        v = dscale.blob_vote(r_dt, r_enc, r_area, circularity)
        if v:
            votes.append(v)

    r_est, scale_conf, n_merged = dscale.consensus_radius(votes)
    sdiag.update({"blobs": n_blobs, "votes": len(votes), "merged_blobs": n_merged})
    return r_est, scale_conf, filled, sdiag


def _white_rescue_pass(img_noglare, existing, r_est, fill_mask, h, w,
                       mask_informative=True, max_added=None):
    """Find buttons the colour mask cannot see (white button on white paper;
    every button when the mask is saturated) by running Hough on the IMAGE
    gradient instead of the mask, seeded with the already-established radius.

    A mask-invisible button still has a crisp circular rim and an edge-dense
    printed slogan, so candidates are accepted only when their interior Canny
    edge density clearly exceeds the background's (sampled from the image
    border strip, which is mostly background even on dense lots).  Existing
    circles are never touched; additions per call are capped so a textured
    background can't flood the set.

    mask_informative=False (saturated mask) skips the "already mask-covered"
    rejection — there everything is mask-covered — and allows the set to
    double per call.  ``max_added`` overrides the addition cap (the guided
    path knows the exact deficit from the user's count).

    Returns a list of new (x, y, r) circles (possibly empty).
    """
    if not existing or r_est is None or r_est < 8:
        return []
    try:
        gray_img = cv2.cvtColor(img_noglare, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray_img, (9, 9), 2)
        c = cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT, dp=1.3,
            minDist=max(8, int(r_est * 1.7)),
            param1=120, param2=30,
            minRadius=int(r_est * 0.85),
            maxRadius=int(r_est * 1.15),
        )
        if c is None:
            return []
        cand = np.around(c[0]).astype(int).tolist()

        edges = cv2.Canny(gray_img, 50, 150)
        # Background edge level from the border strip — the whole-image rate
        # is dominated by the buttons themselves on dense lots.
        _bw = max(1, int(min(h, w) * 0.08))
        _border_edges = np.concatenate([
            edges[:_bw, :].ravel(), edges[h - _bw:, :].ravel(),
            edges[:, :_bw].ravel(), edges[:, w - _bw:].ravel(),
        ])
        bg_edge = float(np.count_nonzero(_border_edges)) / max(1, _border_edges.size)

        margin = int(min(h, w) * 0.05)
        added = []
        if max_added is None:
            max_added = (len(existing) if not mask_informative
                         else max(2, int(len(existing) * 0.6)))
        for (x, y, r) in cand:
            if len(added) >= max_added:
                break
            if not (margin < x < w - margin and margin < y < h - margin):
                continue
            if any(
                math.hypot(x - ex, y - ey) < min(r, er) * 0.7
                for ex, ey, er in existing + added
            ):
                continue
            dm = np.zeros(edges.shape, dtype=np.uint8)
            cv2.circle(dm, (x, y), int(r * 0.9), 255, -1)
            inside = cv2.countNonZero(cv2.bitwise_and(edges, edges, mask=dm))
            disc_area = max(1.0, math.pi * (r * 0.9) ** 2)
            edge_in = inside / disc_area
            # Printed slogans are edge-dense; bare paper/wood/cloth is not.
            if edge_in < max(0.04, bg_edge * 1.5):
                continue
            if mask_informative:
                # Reject discs the colour mask ALREADY covers well — those
                # were findable the normal way; new ones here are more likely
                # duplicates/noise.
                fm = np.zeros(fill_mask.shape, dtype=np.uint8)
                cv2.circle(fm, (x, y), r, 255, -1)
                mask_fill = cv2.countNonZero(
                    cv2.bitwise_and(fill_mask, fill_mask, mask=fm)
                ) / max(1.0, math.pi * r * r)
                if mask_fill > 0.5:
                    continue
            added.append((x, y, r))
        return added
    except Exception as _wr_err:
        print(f">>> DETECT: white-rescue failed ({_wr_err})", flush=True)
        return []


# --- REGION-OF-INTEREST RETRY --------------------------------------------------

def _button_region(image_bgr):
    """Bounding box (original-image coords) of the button cluster, when it
    occupies a clear sub-region of the frame.

    Wide shots (a display case on a wall) leave the buttons tiny in the
    ≤800px working frame and surround them with irrelevant background; the
    human fix is to zoom into the case first.  This finds the union bbox of
    all significant mask components and returns it — padded — when it covers
    5–60% of the frame (above that, the photo IS the lot and zooming is
    pointless; below it, the mask found nothing meaningful).

    Returns (x1, y1, x2, y2) in ORIGINAL image coordinates, or None.
    """
    try:
        prep = _prepare_detection_image(image_bgr)
        h, w = prep["h"], prep["w"]
        mask = prep["mask"]
        n, _labels, stats, _c = cv2.connectedComponentsWithStats(mask, connectivity=8)
        areas = sorted(
            (int(stats[i, cv2.CC_STAT_AREA]), i) for i in range(1, n)
        )
        if not areas:
            return None
        dom_area, dom = areas[-1]
        # The region must clearly dominate the mask (a fused display case vs
        # everything else) — on a normal lot the buttons are similar-sized
        # separate components and no single one dominates, so no zoom happens.
        # Stray same-coloured objects elsewhere in the photo (a figurine, a
        # sticky note) are exactly what this ignores by NOT taking a union.
        second = areas[-2][0] if len(areas) >= 2 else 0
        if dom_area < max(3 * second, int(h * w * 0.03)):
            return None
        x1 = int(stats[dom, cv2.CC_STAT_LEFT])
        y1 = int(stats[dom, cv2.CC_STAT_TOP])
        x2 = x1 + int(stats[dom, cv2.CC_STAT_WIDTH])
        y2 = y1 + int(stats[dom, cv2.CC_STAT_HEIGHT])
        frac = ((x2 - x1) * (y2 - y1)) / float(h * w)
        if not (0.05 <= frac <= 0.60):
            return None
        pad = int(0.04 * min(h, w))
        x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
        x2, y2 = min(w, x2 + pad), min(h, y2 + pad)
        # Map back from the ≤800px working frame to original coordinates.
        H, W = image_bgr.shape[:2]
        sx, sy = W / float(w), H / float(h)
        return (int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy))
    except Exception as _br_err:
        print(f">>> DETECT: button-region failed ({_br_err})", flush=True)
        return None


# --- UNGUIDED DETECTION (scale-first + multi-pass sweep fallback) -------------

def _detect_unguided_once(image_bgr):
    """Detect buttons with NO user input.

    Scale-first primary path: estimate the button radius from the hole-filled
    mask (estimate_button_scale) and run the same Hough geometry the guided
    path uses, seeded with that radius, plus distance-transform peak proposals
    for touching buttons.  This converts the unguided problem into the guided
    one by synthesizing the radius that the human's count implies.

    Sweep fallback (the previous behaviour, kept intact for telemetry
    comparability and for images where the mask doesn't segment blobs):
    three blind Hough passes (conservative/standard/aggressive param2 levels ×
    five radius scales) scored on internal quality, contour-proposal merge when
    confidence is low, and a CLAHE preprocessing variant when still low.

    Whichever candidate set scores higher (_score_solution) wins; layout
    structure analysis then infers the grid and demotes outliers.

    Returns
    -------
    (circles, est_rows, est_cols, diag)

    circles  : list of (x, y, r) in the ≤800px working coordinate space
               (same space detect_buttons works in).  Empty list on failure.
    est_rows / est_cols : inferred grid shape (1, 1 when unknown).
    diag     : dict for build_detection_diag(noinput_diag=...).  Keys:
               conservative, standard, aggressive, selected, confidence,
               layout_conf, outliers, pass_winner, contour_count, merged_count,
               source, variant, bgdiff, r_est, scale_conf, scale_path,
               est_rows, est_cols, gate.  None on unrecoverable error.
    """
    try:
        prep = _prepare_detection_image(image_bgr)
        img = prep["img"]
        h, w = prep["h"], prep["w"]
        mask = prep["mask"]
        gray = prep["gray"]
        fill_threshold = prep["fill_threshold"]

        base_r = max(8, int(min(h, w) * 0.08))
        margin = int(min(h, w) * 0.05)

        # Mask saturation: when the colour mask covers most of the image
        # (blue buttons on a blue cloth, cream quilt matching the white
        # range), the fill-ratio criterion is non-discriminative — every
        # circle looks "full", _score_solution inflates, and mask-blob scale
        # estimates are meaningless.  Real lots measure <= ~0.55 coverage.
        mask_coverage = cv2.countNonZero(mask) / float(h * w)
        mask_saturated = mask_coverage > 0.75

        # --- Sweep passes (previous behaviour; always run so the
        # conservative/standard/aggressive telemetry stays comparable) --------
        pass_configs = [
            ("conservative", 40),   # strict  — few false positives
            ("standard",     28),   # normal  — matches original unguided sweep
            ("aggressive",   18),   # relaxed — catches faint/partial circles
        ]
        results = {
            label: _run_hough_pass(gray, mask, h, w, base_r, fill_threshold, param2)
            for label, param2 in pass_configs
        }
        counts = {label: len(c) for label, c in results.items()}

        scores       = {label: _score_solution(c, mask, fill_threshold, h, w)
                        for label, c in results.items()}
        winner       = max(scores, key=lambda k: scores[k])
        winner_circ  = results[winner]
        winner_score = scores[winner]

        # --- Contour fallback (when sweep confidence is low) -----------------
        min_r = max(4, int(base_r * 0.40))
        max_r = int(base_r * 2.86)
        contour_count = 0
        merged_count = len(winner_circ)
        source = "hough_only"
        variant = "hsv"

        if winner_score < 0.65:
            proposals = _contour_circle_proposals(
                mask, min_r, max_r, margin, fill_threshold, h, w
            )
            contour_count = len(proposals)
            if proposals:
                merged = _merge_circle_sets(winner_circ, proposals, fill_threshold, mask)
                merged_score = _score_solution(merged, mask, fill_threshold, h, w)
                merged_count = len(merged)
                if merged_score > winner_score:
                    winner_circ  = merged
                    winner_score = merged_score
                    source       = "hough+contour"
                    print(
                        f">>> DETECT_UNGUIDED: contour merge accepted — "
                        f"contour_proposals={contour_count} "
                        f"merged_count={merged_count} "
                        f"new_score={merged_score:.3f}",
                        flush=True,
                    )
                else:
                    print(
                        f">>> DETECT_UNGUIDED: contour merge rejected — "
                        f"hough_score={winner_score:.3f} >= merged_score={merged_score:.3f}",
                        flush=True,
                    )

        # --- CLAHE/LAB preprocessing variant (when still low) ----------------
        if winner_score < 0.65:
            try:
                _clahe_mask, _clahe_gray = _build_clahe_mask(
                    prep["img_noglare"], prep["white_bg"]
                )
                _clahe_results = {
                    label: _run_hough_pass(
                        _clahe_gray, _clahe_mask, h, w, base_r, fill_threshold, param2
                    )
                    for label, param2 in pass_configs
                }
                _clahe_scores = {
                    label: _score_solution(c, _clahe_mask, fill_threshold, h, w)
                    for label, c in _clahe_results.items()
                }
                _clahe_best       = max(_clahe_scores, key=lambda k: _clahe_scores[k])
                _clahe_best_score = _clahe_scores[_clahe_best]
                if _clahe_best_score > winner_score:
                    winner_circ  = _clahe_results[_clahe_best]
                    winner_score = _clahe_best_score
                    winner       = _clahe_best
                    source       = "hough_only"   # CLAHE result, no contour merge
                    variant      = "clahe_lab"
                    print(
                        f">>> DETECT_UNGUIDED: CLAHE variant adopted — "
                        f"pass={_clahe_best} score={_clahe_best_score:.3f}",
                        flush=True,
                    )
                else:
                    print(
                        f">>> DETECT_UNGUIDED: CLAHE variant rejected — "
                        f"clahe_best={_clahe_best_score:.3f} <= current={winner_score:.3f}",
                        flush=True,
                    )
            except Exception as _p3_err:
                print(
                    f">>> DETECT_UNGUIDED: CLAHE variant failed ({_p3_err}), "
                    f"keeping sweep result",
                    flush=True,
                )

        # --- Scale-first primary path -----------------------------------------
        # Estimate the radius from the mask blobs; when the blobs agree, run the
        # guided Hough geometry seeded with that radius (scales=(1.0,), param2=24
        # — exactly what the human's count gives detect_buttons today) plus
        # distance-transform peak proposals on the FILLED mask for touching
        # buttons.  Adopted only if it outscores the sweep winner.
        r_est, scale_conf, filled, _sdiag = estimate_button_scale(mask, h, w)
        scale_path = "sweep_fallback"

        def _scale_first_attempt(r_try):
            """Guided Hough geometry seeded with r_try, plus DT-peak proposals
            on the hole-filled mask (skipped on saturated masks, where the
            distance transform reflects the cloth, not the buttons)."""
            circ = _run_hough_pass(
                gray, mask, h, w, int(round(r_try)), fill_threshold,
                param2=24, scales=(1.0,),
            )
            if _blob_buster_enabled() and not mask_saturated:
                _dt_props = _distance_peak_proposals(
                    filled, r_try,
                    int(r_try * 0.7), int(r_try * 1.3), margin, h, w,
                )
                circ = _merge_circle_sets(circ, _dt_props, fill_threshold, mask)
            return circ, _score_solution(circ, mask, fill_threshold, h, w)

        if (r_est is not None and r_est >= 8
                and scale_conf >= dscale.SCALE_CONF_MIN and not mask_saturated):
            sf_circ, sf_score = _scale_first_attempt(r_est)
            if sf_circ and sf_score > winner_score:
                winner_circ  = sf_circ
                winner_score = sf_score
                scale_path   = "scale_first"
                source       = "scale_first"
                print(
                    f">>> DETECT_UNGUIDED: scale-first adopted — "
                    f"r_est={r_est:.0f} scale_conf={scale_conf:.3f} "
                    f"count={len(sf_circ)} score={sf_score:.3f}",
                    flush=True,
                )
            else:
                print(
                    f">>> DETECT_UNGUIDED: scale-first rejected — "
                    f"r_est={r_est:.0f} scale_conf={scale_conf:.3f} "
                    f"score={sf_score:.3f} <= sweep={winner_score:.3f}",
                    flush=True,
                )
        else:
            print(
                f">>> DETECT_UNGUIDED: scale-first skipped — "
                f"r_est={r_est} scale_conf={scale_conf:.3f} "
                f"(blobs={_sdiag.get('blobs')})",
                flush=True,
            )

        # --- Second-chance scale: the winner set's own radii -------------------
        # When the mask blobs can't establish scale (fragmented by text/glare,
        # or merged across touching buttons) but the sweep found circles with
        # consistent radii, that median radius IS the scale — rerun the seeded
        # pass with it so the DT-peak proposals can recover buttons the sweep
        # missed inside merged blobs.
        if (scale_path != "scale_first" and len(winner_circ) >= 2
                and (r_est is None or r_est < 8
                     or scale_conf < dscale.SCALE_CONF_MIN)):
            _rads = [c[2] for c in winner_circ]
            _rmed = float(np.median(_rads))
            _rcv = float(np.std(_rads)) / _rmed if _rmed > 0 else 1.0
            _cv_bar = 0.25 if len(winner_circ) >= 4 else 0.15
            if _rmed >= 8 and _rcv <= _cv_bar:
                r_est = _rmed
                scale_conf = round(max(scale_conf, 1.0 - _rcv), 4)
                sc_circ, sc_score = _scale_first_attempt(_rmed)
                if sc_circ and sc_score > winner_score:
                    winner_circ  = sc_circ
                    winner_score = sc_score
                    scale_path   = "scale_second_chance"
                    source       = "scale_second_chance"
                print(
                    f">>> DETECT_UNGUIDED: second-chance scale r={_rmed:.0f} "
                    f"(cv={_rcv:.3f}) → count={len(winner_circ)} "
                    f"score={winner_score:.3f} path={scale_path}",
                    flush=True,
                )

        # --- White rescue: image-gradient Hough for mask-invisible buttons -----
        # White buttons on white paper (and anything else the colour mask is
        # blind to — e.g. every button on a saturated mask) still have crisp
        # circular rims and edge-dense printed slogans.  Seeded with the
        # established radius, accept only edge-supported, non-overlapping
        # additions.  Saturated masks get extra rounds because the mask
        # detector is known-blind there and the set may need to grow a lot.
        if winner_circ and r_est is not None and r_est >= 8:
            _wr_rounds = 4 if mask_saturated else 1
            _wr_total = 0
            for _wr_i in range(_wr_rounds):
                _added = _white_rescue_pass(
                    prep["img_noglare"], winner_circ, r_est, mask, h, w,
                    mask_informative=not mask_saturated,
                )
                if not _added:
                    break
                winner_circ = winner_circ + _added
                _wr_total += len(_added)
                if len(winner_circ) > 60:
                    break
            if _wr_total:
                print(f">>> DETECT_UNGUIDED: white-rescue added {_wr_total} "
                      f"edge-supported circles → {len(winner_circ)}", flush=True)
        else:
            _wr_total = 0

        # --- Saturated-mask confidence cap -------------------------------------
        # With a non-discriminative mask, the fill criterion (0.40 of the
        # composite) is free for ANY circle, so the score is inflated — never
        # let such an image auto-proceed, and send near-empty results to the
        # manual flow.
        if mask_saturated:
            _cap = 0.60 if len(winner_circ) >= 4 else 0.40
            if winner_score > _cap:
                print(f">>> DETECT_UNGUIDED: mask saturated "
                      f"(coverage={mask_coverage:.2f}) — confidence "
                      f"{winner_score:.3f} capped at {_cap}", flush=True)
            winner_score = min(winner_score, _cap)

        # --- Layout analysis on the winning circle set ------------------------
        est_rows, est_cols, layout_conf, outliers = _estimate_layout(
            winner_circ, h, w
        )

        # Outlier demotion: only when layout is clear (≥0.75) and we'd keep
        # at least 80 % of circles — avoids over-pruning irregular photos
        inlier_circles = winner_circ
        if layout_conf >= 0.75 and outliers > 0:
            r_med       = float(np.median([c[2] for c in winner_circ]))
            snap_radius = r_med * 1.2
            row_centres = _find_band_centres([c[1] for c in winner_circ], r_med)
            col_centres = _find_band_centres([c[0] for c in winner_circ], r_med)
            candidate   = [
                (cx, cy, cr) for (cx, cy, cr) in winner_circ
                if any(
                    math.hypot(cx - gc, cy - gr) <= snap_radius
                    for gr in row_centres for gc in col_centres
                )
            ]
            # Safety cap: never drop more than 20 % via outlier removal
            if len(candidate) >= len(winner_circ) * 0.80:
                inlier_circles = candidate

        selected_count = len(inlier_circles)

        gate = dgate.gate_decision(
            confidence=winner_score, layout_conf=layout_conf,
            selected=selected_count, est_rows=est_rows, est_cols=est_cols,
        )

        diag = {
            # Sweep passes
            "conservative": counts["conservative"],
            "standard":     counts["standard"],
            "aggressive":   counts["aggressive"],
            "selected":     selected_count,
            "confidence":   winner_score,
            "layout_conf":  layout_conf,
            "outliers":     outliers,
            "pass_winner":  winner,
            # Contour fallback
            "contour_count": contour_count,
            "merged_count":  merged_count,
            "source":        source,
            # CLAHE variant
            "variant":       variant,
            # Mask parity + scale-first
            "bgdiff":        bool(prep["bg_diff_used"]),
            "r_est":         (None if r_est is None else round(float(r_est), 1)),
            "scale_conf":    round(float(scale_conf), 4),
            "scale_path":    scale_path,
            "est_rows":      est_rows,
            "est_cols":      est_cols,
            "gate":          gate,
            # Saturation guard + white rescue (printed in DETECT_UNGUIDED logs;
            # ride along in the Sheet's noinput_diag JSON when present)
            "mask_coverage": round(mask_coverage, 3),
            "white_rescue":  _wr_total,
        }

        print(
            f">>> DETECT_UNGUIDED: passes={counts} scores={scores} "
            f"winner={winner}({winner_score:.3f}) variant={variant} source={source} "
            f"scale_path={scale_path} r_est={diag['r_est']} "
            f"scale_conf={diag['scale_conf']} "
            f"layout_conf={layout_conf:.3f} outliers={outliers} "
            f"grid={est_rows}x{est_cols} gate={gate} "
            f"selected={selected_count}",
            flush=True,
        )
        return inlier_circles, est_rows, est_cols, diag

    except Exception as e:
        print(f">>> DETECT: detect_unguided failed: {e}", flush=True)
        return [], 1, 1, None


def detect_unguided(image_bgr, roi_retry=True):
    """detect_unguided with one zoom-in retry: when the photo is a wide shot
    (the button cluster occupies a clear sub-region) and the first pass found
    little, re-run on the cluster's bounding box — buttons get ~2× the pixels
    in the working frame.  The retry is adopted when it finds more buttons.
    Returned circle coordinates are in the frame the adopted pass ran in
    (callers consume the count/grid, not the geometry)."""
    circles, est_rows, est_cols, diag = _detect_unguided_once(image_bgr)
    if not roi_retry or diag is None or len(circles) >= 6:
        return circles, est_rows, est_cols, diag
    roi = _button_region(image_bgr)
    if roi is None:
        return circles, est_rows, est_cols, diag
    x1, y1, x2, y2 = roi
    if x2 - x1 < 64 or y2 - y1 < 64:
        return circles, est_rows, est_cols, diag
    r_circles, r_rows, r_cols, r_diag = _detect_unguided_once(
        image_bgr[y1:y2, x1:x2]
    )
    if r_diag is not None and len(r_circles) > len(circles):
        print(f">>> DETECT_UNGUIDED: ROI retry adopted — region "
              f"({x1},{y1})-({x2},{y2}) found {len(r_circles)} "
              f"(full frame found {len(circles)})", flush=True)
        r_diag["roi_retry"] = True
        return r_circles, r_rows, r_cols, r_diag
    return circles, est_rows, est_cols, diag


def count_circles_unguided(image_bgr):
    """Count buttons with NO user input.  Thin wrapper over detect_unguided —
    kept so the shadow-logging call sites and Sheet schema are unchanged.

    Returns (selected_count, noinput_diag); (None, None) on error.
    """
    circles, _rows, _cols, diag = detect_unguided(image_bgr)
    if diag is None:
        return None, None
    return len(circles), diag


# --- GUIDED BUTTON DETECTION ---------------------------------------------------

def detect_buttons(image_bgr, rows=None, cols=None, expected=None, debug=False,
                   diag_out=None, truncate_to_expected=True, roi_retry=True):
    """Detect and crop buttons using the user-supplied (or auto-detected)
    count/grid, with one zoom-in retry: when the first pass falls well short
    of the expected count and the button cluster occupies a clear sub-region
    of the frame (display case in a wide shot), re-run on that region — the
    buttons get ~2× the pixels and the background sampler sees the actual
    backing material instead of the surroundings.  The retry is adopted only
    when it yields MORE crops; its debug image / circle_info are internally
    consistent (all relative to the region frame).

    ``truncate_to_expected=False`` keeps every surviving circle even past
    ``expected`` — used when the count came from auto-detection, so an
    under-count can't silently drop real buttons.
    """
    result = _detect_buttons_once(
        image_bgr, rows=rows, cols=cols, expected=expected, debug=debug,
        diag_out=diag_out, truncate_to_expected=truncate_to_expected,
    )
    _exp = expected if expected else ((rows or 0) * (cols or 0))
    if not roi_retry or not _exp or len(result[0]) >= _exp * 0.7:
        return result
    roi = _button_region(image_bgr)
    if roi is None:
        return result
    x1, y1, x2, y2 = roi
    if x2 - x1 < 64 or y2 - y1 < 64:
        return result
    _r_diag = {} if diag_out is not None else None
    retry = _detect_buttons_once(
        image_bgr[y1:y2, x1:x2], rows=rows, cols=cols, expected=expected,
        debug=debug, diag_out=_r_diag, truncate_to_expected=truncate_to_expected,
    )
    if len(retry[0]) > len(result[0]):
        print(f">>> DETECT: ROI retry adopted — region ({x1},{y1})-({x2},{y2}) "
              f"yielded {len(retry[0])} crops (full frame {len(result[0])})",
              flush=True)
        if diag_out is not None and _r_diag is not None:
            diag_out.update(_r_diag)
            diag_out["roi_retry"] = True
        return retry
    return result


def _detect_buttons_once(image_bgr, rows=None, cols=None, expected=None, debug=False,
                         diag_out=None, truncate_to_expected=True):
    """Single detection pass (no region retry) — see detect_buttons."""
    print(">>> DETECT: Starting...", flush=True)

    prep = _prepare_detection_image(image_bgr, diag_out=diag_out)
    image_bgr = prep["img"]
    h, w = prep["h"], prep["w"]
    mask = prep["mask"]
    gray = prep["gray"]
    _fill_threshold = prep["fill_threshold"]
    _bg_mean_s = prep["bg_mean_s"]
    _bg_mean_v = prep["bg_mean_v"]
    _mask_components = prep["mask_components"]

    debug_img = image_bgr.copy()
    if not expected:
        expected = rows * cols

    # --- Hough: radius bounds ---
    # When rows/cols are known use cell size; otherwise estimate from count + image area.
    if rows is not None and cols is not None:
        expected_r = int(min(h / rows, w / cols) * 0.35)
    elif expected:
        expected_r = int(math.sqrt(h * w / expected) * 0.35)
    else:
        expected_r = int(min(h, w) / 4 * 0.35)
    min_r      = int(expected_r * 0.7)
    max_r      = int(expected_r * 1.3)
    if diag_out is not None:
        diag_out["expected_radius"] = int(expected_r)
        diag_out["buttons_per_megapixel"] = round(expected / ((h * w) / 1_000_000), 1) if (h * w) else None
    print(f">>> DETECT: expected_r={expected_r}, min_r={min_r}, max_r={max_r}", flush=True)
    print(">>> DETECT: HoughCircles...", flush=True)
    circles = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=1.3,
        minDist=int(expected_r * 1.7),
        param1=120, param2=24,
        minRadius=min_r,
        maxRadius=max_r
    )
    if circles is not None:
        circles = np.around(circles[0]).astype(int)
    print(f">>> DETECT: HoughCircles done. Found: {len(circles) if circles is not None else 0}", flush=True)
    if diag_out is not None:
        diag_out["hough_pass1_count"] = int(len(circles)) if circles is not None else 0
    det_raw_hough     = len(circles) if circles is not None else 0
    det_count_noinput = None
    det_count_auto    = None
    det_radius_min = det_radius_max = det_radius_mean = det_radius_std = None

    # --- Draw ALL raw Hough circles onto debug_img unconditionally (red, thin) ---
    if circles is not None and debug:
        for (x, y, r) in circles:
            cv2.circle(debug_img, (x, y), r, (0, 0, 255), 1)

    crops = []

    if circles is not None:
        # Priority 5 — per-stage filter breakdown. border + fill + overlap always
        # sums to circles_rejected (raw_hough - survivors).
        _n_pre_margin       = len(circles)
        margin  = int(min(h, w) * 0.05)
        circles = [(x, y, r) for (x, y, r) in circles
                   if margin < x < w - margin and margin < y < h - margin]
        _det_border_removed = _n_pre_margin - len(circles)

        _det_fill_removed    = 0
        _det_overlap_removed = 0
        filtered = []
        for c in sorted(circles, key=lambda c: c[2], reverse=True):
            x, y, r = c

            # Reject circles that are not mostly blue/white
            circle_mask = np.zeros(mask.shape, dtype=np.uint8)
            cv2.circle(circle_mask, (x, y), r, 255, -1)
            blue_area   = cv2.countNonZero(cv2.bitwise_and(mask, mask, mask=circle_mask))
            fill_ratio  = blue_area / (np.pi * r * r)
            if fill_ratio < _fill_threshold:
                _det_fill_removed += 1
                continue

            # Deduplicate nearby circles
            if not any(np.hypot(x - fx, y - fy) < min(r, fr) * 0.7 for fx, fy, fr in filtered):
                filtered.append((x, y, r))
            else:
                _det_overlap_removed += 1

        det_count_noinput = len(filtered)

        # Background retry: always run a stricter Hough pass for telemetry.
        # Logged only — never used for crops or user results.
        _retry_circles = cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT, dp=1.3,
            minDist=int(expected_r * 2.2),
            param1=120, param2=35,
            minRadius=min_r,
            maxRadius=max_r,
        )
        if _retry_circles is not None:
            _retry_circles = np.around(_retry_circles[0]).astype(int)
            _retry_filtered = []
            for _rc in sorted(
                [(x, y, r) for (x, y, r) in _retry_circles
                 if margin < x < w - margin and margin < y < h - margin],
                key=lambda c: c[2], reverse=True,
            ):
                _rx, _ry, _rr = _rc
                _circle_mask = np.zeros(mask.shape, dtype=np.uint8)
                cv2.circle(_circle_mask, (_rx, _ry), _rr, 255, -1)
                _fill = cv2.countNonZero(
                    cv2.bitwise_and(mask, mask, mask=_circle_mask)
                ) / (np.pi * _rr * _rr)
                if _fill < _fill_threshold:
                    continue
                if not any(
                    np.hypot(_rx - fx, _ry - fy) < min(_rr, fr) * 0.7
                    for fx, fy, fr in _retry_filtered
                ):
                    _retry_filtered.append((_rx, _ry, _rr))
            det_hough_retry_count = len(_retry_filtered)
        else:
            det_hough_retry_count = 0
        print(
            f">>> DETECT: Background retry (strict): retry_count={det_hough_retry_count} "
            f"noinput={det_count_noinput} user={expected}",
            flush=True,
        )

        if diag_out is not None:
            diag_out["hough_retry_count"] = det_hough_retry_count

        if truncate_to_expected:
            filtered = filtered[:expected]

        cleaned = []
        for i, (x1, y1, r1) in enumerate(filtered):
            is_inner = any(
                i != j and np.hypot(x1 - x2, y1 - y2) < r2 * 0.3 and r1 < r2 * 0.6
                for j, (x2, y2, r2) in enumerate(filtered)
            )
            if not is_inner:
                cleaned.append((x1, y1, r1))

        if cleaned:
            radii    = [r for (_, _, r) in cleaned]
            median_r = np.median(radii)
            cleaned  = [(x, y, r) for (x, y, r) in cleaned
                        if 0.7 * median_r < r < 1.3 * median_r]
            _radii = [r for _, _, r in cleaned]
            if _radii:
                det_radius_min  = int(min(_radii))
                det_radius_max  = int(max(_radii))
                det_radius_mean = round(float(np.mean(_radii)), 1)
                det_radius_std  = round(float(np.std(_radii)), 1)

        # Promote localization-quality fields into diag_out so they land in the
        # Sheet (joinable to outcomes), not only the DETECT_TELEMETRY print.
        if diag_out is not None:
            _rej_q = det_raw_hough - (det_count_noinput or 0)
            diag_out["raw_hough"]        = int(det_raw_hough)
            diag_out["circles_rejected"] = int(_rej_q)
            diag_out["rejection_rate"]   = round(_rej_q / det_raw_hough, 3) if det_raw_hough else None
            # Priority 5 — per-stage breakdown (sums to circles_rejected).
            diag_out["border_removed"]   = int(_det_border_removed)
            diag_out["fill_removed"]     = int(_det_fill_removed)
            diag_out["overlap_removed"]  = int(_det_overlap_removed)
            diag_out["radius_min"]       = det_radius_min
            diag_out["radius_max"]       = det_radius_max
            diag_out["radius_mean"]      = det_radius_mean
            diag_out["radius_std"]       = det_radius_std

        print(f">>> DETECT: After dedup: {len(filtered)}, after inner-remove+radius: {len(cleaned)}", flush=True)

        # --- Radius-correction pass: the count-derived expected_r is a
        # heuristic that assumes buttons tile the image; on scattered lots it
        # overshoots and the Hough window can exclude the true radius
        # entirely.  When we're short of the expected count and the circles we
        # DID find agree on a different radius, re-run Hough at the measured
        # radius and merge the fill-passing newcomers.  Downstream stages
        # (blob-buster, white-rescue) then use the corrected radius too.
        if 3 <= len(cleaned) < expected:
            _rads = [r for (_, _, r) in cleaned]
            _rmed = float(np.median(_rads))
            _rcv = float(np.std(_rads)) / _rmed if _rmed > 0 else 1.0
            if _rcv <= 0.20 and abs(_rmed - expected_r) > expected_r * 0.15:
                _rc = _run_hough_pass(
                    gray, mask, h, w, int(round(_rmed)), _fill_threshold,
                    param2=24, scales=(1.0,),
                )
                _before = len(cleaned)
                cleaned = _merge_circle_sets(cleaned, _rc, _fill_threshold, mask)
                if truncate_to_expected:
                    cleaned = cleaned[:expected]
                if len(cleaned) > _before:
                    print(f">>> DETECT: radius-correction pass "
                          f"(expected_r={expected_r} → measured {_rmed:.0f}): "
                          f"+{len(cleaned) - _before} → {len(cleaned)}",
                          flush=True)
                expected_r = int(round(_rmed))
                min_r = int(expected_r * 0.7)
                max_r = int(expected_r * 1.3)

        # --- Blob-buster: split touching buttons that merged in the mask ------
        # When Hough comes up short AND the mask has fewer components than there
        # are buttons (the blob-merge signature), add a distance-transform circle
        # at each merged button's centre.  Kill-switch: BUTTONMATCHER_BLOB_BUSTER=0.
        _blob_used = False
        if (_blob_buster_enabled() and len(cleaned) < expected
                and (_mask_components or 0) < expected):
            _dt = _distance_peak_proposals(mask, expected_r, min_r, max_r, margin, h, w)
            _before = len(cleaned)
            cleaned = _merge_circle_sets(cleaned, _dt, _fill_threshold, mask)
            if truncate_to_expected:
                cleaned = cleaned[:expected]
            _blob_used = len(cleaned) > _before
            print(f">>> DETECT: blob-buster (components={_mask_components} < "
                  f"expected={expected}): hough={_before} +{len(cleaned) - _before} "
                  f"of {len(_dt)} distance-peaks → {len(cleaned)}", flush=True)

        # --- White rescue (guided): the user's count says buttons are missing
        # and the colour mask can't see them.  Classic case: a white button on
        # white paper — raw Hough finds its rim but the blue-fill filter
        # rejects it ("saw it, filtered it out").  Re-detect on the IMAGE
        # gradient, accept only edge-supported non-overlapping circles, and
        # cap additions at the known deficit so the count can't overshoot.
        if cleaned and len(cleaned) < expected:
            _deficit = expected - len(cleaned)
            _wr_r = float(np.median([c[2] for c in cleaned]))
            _coverage = cv2.countNonZero(mask) / float(h * w)
            _added = _white_rescue_pass(
                prep["img_noglare"], cleaned, _wr_r, mask, h, w,
                mask_informative=(_coverage <= 0.75), max_added=_deficit,
            )
            if _added:
                cleaned = cleaned + _added
                print(f">>> DETECT: white-rescue added {len(_added)} "
                      f"edge-supported circle(s) → {len(cleaned)} "
                      f"(deficit was {_deficit})", flush=True)
                if diag_out is not None:
                    diag_out["white_rescue"] = len(_added)
                if debug:
                    for (x, y, r) in _added:
                        cv2.circle(debug_img, (x, y), r, (0, 165, 255), 1)

        # Accept Hough when it has enough circles to form a grid (>=6, the original
        # floor), OR when it has essentially found the expected FEW-button count.  Logs
        # (Logger_5): Hough engaged on 0% of 1-3 button lots yet its pass-1 nailed the
        # single button 77% of the time -- the old floor of 6 forced every <=5-button
        # image onto the projection-grid fallback and discarded a correct detection.
        _enough = len(cleaned) >= max(6, expected - 4)
        _small_complete = expected <= 5 and len(cleaned) >= max(1, expected - 1)
        if _enough or _small_complete:
            # row_tol: grid-based when rows known, else radius-based
            row_tol = int(h / rows * 0.6) if rows is not None else int(expected_r * 1.5)
            rows_est = []
            for c in sorted(cleaned, key=lambda c: (c[1], c[0])):
                placed = False
                for row in rows_est:
                    if abs(row[0][1] - c[1]) < row_tol:
                        row.append(c)
                        placed = True
                        break
                if not placed:
                    rows_est.append([c])

            # Col filter: use known cols or accept any row with ≥1 button
            _min_per_row = int(cols * 0.4) if cols is not None else 1
            rows_est_filtered = [r for r in rows_est if len(r) >= _min_per_row]
            print(f">>> DETECT: Row groups before col filter: {len(rows_est)}, after: {len(rows_est_filtered)}", flush=True)
            rows_est = rows_est_filtered

            # Infer detected rows/cols from circle clustering
            det_rows = rows if rows is not None else len(rows_est)
            det_cols = cols if cols is not None else (max(len(r) for r in rows_est) if rows_est else 1)
            print(f">>> DETECT: Detected grid {det_rows}×{det_cols}", flush=True)

            # Sort rows top-to-bottom, each row left-to-right
            rows_est = sorted(rows_est, key=lambda row: np.mean([c[1] for c in row]))
            for row in rows_est:
                row.sort(key=lambda c: c[0])

            final_circles = [c for row in rows_est for c in row]
            if truncate_to_expected:
                final_circles = final_circles[:expected]
            det_count_auto = len(final_circles)

            circle_info = []
            for i, (x, y, r) in enumerate(final_circles):
                pad = int(r * 0.1)
                x1, x2 = max(0, x - r - pad), min(w, x + r + pad)
                y1, y2 = max(0, y - r - pad), min(h, y + r + pad)
                crop = image_bgr[y1:y2, x1:x2]
                if crop is not None and crop.size > 0:
                    crops.append(crop)
                    circle_info.append({"shape": "circle", "x": int(x), "y": int(y), "r": int(r)})

            print(f">>> DETECT: Returning {len(crops)} Hough crops.", flush=True)
            if diag_out is not None:
                diag_out["detector_used"] = dmask.detector_label("hough", _blob_used)
            _mpx = (h * w) / 1_000_000
            _bb  = "very_bright" if _bg_mean_v >= 192 else "bright" if _bg_mean_v >= 128 else "medium" if _bg_mean_v >= 64 else "dark"
            _sb  = "high_sat" if _bg_mean_s >= 128 else "medium_sat" if _bg_mean_s >= 64 else "low_sat"
            _rej = det_raw_hough - (det_count_noinput or 0)
            _telem = {
                "det_path":                   dmask.detector_label("hough", _blob_used),
                "det_image_width":            w,
                "det_image_height":           h,
                "det_count_user":             expected,
                "det_count_noinput":          det_count_noinput,
                "det_count_auto":             det_count_auto,
                "det_gap_auto":               det_count_auto - expected,
                "det_gap_noinput":            (det_count_noinput - expected) if det_count_noinput is not None else None,
                "det_raw_hough":              det_raw_hough,
                "det_circles_rejected":       _rej,
                "det_rejection_rate":         round(_rej / det_raw_hough, 3) if det_raw_hough else None,
                "det_radius_min":             det_radius_min,
                "det_radius_max":             det_radius_max,
                "det_radius_mean":            det_radius_mean,
                "det_radius_std":             det_radius_std,
                "det_buttons_per_megapixel":  round(expected / _mpx, 1) if _mpx > 0 else None,
                "det_bg_brightness_bucket":   _bb,
                "det_bg_saturation_bucket":   _sb,
            }
            print(f">>> DETECT_TELEMETRY: {json.dumps(_telem)}", flush=True)
            return crops, debug_img, det_rows, det_cols, circle_info

    # --- Projection-based grid fallback ---
    # Sum the blue mask along each axis to produce 1-D "where are the buttons"
    # profiles, then find the brightest point inside each row/col band.
    # This handles margins, uneven spacing, and white-background images far
    # better than a fixed equal-cell division.
    print(">>> DETECT: Using projection-based grid fallback.", flush=True)

    # Infer rows/cols if still unknown
    if rows is None or cols is None:
        det_rows, det_cols = _infer_grid_from_count(expected or 1, h, w)
        rows = det_rows
        cols = det_cols
        print(f">>> DETECT: Inferred grid from count: {det_rows}×{det_cols}", flush=True)
    else:
        det_rows, det_cols = rows, cols

    # Smooth kernel: ~25 % of one cell dimension, rounded to odd
    _cell_h = h / rows
    _cell_w = w / cols
    _kh = max(5, int(_cell_h * 0.25))
    _kw = max(5, int(_cell_w * 0.25))
    _kh = _kh if _kh % 2 == 1 else _kh + 1
    _kw = _kw if _kw % 2 == 1 else _kw + 1

    row_proj = mask.sum(axis=1).astype(np.float32)               # [h]
    col_proj = mask.sum(axis=0).astype(np.float32)               # [w]
    row_proj = cv2.GaussianBlur(row_proj.reshape(-1, 1),
                                (1, _kh), 0).ravel()
    col_proj = cv2.GaussianBlur(col_proj.reshape(1, -1),
                                (_kw, 1), 0).ravel()

    def _band_peaks(proj, n, length):
        """Return the index of the maximum inside each of n equal bands."""
        centers = []
        for i in range(n):
            s = int(i * length / n)
            e = int((i + 1) * length / n)
            seg = proj[s:e]
            centers.append(s + int(np.argmax(seg)) if seg.max() > 0
                           else (s + e) // 2)
        return centers

    row_centers = _band_peaks(row_proj, rows, h)
    col_centers = _band_peaks(col_proj, cols, w)
    print(f">>> DETECT: Projection row_centers={row_centers}", flush=True)
    print(f">>> DETECT: Projection col_centers={col_centers}", flush=True)

    # Pad each crop to half the median inter-center spacing (capped at cell size)
    pad_r = (int(np.median(np.diff(row_centers)) * 0.48)
             if len(row_centers) > 1 else int(_cell_h * 0.48))
    pad_c = (int(np.median(np.diff(col_centers)) * 0.48)
             if len(col_centers) > 1 else int(_cell_w * 0.48))

    # A projected grid cell only becomes a crop if the HSV mask actually shows a
    # button there.  Without this, the fallback emits numbered boxes over bare
    # background (the empty "2"/"3" over grass/quilt) — pure noise that the user
    # then has to skip.  We reuse the same `mask` Hough used.  The threshold is
    # deliberately LOW so a glare-washed real button (low blue fill) is still
    # kept; we only reject cells that are essentially empty.
    _EMPTY_CELL_FILL = 0.06
    idx = 1            # crop number shown to the user (only incremented on a kept cell)
    _skipped_empty = 0
    circle_info = []
    for cy in row_centers:
        for cx in col_centers:
            if idx > expected:
                break
            y1 = max(0, cy - pad_r)
            y2 = min(h, cy + pad_r)
            x1 = max(0, cx - pad_c)
            x2 = min(w, cx + pad_c)
            crop = image_bgr[y1:y2, x1:x2]
            if crop.size == 0:
                continue

            # Skip cells with almost no button pixels (background-only cells).
            _cell_mask = mask[y1:y2, x1:x2]
            _fill = (cv2.countNonZero(_cell_mask) / _cell_mask.size
                     if _cell_mask.size else 0.0)
            if _fill < _EMPTY_CELL_FILL:
                _skipped_empty += 1
                continue

            crops.append(crop)
            circle_info.append({"shape": "rect", "x1": int(x1), "y1": int(y1),
                                 "x2": int(x2), "y2": int(y2),
                                 "cx": int(cx), "cy": int(cy),
                                 "r": int(min(pad_r, pad_c))})
            idx += 1
    if _skipped_empty:
        print(f">>> DETECT: Grid fallback skipped {_skipped_empty} empty "
              f"(background-only) cells.", flush=True)

    print(f">>> DETECT: Returning {len(crops)} grid crops.", flush=True)
    if diag_out is not None:
        diag_out["detector_used"] = "grid"
    _mpx = (h * w) / 1_000_000
    _bb  = "very_bright" if _bg_mean_v >= 192 else "bright" if _bg_mean_v >= 128 else "medium" if _bg_mean_v >= 64 else "dark"
    _sb  = "high_sat" if _bg_mean_s >= 128 else "medium_sat" if _bg_mean_s >= 64 else "low_sat"
    _telem_proj = {
        "det_path":                   "projection",
        "det_image_width":            w,
        "det_image_height":           h,
        "det_count_user":             expected,
        "det_count_noinput":          det_count_noinput,
        "det_count_auto":             len(crops),
        "det_gap_auto":               len(crops) - expected,
        "det_gap_noinput":            (det_count_noinput - expected) if det_count_noinput is not None else None,
        "det_raw_hough":              det_raw_hough,
        "det_circles_rejected":       (det_raw_hough - det_count_noinput) if det_count_noinput is not None else None,
        "det_rejection_rate":         round((det_raw_hough - det_count_noinput) / det_raw_hough, 3) if det_raw_hough and det_count_noinput is not None else None,
        "det_radius_min":             det_radius_min,
        "det_radius_max":             det_radius_max,
        "det_radius_mean":            det_radius_mean,
        "det_radius_std":             det_radius_std,
        "det_buttons_per_megapixel":  round(expected / _mpx, 1) if _mpx > 0 else None,
        "det_bg_brightness_bucket":   _bb,
        "det_bg_saturation_bucket":   _sb,
    }
    print(f">>> DETECT_TELEMETRY: {json.dumps(_telem_proj)}", flush=True)
    return crops, debug_img, det_rows, det_cols, circle_info


def _circle_center_radius(c):
    """Center (x, y) and radius from a circle_info entry (circle or rect shape)."""
    if c.get("shape") == "rect":
        x1, y1, x2, y2 = c["x1"], c["y1"], c["x2"], c["y2"]
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0), min(x2 - x1, y2 - y1) / 2.0
    return (c["x"], c["y"]), c.get("r")


def _estimate_button_radius_px(centers, w, h):
    """Estimate a single button radius (px) for a lot from its button centres.

    Pinback buttons in a lot are one physical size, so one radius fits all.  When
    Hough has collapsed we have no measured radius, so we infer it from geometry
    we DO trust — the spacing of the Gemini centres (buttons in a lot sit ~a
    diameter apart, so radius ≈ half the median nearest-neighbour distance),
    anchored to a count/area heuristic (n buttons roughly tiling the frame) to
    resist a bad cluster.  Falls back to the area heuristic for <2 buttons."""
    n = len(centers)
    area_r = math.sqrt((h * w) / max(n, 1)) * 0.42   # assume buttons ~tile frame
    if n >= 2:
        nn = []
        for i, (xi, yi) in enumerate(centers):
            nn.append(min(math.hypot(xi - xj, yi - yj)
                          for j, (xj, yj) in enumerate(centers) if j != i))
        nn_r = 0.5 * statistics.median(nn)
        return max(area_r * 0.6, min(nn_r, area_r * 1.8))
    return area_r


# Gemini's per-button size_class is RELATIVE; it scales the lot's geometric base
# radius (it never sets absolute pixels).  Medium = base.
_SIZE_CLASS_MULT = {"small": 0.7, "medium": 1.0, "large": 1.4}


def gemini_led_crops(gemini_slogans, image_bgr, median_r=None):
    """Build one crop per Gemini-localized button directly from Gemini's x/y.

    This is the fallback LAYOUT to use when Hough collapses (the projection-grid
    path): rather than a blind, count-derived lattice that ignores where the
    buttons actually are — and which then masks Gemini's positions from
    ``reconcile_with_gemini`` because the lattice spuriously "covers" them — we
    trust Gemini's per-button centers and synthesize a crop at each one.

    Coordinates come from ``gemini_geometry`` (percent → px) in the same
    post-resize detection space as ``debug_img`` so crops line up with
    annotation.  RADIUS is built from geometry we trust, NOT from an absolute
    Gemini size.  Per button, in priority order:
      1. a rim point (``edge_x``/``edge_y``) → radius = its distance from the
         center (two trusted positions; clamped to 0.25–3× the base to ignore a
         stray point);
      2. else Gemini's RELATIVE ``size_class`` (small/medium/large) scaling the
         base;
      3. else the base itself.
    The lot-wide BASE radius is ``median_r`` when a caller measured one, else the
    button spacing via ``_estimate_button_radius_px``.  Mixed-size lots are thus
    handled per button.  Buttons without x/y are skipped.

    Returns ``(crops, circle_info)`` as parallel lists; circle_info entries are
    ``{"shape":"circle","x","y","r","source":"gemini_led","slogan","index"}``.
    """
    h, w = image_bgr.shape[:2]
    pts, slog = [], []
    for s in gemini_slogans:
        if s.get("x") is None or s.get("y") is None:
            continue
        pts.append(ggeo.pct_to_px(s["x"], s["y"], w, h))
        slog.append(s)
    if not pts:
        return [], []
    base_r = float(median_r) if (median_r and median_r > 0) else \
        _estimate_button_radius_px(pts, w, h)
    crops, circle_info = [], []
    for (gx, gy), s in zip(pts, slog):
        edge_r = ggeo.radius_from_edge(s, gx, gy, w, h)
        if edge_r is not None:
            r_px = min(3.0 * base_r, max(0.25 * base_r, edge_r))   # clamp strays
        elif s.get("size_class") in _SIZE_CLASS_MULT:
            r_px = base_r * _SIZE_CLASS_MULT[s["size_class"]]
        else:
            r_px = base_r
        x1, y1, x2, y2 = ggeo.synth_box(gx, gy, r_px, w, h)
        crop = image_bgr[int(y1):int(y2), int(x1):int(x2)]
        if crop is None or crop.size == 0:
            continue
        crops.append(crop)
        circle_info.append({
            "shape": "circle", "x": int(round(gx)), "y": int(round(gy)),
            "r": int(round(r_px)), "source": "gemini_led",
            "slogan": s.get("slogan"), "index": s.get("index"),
        })
    return crops, circle_info


def reconcile_with_gemini(circle_info, gemini_slogans, image_bgr,
                          median_r=None, cover_factor=1.0):
    """Back-fill buttons Hough missed using Gemini's per-button x/y, and link each
    final crop to its nearest Gemini slogan.

    Hough has already run independently and produced ``circle_info`` (parallel to
    the detected ``crops``).  This compares against Gemini's reading: any Gemini
    button not covered by a detected circle is a MISS, and a crop is synthesized
    at the miss location (sized by Gemini's ``size`` when present, else the median
    detected radius).  The same position map then associates every final crop
    (detected + recovered) with its nearest Gemini slogan for Phase-3 slogan
    confirmation.

    All geometry is delegated to the pure ``gemini_geometry`` module; only the
    actual pixel slice happens here.

    Returns ``(recovered_crops, recovered_circle_info, crop_to_slogan, telemetry)``:
      recovered_crops        : list[np.ndarray]  crops for the missed buttons
      recovered_circle_info  : list[dict]        parallel info, tagged
                               ``source="gemini_recovered"`` with the slogan/index
      crop_to_slogan         : dict[int, dict]   final-crop-index → associated
                               Gemini slogan {slogan, confidence, index, dist}
                               (indices span detected crops first, then recovered)
      telemetry              : dict              miss/coverage telemetry for logging
    """
    h, w = image_bgr.shape[:2]

    detected_centers = []
    detected_radii = []
    for c in circle_info:
        center, r = _circle_center_radius(c)
        detected_centers.append(center)
        detected_radii.append(r)

    plan = ggeo.plan_reconciliation(
        detected_centers, detected_radii, gemini_slogans, w, h,
        cover_factor=cover_factor, median_r=median_r,
    )

    recovered_crops = []
    recovered_circle_info = []
    for miss in plan["misses"]:
        x1, y1, x2, y2 = miss["box"]
        crop = image_bgr[y1:y2, x1:x2]
        if crop is None or crop.size == 0:
            continue
        recovered_crops.append(crop)
        recovered_circle_info.append({
            "shape": "circle",
            "x": int((x1 + x2) / 2),
            "y": int((y1 + y2) / 2),
            "r": int(miss["r_px"]),
            "source": "gemini_recovered",
            "slogan": miss.get("slogan"),
            "gemini_index": miss.get("index"),
            "confidence": miss.get("confidence"),
        })

    # Associate over the FINAL crop set (detected first, then recovered) so the
    # crop indices line up with the caller's crops + recovered_crops.
    final_centers = list(detected_centers)
    for rc in recovered_circle_info:
        final_centers.append((rc["x"], rc["y"]))
    crop_to_slogan, unmatched_gemini = ggeo.associate_slogans(
        final_centers, plan["gemini_px"], gemini_slogans,
    )

    telemetry = dict(plan["telemetry"])
    telemetry["unmatched_gemini_slogans"] = [
        gemini_slogans[i].get("slogan") for i in unmatched_gemini
    ]
    print(
        f">>> RECONCILE: hough={telemetry['hough_count']} "
        f"gemini={telemetry['gemini_count']} recovered={len(recovered_crops)} "
        f"misses={telemetry['misses']} "
        f"unmatched_gemini={telemetry['unmatched_gemini_slogans']}",
        flush=True,
    )
    return recovered_crops, recovered_circle_info, crop_to_slogan, telemetry
