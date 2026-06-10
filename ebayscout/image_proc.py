"""
ebayscout/image_proc.py

Download eBay listing photos and detect individual button crops.

detect_and_crop() is ported from buttonmatcher/main.py detect_buttons()
with all interactive Slack prompts removed.  Returns PIL.Image objects
(RGB) rather than raw numpy arrays so they can be fed directly into the
CLIP preprocess pipeline.
"""

import math
import io
import os
import requests
import cv2
import numpy as np
from PIL import Image

from . import config
from . import detect_mask as dmask
from .utils import sweep_radii


# ---------------------------------------------------------------------------
# Kill-switches (instant rollback without a redeploy)
# ---------------------------------------------------------------------------

def _bg_diff_enabled():
    """Colour-vs-background mask is on by default; EBAYSCOUT_BG_DIFF=0 disables
    it (instant rollback to the blue/white colour mask without a redeploy)."""
    return os.environ.get("EBAYSCOUT_BG_DIFF", "1").strip() not in (
        "0", "false", "False", "",
    )


def _blob_buster_enabled():
    """Distance-transform splitting of touching buttons is on by default;
    EBAYSCOUT_BLOB_BUSTER=0 disables it (instant rollback without redeploy)."""
    return os.environ.get("EBAYSCOUT_BLOB_BUSTER", "1").strip() not in (
        "0", "false", "False", "",
    )


def download_image(url: str, timeout: int = 15) -> bytes:
    """
    Download an image from a URL and return raw bytes.
    Raises requests.HTTPError on non-2xx status.
    """
    resp = requests.get(url, timeout=timeout, stream=True)
    resp.raise_for_status()
    return resp.content


