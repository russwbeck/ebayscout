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
import requests
import cv2
import numpy as np
from PIL import Image


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
) -> list[Image.Image]:
    """
    Detect individual buttons in a lot photo and return PIL.Image crops (RGB).

    Uses OpenCV Hough circle detection; when no usable circles are found it
    falls back to matching the whole photo as a single crop (rather than
    slicing a fabricated grid). The `rows`/`cols`/`expected` hints size the
    expected button radius and cap the number of circle crops.

    Args:
        image_bytes: Raw image bytes (JPEG/PNG).
        rows:        Expected grid rows (default 4).
        cols:        Expected grid columns (default 3).
        expected:    Total expected button count.  Defaults to rows * cols.

    Returns:
        List of PIL.Image.Image crops (RGB).  Empty list if detection fails.
    """
    if expected is None:
        expected = rows * cols

    # Decode
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    image_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image_bgr is None:
        print("!!! IMAGE_PROC: Failed to decode image bytes.", flush=True)
        return []

    h, w = image_bgr.shape[:2]

    # Resize to at most 800px on the longest side
    max_dim = 800
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

    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    gray = cv2.GaussianBlur(mask, (9, 9), 2)

    # --- Hough circle detection ---
    expected_r = int(min(h / rows, w / cols) * 0.35)
    min_r      = int(expected_r * 0.7)
    max_r      = int(expected_r * 1.3)

    circles = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=1.3,
        minDist=int(expected_r * 1.7),
        param1=120, param2=24,
        minRadius=min_r,
        maxRadius=max_r,
    )
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

        filtered = filtered[:expected]

        # Remove inner circles
        cleaned = [
            c for i, (x1, y1, r1) in enumerate(filtered)
            for c in [(x1, y1, r1)]
            if not any(
                i != j and np.hypot(x1 - x2, y1 - y2) < r2 * 0.3 and r1 < r2 * 0.6
                for j, (x2, y2, r2) in enumerate(filtered)
            )
        ]

        # Radius consistency filter
        if cleaned:
            radii    = [r for (_, _, r) in cleaned]
            median_r = float(np.median(radii))
            cleaned  = [
                (x, y, r) for (x, y, r) in cleaned
                if 0.7 * median_r < r < 1.3 * median_r
            ]

        print(
            f">>> IMAGE: Hough circles — raw: {len(circles_list)}, "
            f"filtered: {len(filtered)}, cleaned: {len(cleaned)}",
            flush=True,
        )

        if cleaned:
            row_tol  = int(expected_r * 1.5)
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

            final_circles = [c for row in rows_est for c in row][:expected]
            print(f">>> IMAGE: Returning {len(final_circles)} Hough crops.", flush=True)

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
