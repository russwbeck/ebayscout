"""Colour-cast measurement and correction for detection.

Pure cv2/numpy, no detector state — split out so it can be unit-tested the way
detect_gate / detect_mask / detect_scale are.

WHY THIS EXISTS
A photo taken under a blue-ish light records the white backing paper as
lavender.  Nothing is dark about such a photo — the measured paper brightness
is the same as a good one — but the paper's HSV saturation goes from ~25 to
~83, which breaks detection twice over:

  * ``white_bg = bg_mean_s < 65 and bg_mean_v > 170`` in _prepare_detection_image
    goes False, so masking takes the "blue OR white" path meant for coloured
    backgrounds instead of the "blue only" path meant for buttons on paper.
  * The paper itself now falls inside the blue hue range, so the blue mask
    matches nearly the whole frame.  On the sample that prompted this the mask
    covered 89% of the image, the blue fallback could not rescue it (blue=1.00),
    and Hough returned 2 blobs with r_est=371px against a true radius of ~51.

Correcting the cast puts the paper back near neutral and the normal pipeline
then works unchanged: the sample goes from 2 buttons at confidence 0.40
(gate=manual) to 13 at 0.95 (gate=auto).
"""

import cv2
import numpy as np

# Saturation of the bright (paper) region above which a photo is treated as
# colour-cast.  Measured: a neutral photo of buttons on paper sits near 25, the
# blue-cast photo of the SAME buttons near 83.
CAST_SATURATION = 50.0

# Fraction of the frame taken as "the bright region" — the backing paper.
BRIGHT_FRACTION = 0.15

# Value the paper is normalised to.  Neutralising the cast alone is not enough:
# the white_bg test needs bg_mean_v > 170, so the paper has to come back BRIGHT
# as well as neutral.  Measured on the sample, exposure-preserving correction
# reached only 12 buttons at coverage 0.73 while normalising to a bright target
# reached 13 at 0.34.  Anything in 200-255 works and scores within 0.01 of the
# rest, so this sits mid-range rather than on a tuned edge.
PAPER_TARGET = 220.0


def bright_region_saturation(image_bgr, bright_fraction=BRIGHT_FRACTION):
    """Mean HSV saturation of the brightest pixels — the backing paper.

    This is the cast measurement: paper is white in the world, so whatever
    saturation it carries in the file is the light's colour, not the subject's.
    """
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2]
    cutoff = np.percentile(v, 100.0 * (1.0 - bright_fraction))
    bright = v >= cutoff
    if not np.any(bright):
        return 0.0
    return float(np.mean(hsv[:, :, 1][bright]))


def has_color_cast(image_bgr, threshold=CAST_SATURATION):
    """True when the bright region is too saturated to be white paper."""
    return bright_region_saturation(image_bgr) > threshold


def neutralize_color_cast(image_bgr, bright_fraction=BRIGHT_FRACTION,
                          target=PAPER_TARGET):
    """Rebalance the channels so the bright region reads as bright neutral.

    Per-channel gains come from the bright region only — the paper is the white
    reference — and map it to ``target`` in every channel, which both removes
    the cast and restores the brightness the white_bg test looks for.  Gains are
    clamped so a frame whose bright region is not really paper cannot be wildly
    re-tinted.

    Geometry is untouched, so circles found in the corrected frame map 1:1 back
    onto the original image.

    Returns a new BGR image; the input is not modified.
    """
    img = image_bgr.astype(np.float32)
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2]
    cutoff = np.percentile(v, 100.0 * (1.0 - bright_fraction))
    bright = v >= cutoff
    if not np.any(bright):
        return image_bgr.copy()

    means = np.array([img[:, :, c][bright].mean() for c in range(3)],
                     dtype=np.float32)
    means = np.maximum(means, 1.0)
    gains = np.clip(float(target) / means, 0.3, 4.0)

    out = img * gains.reshape(1, 1, 3)
    return np.clip(out, 0, 255).astype(np.uint8)