def detect_and_crop(
    image_bytes: bytes,
    rows: int = 4,
    cols: int = 3,
    expected: int | None = None,
    button_count: int | None = None,
    max_crops: int | None = None,
    diag_out: dict | None = None,
) -> list[Image.Image]:
    """
    Detect individual buttons in a lot photo and return PIL.Image crops (RGB).

    Two modes, keyed on `button_count`:
      - count mode (button_count given, e.g. manual /scout): single-pass Hough
        sized to that grid, capped at the count — unchanged legacy behaviour.
      - scan mode (button_count is None): multi-scale Hough sweep, NO 12-button
        cap (an old 4x3 grid default) — capped only by a high safety ceiling
        (config.MAX_CROPS_PER_PHOTO, raised by `max_crops` when a listing title
        states more). The point is recall: catch the one needed button in a big
        lot. The radius-consistency filter is skipped in scan mode so a
        size-outlier needed button isn't pruned.

    When no usable circles are found, falls back to the whole photo as one crop.

    Returns: list of PIL.Image.Image crops (RGB). Empty list if decode fails.
    """
    scan_mode = button_count is None
    if button_count is not None:
        expected = button_count
        # Derive rows/cols from count so the base radius scales correctly
        side = max(1, int(button_count ** 0.5))
        rows = side
        cols = max(1, (button_count + side - 1) // side)
    elif expected is None:
        expected = rows * cols   # radius/grouping hint only — NOT a crop cap

    # Effective crop ceiling. Count mode → the explicit count. Scan mode → a high
    # safety ceiling, raised when the title stated a bigger lot (max_crops).
    cap = button_count if button_count is not None else max(
        config.MAX_CROPS_PER_PHOTO, max_crops or 0)

    # Decode
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    image_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image_bgr is None:
        print("!!! IMAGE_PROC: Failed to decode image bytes.", flush=True)
        return []

    h, w = image_bgr.shape[:2]

    # Resize to at most IMAGE_MAX_DIM on the longest side (larger = better
    # small-button recall; Hough-only cost, CLIP still works on 224px crops).
    max_dim = config.IMAGE_MAX_DIM
    scale = min(max_dim / w, max_dim / h, 1.0)
    if scale < 1.0:
        new_w, new_h = int(w * scale), int(h * scale)
        image_bgr = cv2.resize(image_bgr, (new_w, new_h))
        h, w = new_h, new_w

    # --- Glare removal ---
    gray_orig  = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    thresh_val = float(np.percentile(gray_orig, 98))
    _, glare_mask = cv2.threshold(gray_orig, thresh_val, 255, cv2.THRESH_BINARY)
    glare_fraction = float(np.count_nonzero(glare_mask)) / glare_mask.size

    if glare_fraction > 0.10:
        img_noglare = image_bgr
    else:
        img_noglare = cv2.inpaint(image_bgr, glare_mask, 5, cv2.INPAINT_TELEA)

    # --- Background colour detection ---
    _bw = max(1, int(min(h, w) * 0.08))
    _border_px = np.concatenate([
        img_noglare[:_bw, :].reshape(-1, 3),
        img_noglare[h - _bw:, :].reshape(-1, 3),
        img_noglare[:, :_bw].reshape(-1, 3),
        img_noglare[:, w - _bw:].reshape(-1, 3),
    ])
    _hsv_border = cv2.cvtColor(_border_px.reshape(-1, 1, 3),
                               cv2.COLOR_BGR2HSV).reshape(-1, 3)
    _bg_mean_s = float(np.mean(_hsv_border[:, 1]))
    _bg_mean_v = float(np.mean(_hsv_border[:, 2]))
    _white_bg  = _bg_mean_s < 65 and _bg_mean_v > 170

    if diag_out is not None:
        diag_out["h"] = int(h)
        diag_out["w"] = int(w)
        diag_out["bg_brightness"] = _bg_mean_v
        diag_out["bg_saturation"] = _bg_mean_s
        diag_out["bg_is_white"] = bool(_white_bg)
        diag_out["mask_path"] = "blue_only" if _white_bg else "blue_or_white"

    # --- HSV colour mask ---
    hsv        = cv2.cvtColor(img_noglare, cv2.COLOR_BGR2HSV)
    lower_blue = np.array([90, 70, 40])
    upper_blue = np.array([140, 255, 255])
    if _white_bg:
        mask = cv2.inRange(hsv, lower_blue, upper_blue)
        _fill_threshold = 0.30
    else:
        lower_white = np.array([0, 0, 140])
        upper_white = np.array([180, 70, 255])
        mask = cv2.bitwise_or(
            cv2.inRange(hsv, lower_blue, upper_blue),
            cv2.inRange(hsv, lower_white, upper_white),
        )
        _fill_threshold = 0.55

    # --- Colour-vs-background mask (catches non-blue buttons) ----------------
    # The blue/white ranges miss maroon/green Citizens buttons and anything on a
    # solid coloured eBay backdrop — the cause of the 0% exact rate on white
    # backgrounds (60% of crops), where the blue-only mask doesn't contain the
    # button.  On a UNIFORM background, also flag pixels whose Lab colour is far
    # from the sampled background and UNION them in (union → recall only goes
    # up).  Gated to uniform backgrounds so textured wood/quilt doesn't flood
    # the mask.  Kill-switch: EBAYSCOUT_BG_DIFF=0.
    _bg_diff_used = False
    if _bg_diff_enabled():
        try:
            _lab = cv2.cvtColor(img_noglare, cv2.COLOR_BGR2LAB)
            _lab_border = np.concatenate([
                _lab[:_bw, :].reshape(-1, 3),
                _lab[h - _bw:, :].reshape(-1, 3),
                _lab[:, :_bw].reshape(-1, 3),
                _lab[:, w - _bw:].reshape(-1, 3),
            ]).astype(np.float32)
            _bg_lab    = np.median(_lab_border, axis=0)
            _bg_spread = float(np.mean(np.std(_lab_border, axis=0)))
            if dmask.should_use_bg_diff(_bg_spread):
                _thr  = dmask.bg_diff_threshold(_bg_spread)
                _dist = np.linalg.norm(_lab.astype(np.float32) - _bg_lab, axis=2)
                _bgm  = (_dist > _thr).astype(np.uint8) * 255
                mask  = cv2.bitwise_or(mask, _bgm)
                _bg_diff_used = True
            print(f">>> IMAGE: bg-diff spread={_bg_spread:.1f} "
                  f"(<= {dmask.BG_DIFF_MAX_SPREAD} ⇒ uniform) → "
                  f"{'APPLIED' if _bg_diff_used else 'skipped (textured bg)'}",
                  flush=True)
        except Exception as _bd_err:
            print(f">>> IMAGE: bg-diff mask failed ({_bd_err}); colour mask only.",
                  flush=True)
    if diag_out is not None:
        diag_out["mask_path"] = dmask.mask_path_label(
            diag_out.get("mask_path", ""), _bg_diff_used
        )

    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    gray = cv2.GaussianBlur(mask, (9, 9), 2)

    # --- Mask connected-component count (cheap localization-quality signal) ---
    # components >> count → mask is fragmenting buttons; << count → buttons
    # merged into one blob.  Joinable to outcomes in the Sheet.
    _mask_components = None
    try:
        _n_lbl, _, _stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        _min_area = max(1, int(h * w * 0.001))
        _mask_components = int(sum(
            1 for _i in range(1, _n_lbl) if _stats[_i, cv2.CC_STAT_AREA] >= _min_area
        ))
    except Exception as _cc_err:
        print(f">>> IMAGE: connectedComponents failed: {_cc_err}", flush=True)
    if diag_out is not None:
        diag_out["mask_components"] = _mask_components

    # --- Hough circle detection ---
    def _hough(exp_r: int):
        return cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT, dp=1.3,
            minDist=max(8, int(exp_r * 1.7)),
            param1=120, param2=24,
            minRadius=int(exp_r * 0.7),
            maxRadius=int(exp_r * 1.3),
        )

    base_r = int(min(h / rows, w / cols) * 0.35)
    raw_by_scale = {}

    if scan_mode:
        # Multi-scale sweep (large→small radii), MERGE all circles. Largest-first
        # dedup in the fill-ratio loop below removes cross-scale duplicates.
        radii = sweep_radii(base_r, config.HOUGH_RADIUS_SCALES, config.HOUGH_MIN_RADIUS_PX)
        all_circles = []
        for r in radii:
            c = _hough(r)
            n = 0 if c is None else len(c[0])
            raw_by_scale[r] = n
            if c is not None:
                all_circles.extend(np.around(c[0]).astype(int).tolist())
        circles = np.array(all_circles, dtype=int) if all_circles else None
    else:
        # Count mode (manual /scout): single pass + half-radius fallback (legacy).
        circles = _hough(base_r)
        if circles is None or len(circles[0]) < 4:
            small_r = max(10, base_r // 2)
            circles_small = _hough(small_r)
            if circles_small is not None and (
                circles is None or len(circles_small[0]) > len(circles[0])
            ):
                circles = circles_small
        if circles is not None:
            circles = np.around(circles[0]).astype(int)

    crops_bgr: list[np.ndarray] = []

    if circles is not None:
        margin  = int(min(h, w) * 0.05)
        circles_list = [
            (x, y, r) for (x, y, r) in circles
            if margin < x < w - margin and margin < y < h - margin
        ]

        # Filter by fill ratio
        filtered = []
        for c in sorted(circles_list, key=lambda c: c[2], reverse=True):
            x, y, r = c
            circle_mask = np.zeros(mask.shape, dtype=np.uint8)
            cv2.circle(circle_mask, (x, y), r, 255, -1)
            blue_area  = cv2.countNonZero(cv2.bitwise_and(mask, mask, mask=circle_mask))
            fill_ratio = blue_area / (math.pi * r * r)
            if fill_ratio < _fill_threshold:
                continue
            if not any(
                np.hypot(x - fx, y - fy) < min(r, fr) * 0.7
                for fx, fy, fr in filtered
            ):
                filtered.append((x, y, r))

        filtered = filtered[:cap]

        # Remove inner circles
        cleaned = [
            c for i, (x1, y1, r1) in enumerate(filtered)
            for c in [(x1, y1, r1)]
            if not any(
                i != j and np.hypot(x1 - x2, y1 - y2) < r2 * 0.3 and r1 < r2 * 0.6
                for j, (x2, y2, r2) in enumerate(filtered)
            )
        ]

        # Radius consistency filter — count mode only. In scan mode it would
        # prune the size-outlier button, which is often the one we need.
        if cleaned and not scan_mode:
            radii    = [r for (_, _, r) in cleaned]
            median_r = float(np.median(radii))
            cleaned  = [
                (x, y, r) for (x, y, r) in cleaned
                if 0.7 * median_r < r < 1.3 * median_r
            ]

        # --- Blob-buster: split touching buttons that merged in the mask ------
        # When detection comes up short AND the mask has fewer components than
        # buttons (the blob-merge signature), add a distance-transform circle at
        # each merged button's centre.  In scan mode `expected` is only the
        # rows*cols hint, so this is largely dormant; it fires mainly in count
        # mode.  Kill-switch: EBAYSCOUT_BLOB_BUSTER=0.
        if (_blob_buster_enabled() and cleaned
                and len(cleaned) < expected
                and (_mask_components or 0) < expected):
            _min_r = int(base_r * 0.7)
            _max_r = int(base_r * 1.3)
            _dt = _distance_peak_proposals(mask, base_r, _min_r, _max_r, margin, h, w)
            _before = len(cleaned)
            cleaned = _merge_circle_sets(cleaned, _dt, _fill_threshold, mask)[:cap]
            print(f">>> IMAGE: blob-buster (components={_mask_components} < "
                  f"expected={expected}): {_before} +{len(cleaned) - _before} "
                  f"of {len(_dt)} distance-peaks → {len(cleaned)}", flush=True)

        print(
            f">>> IMAGE: Hough circles — mode: {'scan' if scan_mode else 'count'}, "
            f"raw_by_scale: {raw_by_scale or 'n/a'}, merged: {len(circles_list)}, "
            f"filtered: {len(filtered)}, cleaned: {len(cleaned)}, cap: {cap}",
            flush=True,
        )

        if cleaned:
            row_tol  = int(base_r * 1.5)
            rows_est: list[list] = []
            for c in sorted(cleaned, key=lambda c: (c[1], c[0])):
                placed = False
                for row in rows_est:
                    if abs(row[0][1] - c[1]) < row_tol:
                        row.append(c)
                        placed = True
                        break
                if not placed:
                    rows_est.append([c])

            rows_est = [r for r in rows_est if len(r) >= 1]
            rows_est = sorted(rows_est, key=lambda row: float(np.mean([c[1] for c in row])))
            for row in rows_est:
                row.sort(key=lambda c: c[0])

            final_circles = [c for row in rows_est for c in row][:cap]
            print(f">>> IMAGE: Returning {len(final_circles)} Hough crops "
                  f"(mode={'scan' if scan_mode else 'count'}, cap={cap}).", flush=True)

            for (x, y, r) in final_circles:
                pad = int(r * 0.1)
                x1 = max(0, x - r - pad)
                x2 = min(w, x + r + pad)
                y1 = max(0, y - r - pad)
                y2 = min(h, y + r + pad)
                crop = image_bgr[y1:y2, x1:x2]
                if crop is not None and crop.size > 0:
                    crops_bgr.append(crop)

            if crops_bgr:
                return _bgr_to_pil(crops_bgr)

    # --- Whole-image fallback ---
    # Hough found no usable circles.  These photos are almost never a clean
    # grid of buttons, so fabricating a fixed grid produced meaningless crops.
    # Match the whole photo as a single button instead: correct for the common
    # single-button listing, and harmless otherwise (a multi-button blend just
    # scores below threshold and is rejected downstream).
    print(">>> IMAGE: No circles detected — using whole image as a single crop.", flush=True)
    return _bgr_to_pil([image_bgr])


def _bgr_to_pil(crops_bgr: list[np.ndarray]) -> list[Image.Image]:
    """Convert a list of OpenCV BGR arrays to PIL RGB images."""
    result = []
    for crop in crops_bgr:
        try:
            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            result.append(Image.fromarray(rgb))
        except Exception as exc:
            print(f"!!! IMAGE: Failed to convert crop: {exc}", flush=True)
    return result


# ===========================================================================
# Detection helpers (ported verbatim from buttonmatcher/main.py)
#
# _distance_peak_proposals + _merge_circle_sets back the blob-buster in
# detect_and_crop.  The remaining functions back count_circles_unguided(), the
# diagnostic multi-pass detector whose result drives the ni_* match_log columns
# (NEVER the crops the matcher sees — diagnostic only, mirroring buttonmatcher).
# ===========================================================================

def _score_solution(circles, mask, fill_threshold, h, w):
    """Score a candidate circle set on four internal-quality criteria.

    Returns a float in [0, 1].  Higher = more likely to be the correct count.
    Criteria: fill_mean (0.40), spacing consistency (0.30), radius consistency
    (0.20), coverage tent peaking at ~40% (0.10).
    """
    if not circles:
        return 0.0

    n = len(circles)
    xs = np.array([c[0] for c in circles], dtype=float)
    ys = np.array([c[1] for c in circles], dtype=float)
    rs = np.array([c[2] for c in circles], dtype=float)

    fills = []
    for (cx, cy, cr) in circles:
        cm = np.zeros(mask.shape, dtype=np.uint8)
        cv2.circle(cm, (int(cx), int(cy)), int(cr), 255, -1)
        blue = cv2.countNonZero(cv2.bitwise_and(mask, mask, mask=cm))
        fills.append(blue / max(1.0, math.pi * cr * cr))
    fill_mean = float(np.mean(fills))

    r_mean = float(np.mean(rs))
    r_cv   = float(np.std(rs)) / r_mean if r_mean > 0 else 1.0
    radius_score = max(0.0, 1.0 - r_cv)

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
        spacing_score = 0.5

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

    Returns (est_rows, est_cols, layout_conf, outliers).
    """
    if not circles:
        return 1, 1, 0.0, 0

    r_median = float(np.median([c[2] for c in circles]))

    def _find_band_centres(coords):
        coords = sorted(coords)
        if len(coords) <= 1:
            return [float(coords[0])] if coords else []
        gaps      = [coords[i + 1] - coords[i] for i in range(len(coords) - 1)]
        med_gap   = float(np.median(gaps))
        threshold = max(med_gap * 1.5, r_median * 2.0)
        bands, current = [], [coords[0]]
        for i, g in enumerate(gaps):
            if g > threshold:
                bands.append(current)
                current = [coords[i + 1]]
            else:
                current.append(coords[i + 1])
        bands.append(current)
        return [float(np.mean(b)) for b in bands]

    row_centres = _find_band_centres([c[1] for c in circles])
    col_centres = _find_band_centres([c[0] for c in circles])

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


def _run_hough_pass(gray, mask, h, w, base_r, fill_threshold, param2):
    """Run one Hough pass at five radius scales and return filtered circles.

    ``param2`` is the only variable between the three passes (40/28/18).
    """
    all_raw = []
    for s in (2.2, 1.6, 1.1, 0.8, 0.55):
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
    """Propose circle candidates from HSV mask contours (min enclosing circle +
    circularity >= 0.45 filter).  Same (x, y, r) format as Hough output."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    proposals = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < math.pi * min_r * min_r * 0.5:
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
    secondary circles not already covered by a primary circle (and passing the
    fill-ratio check)."""
    merged = list(primary)
    for (sx, sy, sr) in secondary:
        if any(
            math.hypot(sx - mx, sy - my) < min(sr, mr) * 0.7
            for mx, my, mr in merged
        ):
            continue
        cm = np.zeros(mask.shape, dtype=np.uint8)
        cv2.circle(cm, (sx, sy), sr, 255, -1)
        blue = cv2.countNonZero(cv2.bitwise_and(mask, mask, mask=cm))
        if blue / max(1.0, math.pi * sr * sr) < fill_threshold:
            continue
        merged.append((sx, sy, sr))
    return merged


def _distance_peak_proposals(mask, expected_r, min_r, max_r, margin, h, w):
    """Propose a circle at each distance-transform peak of the mask (blob-buster).

    Separates touching buttons merged into one mask blob.  Returns (x, y, r)
    tuples (may be empty).
    """
    try:
        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    except Exception as _dt_err:
        print(f">>> IMAGE: distanceTransform failed ({_dt_err}); no blob-buster.",
              flush=True)
        return []
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
    kept = dmask.select_peaks(candidates, min_separation=expected_r * 1.5)
    return [(x, y, r) for (x, y, r) in kept
            if margin < x < w - margin and margin < y < h - margin]


def _build_clahe_mask(img_noglare, white_bg):
    """Build an alternative HSV mask from a CLAHE-enhanced (LAB L-channel) image.

    Returns (mask, gray) ready to feed into _run_hough_pass.
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


def count_circles_unguided(image_bgr):
    """Count buttons with NO user input via multi-pass Hough + contour + CLAHE.

    Returns (selected_count, noinput_diag).  DIAGNOSTIC ONLY — the result drives
    the ni_* match_log columns, never the crops the matcher sees.  Mirrors
    buttonmatcher.count_circles_unguided.
    """
    try:
        img = image_bgr
        h, w = img.shape[:2]

        max_dim = 800
        scale = min(max_dim / w, max_dim / h, 1.0)
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)))
            h, w = img.shape[:2]

        gray_orig  = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        thresh_val = np.percentile(gray_orig, 98)
        _, glare_mask = cv2.threshold(gray_orig, thresh_val, 255, cv2.THRESH_BINARY)
        if np.count_nonzero(glare_mask) / glare_mask.size > 0.10:
            img_noglare = img
        else:
            img_noglare = cv2.inpaint(img, glare_mask, 5, cv2.INPAINT_TELEA)

        _bw = max(1, int(min(h, w) * 0.08))
        _border = np.concatenate([
            img_noglare[:_bw, :].reshape(-1, 3),
            img_noglare[h - _bw:, :].reshape(-1, 3),
            img_noglare[:, :_bw].reshape(-1, 3),
            img_noglare[:, w - _bw:].reshape(-1, 3),
        ])
        _hsv_b   = cv2.cvtColor(_border.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
        _white_bg = float(np.mean(_hsv_b[:, 1])) < 65 and float(np.mean(_hsv_b[:, 2])) > 170

        hsv        = cv2.cvtColor(img_noglare, cv2.COLOR_BGR2HSV)
        lower_blue = np.array([90,  70,  40])
        upper_blue = np.array([140, 255, 255])
        if _white_bg:
            mask           = cv2.inRange(hsv, lower_blue, upper_blue)
            fill_threshold = 0.30
        else:
            mask = cv2.bitwise_or(
                cv2.inRange(hsv, lower_blue, upper_blue),
                cv2.inRange(hsv, np.array([0, 0, 140]), np.array([180, 70, 255])),
            )
            fill_threshold = 0.55

        kernel = np.ones((5, 5), np.uint8)
        mask   = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask   = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  kernel)
        gray   = cv2.GaussianBlur(mask, (9, 9), 2)

        base_r = max(8, int(min(h, w) * 0.08))

        pass_configs = [
            ("conservative", 40),
            ("standard",     28),
            ("aggressive",   18),
        ]
        results = {
            label: _run_hough_pass(gray, mask, h, w, base_r, fill_threshold, param2)
            for label, param2 in pass_configs
        }
        counts = {label: len(c) for label, c in results.items()}

        if all(n == 0 for n in counts.values()):
            diag = {
                "conservative": 0, "standard": 0, "aggressive": 0,
                "selected": 0, "confidence": 0.0,
                "layout_conf": 0.0, "outliers": 0, "pass_winner": "standard",
            }
            return 0, diag

        scores       = {label: _score_solution(c, mask, fill_threshold, h, w)
                        for label, c in results.items()}
        winner       = max(scores, key=lambda k: scores[k])
        winner_circ  = results[winner]
        winner_score = scores[winner]

        margin = int(min(h, w) * 0.05)
        min_r  = max(4, int(base_r * 0.40))
        max_r  = int(base_r * 2.86)
        contour_count  = 0
        merged_count   = len(winner_circ)
        source         = "hough_only"
        variant        = "hsv"

        if winner_score < 0.65:
            proposals = _contour_circle_proposals(
                mask, min_r, max_r, margin, fill_threshold, h, w
            )
            contour_count = len(proposals)
            if proposals:
                merged = _merge_circle_sets(winner_circ, proposals, fill_threshold, mask)
                merged_score = _score_solution(merged, mask, fill_threshold, h, w)
                if merged_score > winner_score:
                    winner_circ  = merged
                    winner_score = merged_score
                    source       = "hough+contour"
                    merged_count = len(merged)
                else:
                    merged_count = len(merged)

        if winner_score < 0.65:
            try:
                _clahe_mask, _clahe_gray = _build_clahe_mask(img_noglare, _white_bg)
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
                    source       = "hough_only"
                    variant      = "clahe_lab"
            except Exception as _p3_err:
                print(f">>> IMAGE: count_circles_unguided Phase 3 CLAHE failed "
                      f"({_p3_err}), keeping Phase 1+2 result", flush=True)

        est_rows, est_cols, layout_conf, outliers = _estimate_layout(
            winner_circ, h, w
        )

        inlier_circles = winner_circ
        if layout_conf >= 0.75 and outliers > 0:
            r_med       = float(np.median([c[2] for c in winner_circ]))
            snap_radius = r_med * 1.2

            def _band_centres_local(coords, r_med_):
                coords = sorted(coords)
                if len(coords) <= 1:
                    return [float(coords[0])] if coords else []
                gaps      = [coords[i+1] - coords[i] for i in range(len(coords)-1)]
                med_gap   = float(np.median(gaps))
                threshold = max(med_gap * 1.5, r_med_ * 2.0)
                bands, current = [], [coords[0]]
                for i, g in enumerate(gaps):
                    if g > threshold:
                        bands.append(current)
                        current = [coords[i+1]]
                    else:
                        current.append(coords[i+1])
                bands.append(current)
                return [float(np.mean(b)) for b in bands]

            row_centres = _band_centres_local([c[1] for c in winner_circ], r_med)
            col_centres = _band_centres_local([c[0] for c in winner_circ], r_med)
            candidate   = [
                (cx, cy, cr) for (cx, cy, cr) in winner_circ
                if any(
                    math.hypot(cx - gc, cy - gr) <= snap_radius
                    for gr in row_centres for gc in col_centres
                )
            ]
            if len(candidate) >= len(winner_circ) * 0.80:
                inlier_circles = candidate

        selected_count = len(inlier_circles)

        diag = {
            "conservative": counts["conservative"],
            "standard":     counts["standard"],
            "aggressive":   counts["aggressive"],
            "selected":     selected_count,
            "confidence":   winner_score,
            "layout_conf":  layout_conf,
            "outliers":     outliers,
            "pass_winner":  winner,
            "contour_count": contour_count,
            "merged_count":  merged_count,
            "source":        source,
            "variant":       variant,
        }
        return selected_count, diag

    except Exception as e:
        print(f">>> IMAGE: count_circles_unguided failed: {e}", flush=True)
        return None, None
