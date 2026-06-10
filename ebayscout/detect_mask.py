"""
detect_mask — color-vs-background foreground decisions for button detection.

WHY THIS EXISTS
---------------
The original detector masked buttons by their *colour* (a blue HSV range, plus a
white range on non-paper backgrounds).  That works for blue Central/Mellon
buttons but fails the cases the production logs surfaced:

  * maroon / green Citizens buttons (not blue → not in the mask at all),
  * any button on a solid GREEN eBay backdrop,
  * coloured buttons on a solid white background.

The fix is to stop hunting a fixed colour and instead mask *whatever differs
from the background*: sample the border, take its colour, and flag pixels far
from it.  But a naive "different from background" mask FLOODS on a textured
background (wood grain, a quilt) — every grain line differs from the median.

So the decision is gated on how *uniform* the background is.  This module holds
the pure, unit-testable arithmetic of that decision (the cv2/numpy pixel ops
live in main.py, which can't be imported without the full vision stack).  The
inputs are plain scalars the caller computes from the border sample, so the
thresholds are testable with no numpy/cv2/torch.

UNITS
-----
All spreads/thresholds are in OpenCV 8-bit Lab units (L,a,b each 0–255 after
cv2.cvtColor(..., COLOR_BGR2LAB)), i.e. the same space the caller measures the
border in.  They are NOT CIE deltaE; treat them as "distance in cv2 Lab".
"""

from __future__ import annotations

# A background is "uniform enough" to trust the difference mask when the mean
# per-channel spread of its border pixels is at or below this (cv2 Lab units).
# Solid green/white/black backdrops sit well under this; wood grain and quilts
# sit well above it and keep the original colour mask only.
BG_DIFF_MAX_SPREAD = 18.0

# A pixel counts as foreground when its Lab distance from the background colour
# exceeds the threshold.  The threshold never drops below this floor (a clearly
# visible colour step) ...
BG_DIFF_BASE_THRESHOLD = 25.0
# ... and rises to clear the background's own variation by this multiple, so a
# slightly-noisy-but-uniform background still doesn't leak into the mask.
BG_DIFF_SPREAD_MULT = 3.0


def should_use_bg_diff(bg_lab_spread, *, max_spread=BG_DIFF_MAX_SPREAD):
    """True when the background is uniform enough to add a difference mask.

    ``bg_lab_spread`` is the mean per-channel std-dev of the border pixels in
    cv2 Lab.  Low → solid colour → safe to flag "everything unlike it".  High →
    textured → a difference mask would flood, so the caller keeps only the
    colour mask.
    """
    try:
        return float(bg_lab_spread) <= float(max_spread)
    except (TypeError, ValueError):
        return False


def bg_diff_threshold(bg_lab_spread,
                      *,
                      base=BG_DIFF_BASE_THRESHOLD,
                      spread_mult=BG_DIFF_SPREAD_MULT):
    """Lab distance a pixel must exceed (vs the background colour) to be flagged.

    max(base floor, spread * mult) — the floor guarantees a visible colour step
    even on a perfectly flat background; the spread term lifts the bar on a
    noisier (but still uniform) one so background variation isn't masked in.
    """
    try:
        s = float(bg_lab_spread)
    except (TypeError, ValueError):
        s = 0.0
    if s < 0:
        s = 0.0
    return max(float(base), s * float(spread_mult))


def mask_path_label(base_label, bg_diff_used):
    """Annotate the existing mask_path telemetry value with bg-diff activation.

    Keeps the logged vocabulary backward-compatible ("blue_only",
    "blue_or_white") while letting the 1000-button run measure whether the
    difference mask fired — no Sheet schema change required.
    """
    base = base_label or ""
    return f"{base}+bgdiff" if bg_diff_used else base


# --- Blob-buster: separate touching buttons ---------------------------------
#
# When buttons touch, the foreground mask merges into one blob and Hough
# under-detects, dumping detection onto the dumb projection-grid fallback (the
# cause of most grid-fallback failures in the logs).  The fix proposes a circle
# at each *local maximum of the mask's distance transform* — the centre of every
# button is a distance peak even inside a merged blob, because the "neck" where
# two buttons touch has a smaller distance-to-background than either centre.
#
# The distance transform itself is cv2 (in main.py); the peak de-duplication
# below is pure arithmetic, so it is unit-testable here.

def select_peaks(candidates, min_separation):
    """Greedy non-maximum suppression over distance-transform peak candidates.

    Parameters
    ----------
    candidates : iterable of (x, y, value)
        Candidate button centres; ``value`` is the peak strength (the distance-
        transform value ≈ the button radius).
    min_separation : float
        Minimum centre-to-centre distance between kept peaks.  Two peaks closer
        than this are the same button (or heavily overlapping) — keep only the
        stronger.

    Returns
    -------
    list of (x, y, value), strongest first, no two within ``min_separation``.
    """
    try:
        sep_sq = float(min_separation) ** 2
    except (TypeError, ValueError):
        sep_sq = 0.0
    kept = []
    for x, y, v in sorted(candidates, key=lambda c: c[2], reverse=True):
        if all((x - kx) ** 2 + (y - ky) ** 2 >= sep_sq for kx, ky, _ in kept):
            kept.append((x, y, v))
    return kept


def clamp_radius(value, min_r, max_r):
    """Clamp a distance-transform peak value into the expected button-radius band."""
    try:
        r = int(round(float(value)))
    except (TypeError, ValueError):
        r = int(min_r)
    return max(int(min_r), min(int(max_r), r))


def detector_label(base, blob_buster_used):
    """Annotate det_detector_used so the run can measure blob-buster activation
    without a Sheet schema change (e.g. "hough" → "hough+blob")."""
    base = base or ""
    return f"{base}+blob" if blob_buster_used else base

