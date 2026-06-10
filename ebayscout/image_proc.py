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


def _score_solution(circles: list, mask: np.ndarray, fill_threshold: float,
                    h: int, w: int) -> float:
    """Quality score for a Hough candidate set (0–1, higher = better).

    Four criteria (weighted sum):
      fill_mean    (0.40) — average blue-area fill; noise circles score near 0
      spacing_cv   (0.30) — 1-CV of nearest-neighbour distances; real grids are tight
      radius_cv    (0.20) — 1-CV of radii; genuine buttons are all the same size
      coverage     (0.10) — tent function peaking at 40 % image coverage

    Identical to _score_solution in main__3_.py so logged scores are comparable.
    """
    if not circles:
        return 0.0
    n  = len(circles)
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

    r_med    = float(np.median(rs))
    coverage = (n * math.pi * r_med ** 2) / (h * w)
    cov_score = (coverage / 0.40 if coverage <= 0.40
                 else max(0.0, 1.0 - (coverage - 0.40) / 0.60))

    return round(float(
        0.40 * fill_mean + 0.30 * spacing_score +
        0.20 * radius_score + 0.10 * cov_score
    ), 4)


def detect_and_crop(
    image_bytes: bytes,
    rows: int = 4,
    cols: int = 3,
    expected: int | None = None,
    button_count: int | None = None,
    max_crops: int | None = None,
    return_diag: bool = False,
):
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
    When return_diag=True, returns (crops, diag) where diag is a dict of detection
    telemetry compatible with match_logging.build_detection_diag (fields the scan
    can fill; ni_*/user_count stay None — ebayscout has no user-supplied count).
    """
    scan_mode = button_count is None
    diag: dict = {
        "h": None, "w": None,
        "bg_brightness": None, "bg_saturation": None, "bg_is_white": None,
        "mask_path": None, "hough_pass1_count": 0,
        "final_count_user": 0, "n_crops": 0, "detector_used": None,
        "raw_hough": None, "circles_rejected": None,
        "radius_min": None, "radius_max": None,
        "radius_mean": None, "radius_std": None,
        "mask_components": None,
        # Priority 5: per-stage filter breakdown. In scan mode these describe the
        # WINNING pass (the one that produced the crops); None in count mode.
        "border_removed": None, "fill_removed": None, "overlap_removed": None,
        # Priority 4: whole-image quality (computed on img_noglare).
        "edge_density": None, "brightness_std": None,
        # Phase 1: multi-pass fields (scan mode only; None in count mode)
        "ni_conservative": None, "ni_standard": None, "ni_loose": None,
        "ni_pass_winner": None, "ni_confidence": None,
    }

    def _ret(crops):
        diag["n_crops"] = len(crops)
        diag["final_count_user"] = len(crops)
        return (crops, diag) if return_diag else crops
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
        return _ret([])

    h, w = image_bgr.shape[:2]

    # Resize to at most IMAGE_MAX_DIM on the longest side (larger = better
    # small-button recall; Hough-only cost, CLIP still works on 224px crops).
    max_dim = config.IMAGE_MAX_DIM
    scale = min(max_dim / w, max_dim / h, 1.0)
    if scale < 1.0:
        new_w, new_h = int(w * scale), int(h * scale)
        image_bgr = cv2.resize(image_bgr, (new_w, new_h))
        h, w = new_h, new_w

    diag["h"], diag["w"] = int(h), int(w)

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

    diag["bg_brightness"] = _bg_mean_v
    diag["bg_saturation"] = _bg_mean_s
    diag["bg_is_white"]   = bool(_white_bg)
    diag["mask_path"]     = "blue_only" if _white_bg else "blue_or_white"

    # Priority 4 — whole-image quality on img_noglare (not the border sample).
    # edge_density: how busy is the background; brightness_std: whole-image contrast.
    try:
        _p4_gray = cv2.cvtColor(img_noglare, cv2.COLOR_BGR2GRAY)
        _p4_canny = cv2.Canny(_p4_gray, 50, 150)
        diag["edge_density"] = float(np.count_nonzero(_p4_canny)) / max(1, _p4_canny.size)
        _p4_hsv = cv2.cvtColor(img_noglare, cv2.COLOR_BGR2HSV)
        diag["brightness_std"] = float(np.std(_p4_hsv[:, :, 2]))
    except Exception as _p4_err:
        print(f">>> IMAGE: Priority-4 metrics failed ({_p4_err}).", flush=True)

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
    # The blue/white ranges miss maroon/green buttons and anything on a solid
    # coloured backdrop — the cause of the 0% exact rate on white backgrounds
    # (~60% of crops), where the blue-only mask doesn't contain the button. On a
    # UNIFORM background, also flag pixels whose Lab colour is far from the
    # sampled background and UNION them in (union → recall only goes up). Gated
    # to uniform backgrounds so textured wood/quilt doesn't flood the mask.
    # Kill-switch: EBAYSCOUT_BG_DIFF=0.
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
    diag["mask_path"] = dmask.mask_path_label(diag.get("mask_path", ""), _bg_diff_used)

    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    gray = cv2.GaussianBlur(mask, (9, 9), 2)

    # --- Mask connected-component count (cheap localization-quality signal) ---
    # components >> count → mask fragmenting buttons; << count → buttons merged.
    _mask_components = None
    try:
        _n_lbl, _, _stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        _min_area = max(1, int(h * w * 0.001))
        _mask_components = int(sum(
            1 for _i in range(1, _n_lbl) if _stats[_i, cv2.CC_STAT_AREA] >= _min_area
        ))
    except Exception as _cc_err:
        print(f">>> IMAGE: connectedComponents failed: {_cc_err}", flush=True)
    diag["mask_components"] = _mask_components

    # --- Hough circle detection ---
    # Three param2 values run in parallel for scan mode (conservative/standard/loose).
    # Each pass is scored on fill quality, spacing consistency, radius uniformity,
    # and image coverage; the highest-scoring pass drives the final crops.
    # Count mode keeps the original single-pass + half-radius fallback (legacy).
    _SCAN_PASSES = [
        ("conservative", 40),   # strict  — high accumulator threshold, few false positives
        ("standard",     24),   # normal  — original param2
        ("loose",        15),   # relaxed — catches faint/partial circles on white bg
    ]

    def _hough_p2(exp_r: int, p2: int):
        return cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT, dp=1.3,
            minDist=max(8, int(exp_r * 1.7)),
            param1=120, param2=p2,
            minRadius=int(exp_r * 0.7),
            maxRadius=int(exp_r * 1.3),
        )

    # Legacy single-param2 closure kept for count mode
    def _hough(exp_r: int):
        return _hough_p2(exp_r, 24)

    base_r = int(min(h / rows, w / cols) * 0.35)
    raw_by_scale = {}
    _margin = int(min(h, w) * 0.05)

    if scan_mode:
        # Run each pass across all radius scales, filter + dedup per pass,
        # then score and select the winner.
        def _run_pass(p2: int):
            """Returns (raw_count, border_removed, fill_removed, overlap_removed,
            filtered_circles) for one param2 value. The three removed-counts sum
            to raw_count - len(filtered) (the per-stage breakdown for this pass)."""
            all_raw: list = []
            radii_local = sweep_radii(
                base_r, config.HOUGH_RADIUS_SCALES, config.HOUGH_MIN_RADIUS_PX
            )
            for r in radii_local:
                c = _hough_p2(r, p2)
                if c is not None:
                    all_raw.extend(np.around(c[0]).astype(int).tolist())

            cand = [
                (x, y, r) for (x, y, r) in all_raw
                if _margin < x < w - _margin and _margin < y < h - _margin
            ]
            border_removed = len(all_raw) - len(cand)
            fill_removed = 0
            overlap_removed = 0
            filtered: list = []
            for (x, y, r) in sorted(cand, key=lambda c: c[2], reverse=True):
                cm = np.zeros(mask.shape, dtype=np.uint8)
                cv2.circle(cm, (x, y), r, 255, -1)
                blue = cv2.countNonZero(cv2.bitwise_and(mask, mask, mask=cm))
                if blue / max(1.0, math.pi * r * r) < _fill_threshold:
                    fill_removed += 1
                    continue
                if not any(
                    np.hypot(x - fx, y - fy) < min(r, fr) * 0.7
                    for fx, fy, fr in filtered
                ):
                    filtered.append((x, y, r))
                else:
                    overlap_removed += 1
            return len(all_raw), border_removed, fill_removed, overlap_removed, filtered

        # Each pass result: (raw, border_removed, fill_removed, overlap_removed, filtered)
        pass_results: dict[str, tuple] = {}
        for label, p2 in _SCAN_PASSES:
            pass_results[label] = _run_pass(p2)

        pass_scores = {
            label: _score_solution(res[4], mask, _fill_threshold, h, w)
            for label, res in pass_results.items()
        }
        winner_label = max(pass_scores, key=lambda k: pass_scores[k])
        raw_total    = sum(res[0] for res in pass_results.values())
        _winner      = pass_results[winner_label]
        circles_list = _winner[4]   # already filtered

        diag["raw_hough"]         = raw_total
        diag["hough_pass1_count"] = raw_total
        diag["ni_conservative"]   = len(pass_results["conservative"][4])
        diag["ni_standard"]       = len(pass_results["standard"][4])
        diag["ni_loose"]          = len(pass_results["loose"][4])
        diag["ni_pass_winner"]    = winner_label
        diag["ni_confidence"]     = pass_scores[winner_label]
        # Priority 5 — per-stage breakdown for the WINNING pass (the crops kept).
        diag["border_removed"]    = int(_winner[1])
        diag["fill_removed"]      = int(_winner[2])
        diag["overlap_removed"]   = int(_winner[3])

        print(
            f">>> IMAGE_PROC: scan passes — "
            f"conservative={diag['ni_conservative']} "
            f"standard={diag['ni_standard']} "
            f"loose={diag['ni_loose']} "
            f"scores={pass_scores} "
            f"winner={winner_label}({pass_scores[winner_label]:.3f}) "
            f"raw_total={raw_total}",
            flush=True,
        )

        # Feed winner into the cap + inner-circle removal + row-grouping below.
        # circles_list is already fill-ratio filtered — skip re-filtering.
        _scan_prefiltered = True

    else:
        # Count mode (manual /scout): single pass + half-radius fallback (legacy).
        radii = sweep_radii(base_r, config.HOUGH_RADIUS_SCALES, config.HOUGH_MIN_RADIUS_PX)
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

        diag["raw_hough"]         = 0 if circles is None else len(circles)
        diag["hough_pass1_count"] = diag["raw_hough"]
        circles_list              = None   # will be built in the shared block below
        _scan_prefiltered         = False

    crops_bgr: list[np.ndarray] = []

    # For scan mode the winner circles are already fill-ratio filtered.
    # For count mode build circles_list from raw Hough output.
    if not _scan_prefiltered:
        circles_list = None

    _have_circles = (
        (_scan_prefiltered and bool(circles_list))
        or (not _scan_prefiltered and circles is not None)
    )

    if _have_circles:
        if not _scan_prefiltered:
            margin  = int(min(h, w) * 0.05)
            circles_list = [
                (x, y, r) for (x, y, r) in circles
                if margin < x < w - margin and margin < y < h - margin
            ]

        # Fill-ratio filter + dedup — count mode only (scan already did this).
        if _scan_prefiltered:
            filtered = list(circles_list)
        else:
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
        # each merged button's centre. In scan mode `expected` is only the
        # rows*cols hint, so this is largely dormant; it fires mainly in count
        # mode. Kill-switch: EBAYSCOUT_BLOB_BUSTER=0.
        if (_blob_buster_enabled() and cleaned
                and len(cleaned) < expected
                and (_mask_components or 0) < expected):
            _min_r = int(base_r * 0.7)
            _max_r = int(base_r * 1.3)
            _dt = _distance_peak_proposals(mask, base_r, _min_r, _max_r, _margin, h, w)
            _before = len(cleaned)
            cleaned = _merge_circle_sets(cleaned, _dt, _fill_threshold, mask)[:cap]
            print(f">>> IMAGE: blob-buster (components={_mask_components} < "
                  f"expected={expected}): {_before} +{len(cleaned) - _before} "
                  f"of {len(_dt)} distance-peaks → {len(cleaned)}", flush=True)

        print(
            f">>> IMAGE: Hough circles — mode: {'scan' if scan_mode else 'count'}, "
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
                radii_final = [int(r) for (_, _, r) in final_circles]
                if radii_final:
                    diag["radius_min"]  = int(min(radii_final))
                    diag["radius_max"]  = int(max(radii_final))
                    diag["radius_mean"] = float(np.mean(radii_final))
                    diag["radius_std"]  = float(np.std(radii_final))
                if diag["raw_hough"] is not None:
                    diag["circles_rejected"] = max(0, diag["raw_hough"] - len(final_circles))
                diag["detector_used"] = "hough"
                return _ret(_bgr_to_pil(crops_bgr))

    # --- Whole-image fallback ---
    # Hough found no usable circles.  These photos are almost never a clean
    # grid of buttons, so fabricating a fixed grid produced meaningless crops.
    # Match the whole photo as a single button instead: correct for the common
    # single-button listing, and harmless otherwise (a multi-button blend just
    # scores below threshold and is rejected downstream).
    print(">>> IMAGE: No circles detected — using whole image as a single crop.", flush=True)
    diag["detector_used"] = "whole"
    return _ret(_bgr_to_pil([image_bgr]))


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


# --- Blob-buster helpers (ported verbatim from buttonmatcher/main.py) -------

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

    Separates touching buttons merged into one mask blob. Returns (x, y, r)
    tuples (may be empty), same format as Hough output.
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
