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


def _hole_invert_enabled():
    """Dark-buttons-on-light-background mask inversion is on by default;
    EBAYSCOUT_HOLE_INVERT=0 (or BUTTONMATCHER_HOLE_INVERT=0) disables it."""
    return _flag_on("EBAYSCOUT_HOLE_INVERT", "BUTTONMATCHER_HOLE_INVERT")


def _fill_veto_enabled():
    """Phantom fill-in veto (center-inside-disc + radius-band, PROBLEM_IMAGE_FINDINGS
    validation: 5/5 phantoms vetoed -> 13/13 lot) is on by default;
    EBAYSCOUT_FILL_VETO=0 (or BUTTONMATCHER_FILL_VETO=0) disables it."""
    return _flag_on("EBAYSCOUT_FILL_VETO", "BUTTONMATCHER_FILL_VETO")


def _mask_radius_prior_enabled():
    """Mask-evidence radius prior for the guided detector (Fix A,
    PROBLEM_IMAGE_FINDINGS root cause #7: the count-derived expected_r is
    reliably wrong at small counts and can exclude the true radius from the
    Hough window entirely) is on by default; EBAYSCOUT_MASK_RADIUS_PRIOR=0 (or
    BUTTONMATCHER_MASK_RADIUS_PRIOR=0) disables it."""
    return _flag_on("EBAYSCOUT_MASK_RADIUS_PRIOR", "BUTTONMATCHER_MASK_RADIUS_PRIOR")


def _deficit_fill_enabled():
    """Deficit-fill instead of the projection-grid cliff (Fix C: when the guided
    Hough pass lands close to but short of the expected count, keep those
    circles and fill only the shortfall instead of discarding everything for
    the blind projection grid) is on by default; EBAYSCOUT_DEFICIT_FILL=0 (or
    BUTTONMATCHER_DEFICIT_FILL=0) disables it."""
    return _flag_on("EBAYSCOUT_DEFICIT_FILL", "BUTTONMATCHER_DEFICIT_FILL")


def _reconcile_swap_enabled():
    """Two-signal reconcile swap — recover a Gemini-located button a Hough
    false-positive suppressed, by dropping the off-mask phantom in its place
    (see reconcile_with_gemini / plan_reconciliation) — is on by default;
    EBAYSCOUT_RECONCILE_SWAP=0 (or BUTTONMATCHER_RECONCILE_SWAP=0) disables it."""
    return _flag_on("EBAYSCOUT_RECONCILE_SWAP", "BUTTONMATCHER_RECONCILE_SWAP")


def _anchor_recovery_enabled():
    """Anchor-gated recovery (1979-front incident): when a crop→slogan
    association is UNANCHORED and the slogan's own Gemini point is clear of
    every crop, synthesize a crop at the point — the recovery the fill-gated
    swap can't do on a flooded mask (phantoms score high fill there, so the
    count stays wrong and the deficit is 0).  No crop is dropped.  On by
    default; EBAYSCOUT_ANCHOR_RECOVERY=0 (or BUTTONMATCHER_ANCHOR_RECOVERY=0)
    disables it (instant rollback)."""
    return _flag_on("EBAYSCOUT_ANCHOR_RECOVERY", "BUTTONMATCHER_ANCHOR_RECOVERY")


def _frame_fit_enabled():
    """Gemini frame fit (1987-front dual incident): correct a stretched/offset
    Gemini coordinate frame against detection's row/column structure before any
    position is trusted (see gemini_geometry.fit_frame_map — identity unless
    the fit decisively improves anchored agreement).  On by default;
    EBAYSCOUT_FRAME_FIT=0 (or BUTTONMATCHER_FRAME_FIT=0) disables it."""
    return _flag_on("EBAYSCOUT_FRAME_FIT", "BUTTONMATCHER_FRAME_FIT")


def _auto_detect_enabled():
    """Auto detection (no count prompt) — default OFF until the gate is
    calibrated; BUTTONMATCHER_AUTO_DETECT=1 enables."""
    return os.environ.get("BUTTONMATCHER_AUTO_DETECT", "0").strip() in (
        "1", "true", "True",
    )


# Fix A — minimum estimate_button_scale confidence for the mask-evidence radius
# prior to override the count-derived expected_r in the guided detector.  Tuned
# against the six PROBLEM_IMAGE_FINDINGS fixtures: the clean cases read 0.90-0.98
# (turf lots 0.98), while the shadow/glare wood lot's best variant reads 0.58 —
# still well above chance and, once adopted, takes the deficit-fill count from
# 27 to 29 circles (both inside the 26-29 replay-evidence range) — so the floor
# sits at 0.55, just under it, rather than the initially-considered 0.6.
MASK_RADIUS_PRIOR_CONF_THRESHOLD = 0.55

# Fix C — minimum fraction of `expected` that a starved guided Hough pass must
# already hold to earn deficit-fill (keep the circles, fill only the shortfall)
# instead of falling all the way to the projection-grid cliff.
DEFICIT_FILL_MIN_FRACTION = 0.60

# Fix C quality gate — maximum share of the frame the adopted mask may cover for
# deficit-fill to be TRUSTED enough to skip the projection fallback.  Deficit-fill
# keeps the guided Hough circles and commits the lot to the hough path; that is
# only safe when the mask actually isolated the buttons.  When the colour mask
# leaks the whole background in (blue buttons on green turf: the mask floods ~68%
# of the frame and Hough locks onto grass texture — 18/21 "kept" circles land on
# grass), the kept circles are garbage and must NOT preempt projection + Gemini
# reconcile, which is what rescues these lots.  Calibrated on the deficit-fill
# fixtures: the one legit case (case1_wood_glare_37) fills 30% of the frame; the
# turf failure fills 68% — so the floor sits at 0.50, clear of both.
DEFICIT_FILL_MAX_MASK_FRACTION = 0.50


def _deficit_fill_decision(enabled, n_cleaned, expected, already_enough,
                           small_complete, mask_fraction):
    """Branch a starved guided pass takes at the deficit-fill / projection fork.

    Returns:
      "commit"  — keep the Hough circles and fill only the shortfall, skipping
                  the projection fallback.
      "decline" — eligible by count, but the adopted mask floods the frame
                  (foreground fraction > DEFICIT_FILL_MAX_MASK_FRACTION), so it
                  never isolated the buttons and the kept circles are background
                  hits (blue-on-turf: ~68% mask, Hough on grass) — fall through
                  to projection + Gemini reconcile instead of trusting them.
      "n/a"     — not a deficit-fill situation (already enough, too few held,
                  a small-complete lot, or the feature is off).

    Pure (no cv2 / image) so the gate is unit-testable directly."""
    if already_enough or small_complete:
        return "n/a"
    if not (enabled and n_cleaned and expected):
        return "n/a"
    if n_cleaned < expected * DEFICIT_FILL_MIN_FRACTION:
        return "n/a"
    return "decline" if mask_fraction > DEFICIT_FILL_MAX_MASK_FRACTION else "commit"


def _guided_mask_floods(expected, mask_fraction, enabled):
    """True when a non-small lot's adopted mask floods the frame past
    DEFICIT_FILL_MAX_MASK_FRACTION.  The guided Hough circles are then background
    hits (turf/carpet) even if they clear the acceptance floor, so the lot should
    be refused and routed to projection + Gemini reconcile.  Scoped to expected > 5
    so small-count lots keep their small-complete behaviour.  Pure, for unit
    testing; shares the deficit-fill threshold and kill switch."""
    return bool(enabled and expected and expected > 5
                and mask_fraction > DEFICIT_FILL_MAX_MASK_FRACTION)


# A flooded mask alone cannot tell "background leaked in" (turf/carpet — the
# gate's real target) from "big buttons legitimately fill the frame" (the
# dense-Mellon 11-lot, 2026-07-15).  The first check ("circles explain >= 60%
# of the mask") overfit its single positive example (§4.2 — again): a LEAKY
# mask on a real dense lot (CCB-12 on dark wood, 2026-07-16: mask 61%, 12/12
# perfect circles, explained only 53%) was refused, because the ratio
# conflates "mask has extra background" with "circles are on background".
# The separating quantity is the RESIDUAL — mask area OUTSIDE the accepted
# circles, as a fraction of the FRAME: a background sheet is huge whether or
# not the mask also contains buttons.  Measured: dense-Mellon 0.10, CCB-12
# 0.28 (both must accept) vs navy-carpet ~0.56 / turf ~0.58 (both must
# refuse) — 0.42 splits with ~0.14 margins on both sides.
FLOOD_RESIDUAL_MAX = 0.42


def _flood_refusal_decision(expected, mask_fraction, explained, enabled):
    """Decision at the flooded-mask refusal fork of the guided acceptance.

    Returns:
      "refuse"       — mask floods AND a large mask sheet remains OUTSIDE the
                       accepted circles (residual = mask_fraction * (1 -
                       explained) > FLOOD_RESIDUAL_MAX): turf/carpet
                       background hits → route to projection + Gemini
                       reconcile, as shipped 2026-07-12.
      "accept_dense" — mask floods but nearly all of it is under the circles
                       (dense lot of large buttons, possibly with a leaky
                       mask) — keep the guided circles.
      "n/a"          — mask does not flood (or gate disabled / small lot);
                       the fork is not taken.

    ``explained`` (fraction of mask foreground covered by the circle union)
    None means the check could not run — treated as 0.0 so the behaviour
    degrades to the shipped refusal, never to a silent accept.  Pure (no
    cv2) so the margins are unit-testable."""
    if not _guided_mask_floods(expected, mask_fraction, enabled):
        return "n/a"
    residual = mask_fraction * (1.0 - (explained or 0.0))
    if residual <= FLOOD_RESIDUAL_MAX:
        return "accept_dense"
    return "refuse"


def _circles_explain_mask(mask, circles):
    """Fraction of the mask's foreground covered by the union of the accepted
    circle disks — the flood gate's independent on-target check.  0.0 when
    there is nothing to measure (empty mask / no circles)."""
    if mask is None or not len(circles):
        return 0.0
    fg = cv2.countNonZero(mask)
    if not fg:
        return 0.0
    union = np.zeros(mask.shape[:2], dtype=np.uint8)
    for c in circles:
        try:
            x, y, r = int(c[0]), int(c[1]), int(c[2])
        except (TypeError, ValueError, IndexError):
            continue
        cv2.circle(union, (x, y), max(r, 1), 255, -1)
    covered = cv2.countNonZero(cv2.bitwise_and(mask, union))
    return covered / float(fg)


# A fused sheet BELOW the 0.75 saturation trigger starves guided Hough the
# same way the >0.75 lots did (white envelope under 13 navy buttons: coverage
# 0.72, Hough found 0) — but healthy DENSE lots live at 0.58-0.61 coverage, so
# lowering the chooser trigger would risk switching masks under working lots.
# Instead, retry with the same fallback variants ONLY on demonstrated failure:
# guided pass-1 found under FUSED_RETRY_MAX_FRACTION of `expected` while the
# mask sits in the fused band (tested_hypothesis §4.2 — gate a new path on an
# independent check of the current path's own output).
FUSED_RETRY_MIN_COVERAGE = 0.50
FUSED_RETRY_MAX_FRACTION = 0.30   # pass-1 short of 30% of expected = starved


def _fused_mask_fallback(img_bgr, bg_mean_v, h, w):
    """Blue-only / bright hole-filled fallback variants for a fused mask —
    the same two hypotheses the >0.75 saturation chooser uses, packaged for
    the starved-Hough retry.  Returns (mask, label, coverage) for the first
    plausible variant (coverage 8-75%), or None."""
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    kernel = np.ones((5, 5), np.uint8)

    def _finish(fb):
        fb = cv2.morphologyEx(fb, cv2.MORPH_CLOSE, kernel)
        fb = cv2.morphologyEx(fb, cv2.MORPH_OPEN, kernel)
        seed = next(((sx, sy) for (sx, sy) in
                     ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))
                     if fb[sy, sx] == 0), None)
        if seed is not None:
            ff = fb.copy()
            cv2.floodFill(ff, np.zeros((h + 2, w + 2), np.uint8), seed, 255)
            fb = cv2.bitwise_or(fb, cv2.bitwise_not(ff))
        return fb, (cv2.countNonZero(fb) / float(h * w)) if (h * w) else 0.0

    _fb1, _cov1 = _finish(cv2.inRange(
        hsv, np.array([90, 70, 40]), np.array([140, 255, 255])))
    _fb2, _cov2 = _finish(((hsv[:, :, 2] > (bg_mean_v or 0) + 60)
                           & (hsv[:, :, 1] < 80)).astype(np.uint8) * 255)
    for label, fb, cov in (("blue", _fb1, _cov1), ("bright", _fb2, _cov2)):
        if 0.08 <= cov <= 0.75:
            return fb, label, cov
    return None


# --- SHARED IMAGE PREP --------------------------------------------------------

# Minimum frame coverage the kept holes must reach before the dark-on-light
# inversion may replace the mask.  Real buttons-as-holes are button-sized: the
# dark_on_white_13 fixture measures 0.157.  Slogan-TEXT blocks inside
# foreground buttons — multi-line text morph-closes into roughly-square blobs
# that pass the circularity filter — measure far smaller: the blue-on-white
# 6-lot that lost a confident 6-button mask (r_est=107 conf=0.98) to fifteen
# r~21 text holes read 0.043 (2026-07-15).  0.08 is the same 8% plausibility
# floor the mask variants already use, and it clears both cases with ~2x
# margin.  A sub-floor holes mask means "leave the mask alone", never invert.
HOLE_INVERT_MIN_COVERAGE = 0.08


def _buttons_as_holes(mask, h, w):
    """Detect the "dark buttons on a light background" inversion and return the
    button disks.

    Near-black buttons on a white matte match neither the blue nor the white HSV
    range, so the colour mask captures the bright matte as foreground and each
    button becomes an enclosed HOLE.  Every mask-driven stage then rejects
    on-button circles (their interior isn't foreground) and lands crops in the
    white pockets BETWEEN buttons — the "negative space" failure.

    Flood-fill the background inward from the four corners; whatever stays 0 is
    enclosed.  Keep only button-plausible holes — circular (area vs. its bounding
    circle >= 0.6), roughly square bbox, and >= 0.2% of the frame — so slogan-text
    holes and thin gaps are dropped.  Returns (holes_mask, n_kept, coverage).  The
    caller inverts only when several such holes appear, which is the signature of
    buttons-as-holes; it does not occur when the buttons ARE the foreground (the
    background then reaches the border and nothing is enclosed)."""
    ff = mask.copy()
    seed_buf = np.zeros((h + 2, w + 2), np.uint8)
    for sx, sy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if ff[sy, sx] == 0:
            cv2.floodFill(ff, seed_buf, (sx, sy), 255)
    holes = cv2.bitwise_not(ff)
    holes = cv2.morphologyEx(holes, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n_lbl, labels, stats, _ = cv2.connectedComponentsWithStats(holes, connectivity=8)
    min_area = max(1, int(h * w * 0.002))
    kept = np.zeros_like(holes)
    n_kept = 0
    for i in range(1, n_lbl):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < min_area:
            continue
        bw = stats[i, cv2.CC_STAT_WIDTH]
        bh = stats[i, cv2.CC_STAT_HEIGHT]
        r = (bw + bh) / 4.0
        circ = area / (np.pi * r * r + 1e-6)
        aspect = bw / max(1, bh)
        if circ >= 0.6 and 0.5 <= aspect <= 2.0:
            kept[labels == i] = 255
            n_kept += 1
    coverage = cv2.countNonZero(kept) / float(h * w) if (h * w) else 0.0
    return kept, n_kept, coverage


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

    # --- Saturation fallback (Phase 2a / defect C) --------------------------
    # A light-but-not-"white" background (cream blanket, batting) passes the
    # white HSV range, so the blue_or_white mask calls ~everything foreground
    # and every mask-driven stage goes blind: Hough sees no circular edges and
    # the scale voter reads the background sheet as one giant button.  Logger_4
    # measured this on 19.2% of pipeline lots (guided exact 19.6% inside vs
    # 64.5% outside).  Fallback: rebuild as BLUE-ONLY + hole-fill and adopt it
    # when it is materially less saturated.  Verified on both real failed lots
    # (35/35 and 23-blue-of-26 recovered at the correct radius).  White buttons
    # are invisible to the fallback by construction — the white-rescue pass and
    # Gemini reconcile cover that deficit.
    _SAT_COVERAGE = 0.75
    _cov0 = (cv2.countNonZero(mask) / float(h * w)) if (h * w) else 0.0
    if _cov0 > _SAT_COVERAGE:
        # Hole-fill: flood the background from an empty corner and add back
        # the unreached interior.  Without it the slogan text punches holes
        # that cap the distance transform at the text-gap size (r_est 19.6 vs
        # 38 on the real failed 35-lot), poisoning every radius consumer.
        def _fb_finish(fb):
            fb = cv2.morphologyEx(fb, cv2.MORPH_CLOSE, kernel)
            fb = cv2.morphologyEx(fb, cv2.MORPH_OPEN, kernel)
            seed = next(((sx, sy) for (sx, sy) in
                         ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))
                         if fb[sy, sx] == 0), None)
            if seed is not None:
                ff = fb.copy()
                cv2.floodFill(ff, np.zeros((h + 2, w + 2), np.uint8), seed, 255)
                fb = cv2.bitwise_or(fb, cv2.bitwise_not(ff))
            return fb, (cv2.countNonZero(fb) / float(h * w)) if (h * w) else 0.0

        # Variant 1 — blue-only: blue buttons on a light background (the
        # quilt/batting lots).  Variant 2 — brighter-than-background, low
        # saturation: WHITE buttons on a mid-tone background (white Mellon
        # buttons on a gray mat), where blue-only keeps only the slogan text
        # (~5% speck coverage) and detection hunted text fragments.
        _fb1, _cov1 = _fb_finish(cv2.inRange(hsv, lower_blue, upper_blue))
        _fb2, _cov2 = _fb_finish(((hsv[:, :, 2] > bg_mean_v + 60)
                                  & (hsv[:, :, 1] < 80)).astype(np.uint8) * 255)
        # Adopt the first PLAUSIBLE variant: buttons occupy a real fraction of
        # a lot photo, so require >= 8% coverage (the old 2% floor let the
        # text-specks mask through on the white-button lot) and stay below
        # the saturation bound.  Blue-only preferred (dominant button colour).
        _adopt = None
        for _fb_lbl, _fbm, _fbc in (("blue", _fb1, _cov1),
                                    ("bright", _fb2, _cov2)):
            if 0.08 <= _fbc <= _SAT_COVERAGE:
                _adopt = (_fb_lbl, _fbm, _fbc)
                break
        if _adopt is not None:
            _fb_lbl, _fbm, _covf = _adopt
            print(f">>> DETECT: mask saturated (coverage={_cov0:.2f}) -> "
                  f"{_fb_lbl} hole-filled fallback adopted "
                  f"(coverage={_covf:.2f}).", flush=True)
            mask = _fbm
            fill_threshold = 0.30   # solid-disk fallback masks: white-bg threshold
            if diag_out is not None:
                diag_out["mask_path"] = ((diag_out.get("mask_path") or "")
                                         + "+satfallback_" + _fb_lbl)
        else:
            print(f">>> DETECT: mask saturated (coverage={_cov0:.2f}); no "
                  f"plausible fallback (blue={_cov1:.2f}, bright={_cov2:.2f}) "
                  f"— keeping original mask.", flush=True)

    # --- Dark-buttons-on-light-background inversion (buttons-as-holes) -------
    # Near-black buttons on a white matte match neither the blue nor the white
    # HSV range, so the mask above holds the bright matte as foreground and each
    # button is an enclosed hole; every mask-driven stage then rejects on-button
    # circles and lands crops in the white pockets between buttons.  When the mask
    # holds several button-sized circular enclosed holes, invert to them so the
    # buttons become foreground.  This signature does not arise when the buttons
    # ARE the foreground (the background then reaches the border, enclosing
    # nothing), so ordinary blue/white/navy lots are untouched.
    if _hole_invert_enabled():
        _holes, _n_holes, _hole_cov = _buttons_as_holes(mask, h, w)
        if _n_holes >= 4 and HOLE_INVERT_MIN_COVERAGE <= _hole_cov <= 0.75:
            print(f">>> DETECT: dark-on-light inversion — {_n_holes} circular "
                  f"button-holes (cov={_hole_cov:.2f}) → mask inverted to holes.",
                  flush=True)
            mask = _holes
            fill_threshold = 0.30   # solid-disk mask: white-bg fill threshold
            if diag_out is not None:
                diag_out["mask_path"] = ((diag_out.get("mask_path") or "")
                                         + "+holeinvert")

    gray = cv2.GaussianBlur(mask, (9, 9), 2)

    # Mask connected-component count (cheap localization-quality signal):
    #   components >> count → mask fragments buttons; << count → buttons merged.
    mask_components = None
    mask_blobs_raw  = None
    dt_peaks_total  = None
    try:
        _n_lbl, _labels, _stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        _min_area = max(1, int(h * w * 0.001))
        mask_components = int(sum(
            1 for _i in range(1, _n_lbl) if _stats[_i, cv2.CC_STAT_AREA] >= _min_area
        ))
        mask_blobs_raw = int(_n_lbl - 1)
        # Count-free over-merge signal (log_analysis.md gap 5): per-blob
        # distance-transform peak count.  A blob of N fused buttons keeps its
        # DT maximum ≈ one button radius (the neck between touching circles
        # stays shallower), so thresholding each blob's DT at 0.55× its own
        # max — the blob-buster's core convention — splits it into ~one core
        # per button with NO expected count.  The summed core count is a
        # count-free estimate of how many buttons the mask holds; grade it
        # against gemini_button_count per lot-size bucket.
        _dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
        _peaks = 0
        for _i in range(1, _n_lbl):
            if _stats[_i, cv2.CC_STAT_AREA] < _min_area:
                continue
            _x0 = _stats[_i, cv2.CC_STAT_LEFT]
            _y0 = _stats[_i, cv2.CC_STAT_TOP]
            _bw = _stats[_i, cv2.CC_STAT_WIDTH]
            _bh = _stats[_i, cv2.CC_STAT_HEIGHT]
            _bl = (_labels[_y0:_y0 + _bh, _x0:_x0 + _bw] == _i)
            _bd = np.where(_bl, _dist[_y0:_y0 + _bh, _x0:_x0 + _bw], 0.0)
            _rmax = float(_bd.max())
            if _rmax <= 0:
                continue
            _cores = (_bd >= 0.55 * _rmax).astype(np.uint8)
            _n_cores, _ = cv2.connectedComponents(_cores, connectivity=8)
            _peaks += max(1, int(_n_cores - 1))
        dt_peaks_total = int(_peaks)
    except Exception as _cc_err:
        print(f">>> DETECT: connectedComponents failed: {_cc_err}", flush=True)
    # Mask coverage: fraction of the image the mask calls foreground. Near 1.0
    # means the mask saturated (background bled in — e.g. a cream blanket passing
    # the "white" range) and every mask-driven stage downstream is blind; the
    # trigger condition for the blue-only saturation fallback (defect C).
    mask_coverage = round(cv2.countNonZero(mask) / float(h * w), 4) if (h * w) else None
    if diag_out is not None:
        diag_out["mask_components"] = mask_components
        diag_out["mask_blobs_raw"]  = mask_blobs_raw
        diag_out["dt_peaks_total"]  = dt_peaks_total
        diag_out["mask_coverage"]   = mask_coverage

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


def _fit_lattice(coords, n, init):
    """Snap n evenly-spaced band centres onto the buttons detection DID find.

    ``init`` is the even-extent guess (already close).  Robustly fit
    ``centre = a + b*i`` to the found ``coords`` (button x- or y-positions):
    assign each coord to its nearest band, keep only the near ones (INLIERS, so
    off-grid noise on a saturated blue-on-blue mask is rejected), least-squares
    fit the origin ``a`` and pitch ``b``, and iterate.  A handful of reliable
    anchors — the high-contrast white buttons Hough nails — pin the whole
    lattice; with too few inliers it falls back to ``init`` unchanged."""
    if len(coords) < 3 or len(init) < 2:
        return init
    coords = [float(c) for c in coords]
    cell = float(np.median(np.diff(sorted(init)))) or 1.0
    centers = [float(c) for c in init]
    for _ in range(4):
        pairs = []
        for c in coords:
            k = min(range(n), key=lambda i: abs(centers[i] - c))
            if abs(centers[k] - c) <= 0.40 * cell:
                pairs.append((k, c))
        if len({k for k, _ in pairs}) < 2:
            return [int(round(x)) for x in centers]
        ii = np.array([k for k, _ in pairs], dtype=float)
        cc = np.array([c for _, c in pairs], dtype=float)
        a, b = np.linalg.lstsq(np.vstack([np.ones_like(ii), ii]).T, cc, rcond=None)[0]
        if not (0.5 * cell < b < 2.0 * cell):    # reject an implausible pitch
            return [int(round(x)) for x in centers]
        centers = [a + b * i for i in range(n)]
    return [int(round(x)) for x in centers]


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


def _fill_veto_reason(cx, cy, cr, accepted_circles):
    """Phantom-fill veto for a single deficit-fill PROPOSAL (never applied to
    primary Hough pass-1 detections, and never used to drop an already-accepted
    circle) — see PROBLEM_IMAGE_FINDINGS: measured on live lots, deficit-fill
    mechanisms (blob-buster DT-peak proposals, white-rescue candidates) add
    phantom circles on shadows/fabric/grass whose (a) centre lies INSIDE an
    already-accepted circle's disc, or (b) radius is an outlier vs the accepted
    cohort.  A prototype of this exact rule vetoed 5/5 phantoms on a failing
    lot and produced a perfect 13/13.

    Returns None when the proposal is fine, else 'center' or 'radius' naming
    which check rejected it.

    accepted_circles: (x, y, r) tuples already accepted BEFORE this proposal —
    never circles from earlier in the same fill-in batch (they are only
    proposals until the merge accepts them, so treating them as authoritative
    cohort/anchor members would let the veto's own outputs feed itself).
    """
    if not accepted_circles:
        return None
    # (a) center-inside-circle: flat-lying buttons can't have a centre inside
    # another button's disc — 0.85 x r_existing is tighter than (and layered
    # on top of) the pre-existing 0.7 x min(r1, r2) overlap dedup, which lets
    # a SMALL phantom fully inside a big correct circle survive today.
    for (ax, ay, ar) in accepted_circles:
        if ar > 0 and math.hypot(cx - ax, cy - ay) < 0.85 * ar:
            return "center"
    # (b) radius-band vs the accepted cohort's MEASURED median (never the
    # count-derived expected_r — that prior is a known separate defect).
    # Skipped with < 2 accepted circles: no cohort to anchor to.
    if len(accepted_circles) >= 2:
        med_r = statistics.median(r for (_, _, r) in accepted_circles)
        if med_r > 0 and not (0.7 * med_r <= cr <= 1.3 * med_r):
            return "radius"
    return None


def _fill_proposal_ok(cx, cy, cr, accepted_circles):
    """True if a deficit-fill proposal survives the phantom veto (see
    _fill_veto_reason)."""
    return _fill_veto_reason(cx, cy, cr, accepted_circles) is None


def _merge_circle_sets(primary, secondary, fill_threshold, mask, diag_out=None):
    """Merge two (x, y, r) circle lists, keeping all primary circles and adding
    secondary circles that are not already covered by a primary circle.

    A secondary circle is considered covered if its centre is within
    0.7 × min(r_primary, r_secondary) of any primary circle — the same overlap
    rule used throughout the detection pipeline.  Secondary circles that pass
    the merge are also required to pass the fill-ratio check so noise contour
    proposals don't inflate the count, and — since every ``secondary`` here is
    a deficit-fill PROPOSAL, never a primary Hough pass-1 detection — the
    phantom veto (_fill_veto_reason): reject a proposal centred inside an
    already-accepted circle's disc, or whose radius is an outlier vs the
    accepted cohort's median.  EBAYSCOUT_FILL_VETO=0 (or BUTTONMATCHER_FILL_VETO=0)
    disables it.

    Returns the merged list (primary circles first, then accepted secondary).
    """
    merged = list(primary)
    veto_on = _fill_veto_enabled()
    _center_vetoed = 0
    _radius_vetoed = 0
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
        if veto_on:
            _reason = _fill_veto_reason(sx, sy, sr, merged)
            if _reason == "center":
                _center_vetoed += 1
                continue
            if _reason == "radius":
                _radius_vetoed += 1
                continue
        merged.append((sx, sy, sr))
    if _center_vetoed or _radius_vetoed:
        print(f">>> FILL_VETO: rejected {_center_vetoed + _radius_vetoed} "
              f"proposals (center={_center_vetoed}, radius={_radius_vetoed})",
              flush=True)
        if diag_out is not None:
            diag_out["fill_vetoed"] = (diag_out.get("fill_vetoed", 0)
                                        + _center_vetoed + _radius_vetoed)
    return merged


def _circle_fill(mask, x, y, r):
    """Fraction of the circle's area covered by the mask."""
    cm = np.zeros(mask.shape, dtype=np.uint8)
    cv2.circle(cm, (int(x), int(y)), int(r), 255, -1)
    area = cv2.countNonZero(cv2.bitwise_and(mask, mask, mask=cm))
    return area / max(1.0, math.pi * float(r) * float(r))


def _collapse_concentric(circles, mask):
    """Collapse near-concentric detections of the same button (Phase 3 /
    defect B).  Logger_4: 68% of single-button lots overcounted unguided, with
    exactly +1 as the dominant mode (69/216) — a glare ring or printed inner
    circle detected alongside the true rim.  Two circles whose centres sit
    within 0.35x the larger radius cannot be adjacent buttons (adjacent
    centres are >= ~1.4r apart), so they are the same button: keep the
    better-filled one.
    """
    kept = []
    for c in sorted(circles, key=lambda c: c[2], reverse=True):
        x, y, r = c
        clash = None
        for i, (kx, ky, kr) in enumerate(kept):
            if math.hypot(x - kx, y - ky) < 0.35 * max(r, kr):
                clash = i
                break
        if clash is None:
            kept.append(c)
            continue
        kx, ky, kr = kept[clash]
        if _circle_fill(mask, x, y, r) > _circle_fill(mask, kx, ky, kr):
            kept[clash] = c
    return kept


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


def _deficit_fill_proposals(mask, accepted, expected_r, min_r, max_r, margin, h, w):
    """Fix C — deficit-fill counterpart to _distance_peak_proposals.

    When the guided Hough pass (plus radius-correction / blob-buster / white-
    rescue) lands close to but short of ``expected``, propose circles ONLY from
    mask blobs not already claimed by an accepted circle, instead of discarding
    every accepted circle for the blind projection grid.  Masking out the
    already-covered discs before running the distance transform stops the peak
    search from just re-finding buttons Hough already has, and keeps the
    proposal count honest (each is still subject to _merge_circle_sets' fill
    check and the phantom veto).

    Returns a list of (x, y, r) tuples (may be empty).
    """
    if not accepted:
        return _distance_peak_proposals(mask, expected_r, min_r, max_r, margin, h, w)
    covered = np.zeros(mask.shape, dtype=np.uint8)
    for (ax, ay, ar) in accepted:
        cv2.circle(covered, (int(ax), int(ay)), int(ar), 255, -1)
    unclaimed = cv2.bitwise_and(mask, cv2.bitwise_not(covered))
    return _distance_peak_proposals(unclaimed, expected_r, min_r, max_r, margin, h, w)


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


def _mask_radius_prior(mask, img_noglare, bg_mean_v, h, w):
    """Fix A — mask-evidence radius prior for the guided detector.

    detect_buttons' count-derived expected_r (math.sqrt(h*w/expected) * 0.35)
    is a heuristic that assumes buttons tile the image; PROBLEM_IMAGE_FINDINGS
    measured it reliably wrong at small counts (37->34 vs real 24; 5->107 vs
    78; 2->171 vs ~95), sometimes badly enough that the resulting Hough window
    EXCLUDES the true radius entirely.  estimate_button_scale already reads the
    radius off mask evidence with a confidence score and nailed several of
    these masks (0.98 conf on the turf lots) — it was previously wired only
    into the unguided path.  This runs it here too, over the adopted mask AND
    cheap colour variants (blue-only, bright/white-only — built the same way
    the saturation-fallback in _prepare_detection_image builds them) and
    returns the single highest-confidence estimate across all variants.
    estimate_button_scale hole-fills each candidate mask internally (redraws
    external contours filled) before measuring, so no separate hole-fill step
    is needed here.

    Returns (r_est, conf, source_label) — r_est/conf are None when no variant's
    mask produced a usable blob.
    """
    variants = [("adopted", mask)]
    try:
        hsv = cv2.cvtColor(img_noglare, cv2.COLOR_BGR2HSV)
        lower_blue = np.array([90, 70, 40])
        upper_blue = np.array([140, 255, 255])
        kernel = np.ones((5, 5), np.uint8)

        blue_only = cv2.inRange(hsv, lower_blue, upper_blue)
        blue_only = cv2.morphologyEx(blue_only, cv2.MORPH_CLOSE, kernel)
        blue_only = cv2.morphologyEx(blue_only, cv2.MORPH_OPEN, kernel)
        variants.append(("blue_only", blue_only))

        bright = (((hsv[:, :, 2] > bg_mean_v + 60) & (hsv[:, :, 1] < 80))
                  .astype(np.uint8) * 255)
        bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, kernel)
        bright = cv2.morphologyEx(bright, cv2.MORPH_OPEN, kernel)
        variants.append(("bright", bright))
    except Exception as _mrp_err:
        print(f">>> DETECT: mask-radius-prior variant build failed "
              f"({_mrp_err}); adopted mask only.", flush=True)

    best_r, best_conf, best_src = None, 0.0, None
    for label, m in variants:
        try:
            r_est, conf, _filled, _sdiag = estimate_button_scale(m, h, w)
        except Exception as _es_err:
            print(f">>> DETECT: estimate_button_scale({label}) failed "
                  f"({_es_err}).", flush=True)
            continue
        if r_est is not None and conf > best_conf:
            best_r, best_conf, best_src = r_est, conf, label
    return best_r, best_conf, best_src


WHITE_RESCUE_RIM_MIN = 0.50   # min circumference-gradient coverage to accept


def _rim_support(gbin, cx, cy, r, n=72, band=3):
    """Fraction of ``n`` circumference samples that have a gradient pixel within
    +/-``band`` px of radius ``r``.  A genuine button (blue OR sparse-print white)
    has a near-complete circular rim; textured background, glare, and printed
    text do not.  Robust exactly where interior-edge density fails: pale buttons
    with little print (rim strong, interior nearly blank) and textured borders
    that inflate a background-edge reference."""
    H, W = gbin.shape
    hits = 0
    for k in range(n):
        a = 2.0 * math.pi * k / n
        ca, sa = math.cos(a), math.sin(a)
        for dr in range(-band, band + 1):
            px = int(round(cx + (r + dr) * ca))
            py = int(round(cy + (r + dr) * sa))
            if 0 <= px < W and 0 <= py < H and gbin[py, px]:
                hits += 1
                break
    return hits / float(n)


def _white_rescue_pass(img_noglare, existing, r_est, fill_mask, h, w,
                       mask_informative=True, max_added=None, diag_out=None):
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

    Every candidate here is a deficit-fill PROPOSAL, so it is also subject to
    the phantom veto (_fill_veto_reason) against ``existing`` (the accepted
    circles at call time — never against other rescue candidates added
    earlier in this same call): reject a candidate centred inside an
    already-accepted circle's disc, or whose radius is an outlier vs the
    accepted cohort's median.  EBAYSCOUT_FILL_VETO=0 (or BUTTONMATCHER_FILL_VETO=0)
    disables it.

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

        # Gradient-magnitude binary for rim-support: the RIM is the signal that
        # survives when a button matches its background in colour (white-on-white)
        # or has sparse print (interior-edge density near background level).
        _gx = cv2.Sobel(gray_img, cv2.CV_32F, 1, 0, ksize=3)
        _gy = cv2.Sobel(gray_img, cv2.CV_32F, 0, 1, ksize=3)
        _gmag = cv2.magnitude(_gx, _gy)
        _gbin = (_gmag >= np.percentile(_gmag, 80)).astype(np.uint8)

        margin = int(min(h, w) * 0.05)
        added = []
        veto_on = _fill_veto_enabled()
        _center_vetoed = 0
        _radius_vetoed = 0
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
            # Accept on EITHER a coherent circular rim (works for pale/sparse
            # buttons the interior test misses) OR an edge-dense printed interior
            # (the original signal).  Rim-support is background-reference-free, so
            # a textured border can't suppress it the way it inflates bg_edge.
            _rim = _rim_support(_gbin, x, y, r)
            if _rim < WHITE_RESCUE_RIM_MIN and edge_in < max(0.04, bg_edge * 1.5):
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
            if veto_on:
                _reason = _fill_veto_reason(x, y, r, existing)
                if _reason == "center":
                    _center_vetoed += 1
                    continue
                if _reason == "radius":
                    _radius_vetoed += 1
                    continue
            added.append((x, y, r))
        if _center_vetoed or _radius_vetoed:
            print(f">>> FILL_VETO: rejected {_center_vetoed + _radius_vetoed} "
                  f"proposals (center={_center_vetoed}, radius={_radius_vetoed})",
                  flush=True)
            if diag_out is not None:
                diag_out["fill_vetoed"] = (diag_out.get("fill_vetoed", 0)
                                            + _center_vetoed + _radius_vetoed)
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

        # --- Phase 3 (defect B): radius-consistency + concentric dedup ----
        # Genuine buttons in one lot share a radius; a detection whose radius
        # is far off the set median is glare/print/background.  Mirrors the
        # guided path's 0.7-1.3x-median cleanup (>=3 circles so the median is
        # meaningful), then collapses near-concentric duplicates of the same
        # button (the dominant single-lot overcount mode in Logger_4).
        _pre_band = len(inlier_circles)
        if len(inlier_circles) >= 3:
            _bmed = float(np.median([c[2] for c in inlier_circles]))
            _band = [c for c in inlier_circles
                     if 0.7 * _bmed < c[2] < 1.3 * _bmed]
            if _band:
                inlier_circles = _band
        _pre_conc = len(inlier_circles)
        inlier_circles = _collapse_concentric(inlier_circles, mask)
        if len(inlier_circles) != _pre_band:
            print(f">>> DETECT_UNGUIDED: radius/concentric dedup "
                  f"{_pre_band} -> {len(inlier_circles)} "
                  f"(band -{_pre_band - _pre_conc}, "
                  f"concentric -{_pre_conc - len(inlier_circles)})", flush=True)

        selected_count = len(inlier_circles)

        # Phase 3.5: AUTO additionally requires the scale-first radius path —
        # Logger_4 split gate=auto cleanly on it (scale_first 40/40 exact;
        # all six wrong autos came from the fallback paths).
        gate = dgate.gate_decision(
            confidence=winner_score, layout_conf=layout_conf,
            selected=selected_count, est_rows=est_rows, est_cols=est_cols,
            scale_path=scale_path,
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

    # --- Fix A: mask-evidence radius prior ---------------------------------
    # The count/geometry guess above is reliably wrong at small counts and can
    # exclude the true radius from the Hough window entirely (root cause #7 —
    # see _mask_radius_prior's docstring).  Consult estimate_button_scale over
    # the adopted mask + cheap colour variants and adopt its estimate when
    # confident enough; otherwise keep the count-derived guess.
    radius_source = "count_prior"
    _mr_est = _mr_conf = None
    _mr_src = None
    if _mask_radius_prior_enabled():
        _mr_est, _mr_conf, _mr_src = _mask_radius_prior(
            mask, prep["img_noglare"], _bg_mean_v, h, w)
        if _mr_est is not None and _mr_conf >= MASK_RADIUS_PRIOR_CONF_THRESHOLD:
            print(f">>> DETECT: mask radius prior ADOPTED — {_mr_src} "
                  f"r_est={_mr_est:.1f} conf={_mr_conf:.2f} "
                  f"(count-derived expected_r was {expected_r}) → using "
                  f"mask estimate.", flush=True)
            expected_r = int(round(_mr_est))
            radius_source = "mask_scale"
        elif _mr_est is not None:
            print(f">>> DETECT: mask radius prior available but below "
                  f"threshold ({_mr_src} r_est={_mr_est:.1f} conf={_mr_conf:.2f} "
                  f"< {MASK_RADIUS_PRIOR_CONF_THRESHOLD}) — keeping "
                  f"count-derived expected_r={expected_r}.", flush=True)

    min_r      = int(expected_r * 0.7)
    max_r      = int(expected_r * 1.3)
    # HARD RULE: a mask estimate whose confidence clears the threshold must
    # never be excluded by the final window, however expected_r got set above.
    if (_mr_est is not None and _mr_conf is not None
            and _mr_conf >= MASK_RADIUS_PRIOR_CONF_THRESHOLD
            and not (min_r <= _mr_est <= max_r)):
        min_r = min(min_r, int(_mr_est * 0.85))
        max_r = max(max_r, int(_mr_est * 1.15))
    if diag_out is not None:
        diag_out["expected_radius"] = int(expected_r)
        diag_out["buttons_per_megapixel"] = round(expected / ((h * w) / 1_000_000), 1) if (h * w) else None
        diag_out["radius_source"] = radius_source
        diag_out["mask_radius_est"] = round(float(_mr_est), 1) if _mr_est is not None else None
        diag_out["mask_radius_conf"] = round(float(_mr_conf), 4) if _mr_conf is not None else None
    print(f">>> DETECT: expected_r={expected_r}, min_r={min_r}, max_r={max_r}", flush=True)
    print(">>> DETECT: HoughCircles...", flush=True)
    _hough_dp, _hough_param1, _hough_param2 = 1.3, 120, 24
    _hough_mindist = int(expected_r * 1.7)
    circles = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=_hough_dp,
        minDist=_hough_mindist,
        param1=_hough_param1, param2=_hough_param2,
        minRadius=min_r,
        maxRadius=max_r
    )
    if circles is not None:
        circles = np.around(circles[0]).astype(int)
    print(f">>> DETECT: HoughCircles done. Found: {len(circles) if circles is not None else 0}", flush=True)
    if diag_out is not None:
        diag_out["hough_pass1_count"] = int(len(circles)) if circles is not None else 0
        # Detection-tuning instrumentation: the Hough params actually used (so
        # dense-miss failures trace to minDist/param2). Set here, beside the
        # already-logged expected_radius (line above), so they log on every path
        # where Hough runs — including when it finds nothing.
        diag_out["hough_dp"]         = _hough_dp
        diag_out["hough_mindist"]    = _hough_mindist
        diag_out["hough_param1"]     = _hough_param1
        diag_out["hough_param2"]     = _hough_param2
        diag_out["hough_minradius"]  = int(min_r) if min_r is not None else None
        diag_out["hough_maxradius"]  = int(max_r) if max_r is not None else None
    det_raw_hough     = len(circles) if circles is not None else 0
    det_count_noinput = None
    det_count_auto    = None
    det_radius_min = det_radius_max = det_radius_mean = det_radius_std = None

    # --- Starved-Hough fused-mask retry (white-envelope class, 2026-07-16) ---
    # A background sheet that passes the colour mask below the 0.75 saturation
    # trigger (envelope 0.72) fuses everything and pass-1 finds ~nothing.
    # Demonstrated failure + fused-band coverage → rebuild with the fallback
    # variants and re-run the SAME Hough once.  Healthy dense lots (coverage
    # 0.58-0.61 with a full pass-1) never enter.  Fail-open.
    if expected and det_raw_hough < max(2, int(expected * FUSED_RETRY_MAX_FRACTION)):
        try:
            _cov_now = (cv2.countNonZero(mask) / float(h * w)) if (h * w) else 0.0
            if FUSED_RETRY_MIN_COVERAGE < _cov_now <= 0.75:
                _fr = _fused_mask_fallback(image_bgr, _bg_mean_v, h, w)
                if _fr is not None:
                    _fr_mask, _fr_lbl, _fr_cov = _fr
                    print(f">>> DETECT: fused-mask retry — pass-1 found "
                          f"{det_raw_hough} of {expected} on a "
                          f"{_cov_now:.0%} mask; retrying with the "
                          f"{_fr_lbl} variant (coverage {_fr_cov:.0%}).",
                          flush=True)
                    mask = _fr_mask
                    gray = cv2.GaussianBlur(mask, (9, 9), 2)
                    _fill_threshold = 0.30
                    circles = cv2.HoughCircles(
                        gray, cv2.HOUGH_GRADIENT, dp=_hough_dp,
                        minDist=_hough_mindist,
                        param1=_hough_param1, param2=_hough_param2,
                        minRadius=min_r, maxRadius=max_r)
                    if circles is not None:
                        circles = np.around(circles[0]).astype(int)
                    det_raw_hough = len(circles) if circles is not None else 0
                    print(f">>> DETECT: fused-mask retry found "
                          f"{det_raw_hough} circles.", flush=True)
                    if diag_out is not None:
                        diag_out["mask_path"] = ((diag_out.get("mask_path") or "")
                                                 + "+fusedretry_" + _fr_lbl)
                        diag_out["hough_pass1_count"] = det_raw_hough
        except Exception as _fr_err:
            print(f"!!! DETECT: fused-mask retry failed (keeping original "
                  f"mask): {_fr_err}", flush=True)

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
        _all_circles = list(circles)
        circles = [(x, y, r) for (x, y, r) in _all_circles
                   if margin < x < w - margin and margin < y < h - margin]
        _det_border_removed = _n_pre_margin - len(circles)
        # Radii of circles the filters REJECT (border + fill + overlap) — the
        # over-count signal (glare/concentric rims are a different size than the
        # real button; feeds the rejected-radius telemetry).
        _rej_radii = [int(r) for (x, y, r) in _all_circles
                      if not (margin < x < w - margin and margin < y < h - margin)]

        _det_fill_removed    = 0
        _det_overlap_removed = 0
        filtered = []
        _fill_by_circle = {}
        for c in sorted(circles, key=lambda c: c[2], reverse=True):
            x, y, r = c

            # Reject circles that are not mostly blue/white
            circle_mask = np.zeros(mask.shape, dtype=np.uint8)
            cv2.circle(circle_mask, (x, y), r, 255, -1)
            blue_area   = cv2.countNonZero(cv2.bitwise_and(mask, mask, mask=circle_mask))
            fill_ratio  = blue_area / (np.pi * r * r)
            if fill_ratio < _fill_threshold:
                _det_fill_removed += 1
                _rej_radii.append(int(r))
                continue

            # Deduplicate nearby circles
            if not any(np.hypot(x - fx, y - fy) < min(r, fr) * 0.7 for fx, fy, fr in filtered):
                filtered.append((x, y, r))
                _fill_by_circle[(x, y, r)] = fill_ratio
            else:
                _det_overlap_removed += 1
                _rej_radii.append(int(r))

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

        if truncate_to_expected and expected and len(filtered) > expected:
            # More plausible circles survived than `expected` — pick the
            # best-filled ones rather than whatever order Hough's accumulator
            # happened to emit them in.  Raw Hough order is not a quality
            # signal; fill_ratio (already computed above) is, and matters most
            # exactly when Fix A's mask-radius-prior widens the window enough
            # to admit textured-background false positives at the true
            # button's radius (PROBLEM_IMAGE_FINDINGS case3: turf grass at the
            # same size band as the real button).  Only reorders which
            # survivors get kept on a surplus -- never changes who survives.
            filtered = sorted(
                filtered, key=lambda c: _fill_by_circle.get(c, 0.0), reverse=True
            )[:expected]
        elif truncate_to_expected:
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
            # Rejected-circle radii (the over-count / concentric-ring signal) —
            # only meaningful when Hough returned circles, so set here.
            if _rej_radii:
                diag_out["rej_radius_min"]    = int(min(_rej_radii))
                diag_out["rej_radius_median"] = int(np.median(_rej_radii))
                diag_out["rej_radius_max"]    = int(max(_rej_radii))

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
                cleaned = _merge_circle_sets(cleaned, _rc, _fill_threshold, mask,
                                              diag_out=diag_out)
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
            cleaned = _merge_circle_sets(cleaned, _dt, _fill_threshold, mask,
                                          diag_out=diag_out)
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
                diag_out=diag_out,
            )
            if _added:
                cleaned = cleaned + _added
                print(f">>> DETECT: white-rescue added {len(_added)} "
                      f"edge-supported circle(s) → {len(cleaned)} "
                      f"(deficit was {_deficit})", flush=True)
                if diag_out is not None:
                    diag_out["white_rescue"] = len(_added)
                    # Layer-2 measurability (log_analysis Logger_11 gap): tag
                    # the mask path so rim-rescued lots are queryable, and the
                    # recovered count flows to det_white_recovered — without
                    # these the white-on-white fix is invisible in production.
                    diag_out["mask_path"] = ((diag_out.get("mask_path") or "")
                                             + "+whitepass")
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

        # --- Fix C: deficit-fill instead of the projection cliff ---------------
        # The floor above is all-or-nothing: one circle short of it discarded
        # EVERY well-placed circle for the blind projection grid (root cause #1/
        # #2 — 28 good circles thrown away for a 37x1 phantom column).  When
        # what Hough (+ radius-correction + blob-buster + white-rescue) already
        # holds is at least DEFICIT_FILL_MIN_FRACTION of `expected`, keep those
        # circles and propose fill-ins ONLY from mask blobs no accepted circle
        # already covers — each still subject to _merge_circle_sets' fill check
        # and the phantom veto — rather than discarding everything.  Once a lot
        # earns this path it is committed: the projection fallback never runs,
        # even if the fill pass adds nothing.  Kill switch: EBAYSCOUT_DEFICIT_FILL
        # (or the shared BUTTONMATCHER_DEFICIT_FILL).
        # Quality gate: deficit-fill trusts the kept Hough circles enough to SKIP
        # the projection fallback, so it must only fire when the mask actually
        # isolated the buttons.  A mask that floods the frame (foreground fraction
        # over DEFICIT_FILL_MAX_MASK_FRACTION) has leaked the background in — Hough
        # then locks onto that texture and the "kept" circles are junk — so decline
        # and let the lot fall through to projection + Gemini reconcile.
        _mask_fraction = (cv2.countNonZero(mask) / mask.size) if (mask is not None and mask.size) else 0.0
        _deficit_decision = _deficit_fill_decision(
            _deficit_fill_enabled(), len(cleaned), expected, _enough,
            _small_complete, _mask_fraction)
        _deficit_filled = False
        if _deficit_decision == "commit":
            _fill_props = _deficit_fill_proposals(
                mask, cleaned, expected_r, min_r, max_r, margin, h, w)
            _before = len(cleaned)
            cleaned = _merge_circle_sets(cleaned, _fill_props, _fill_threshold, mask,
                                          diag_out=diag_out)
            if truncate_to_expected:
                cleaned = cleaned[:expected]
            _deficit_filled = True
            print(f">>> DETECT: deficit-fill (cleaned={_before} >= "
                  f"{DEFICIT_FILL_MIN_FRACTION:.0%} of expected={expected}, "
                  f"mask_fg={_mask_fraction:.0%}): "
                  f"+{len(cleaned) - _before} of {len(_fill_props)} "
                  f"unclaimed-blob proposals → {len(cleaned)}", flush=True)
            # Committed: this lot is kept on the Hough path, never falls
            # through to the projection grid, regardless of the fill outcome.
            _enough = True
        elif _deficit_decision == "decline":
            # Mask floods the frame → the kept circles can't be trusted to preempt
            # projection.  Leave _enough False so the projection fallback runs.
            print(f">>> DETECT: deficit-fill DECLINED — mask floods "
                  f"{_mask_fraction:.0%} of frame (> {DEFICIT_FILL_MAX_MASK_FRACTION:.0%}); "
                  f"mask did not isolate buttons, using projection fallback.", flush=True)
            if diag_out is not None:
                diag_out["deficit_declined_mask_fraction"] = round(_mask_fraction, 3)

        # Flooded-mask guard (generalises the deficit-fill gate to the WHOLE
        # acceptance): a non-small lot whose adopted mask floods the frame has not
        # isolated the buttons, so the guided Hough circles are background hits
        # (turf/carpet) EVEN WHEN they clear the acceptance floor — deficit-fill
        # never fires here because `_enough` is already met, so the deficit gate
        # above can't catch it (navy-on-carpet: mask 66%, 6 circles all on the rug,
        # accepted on the hough path).  Refuse the guided result and route to
        # projection + Gemini reconcile, which places buttons the mask can't see.
        # Small lots (expected <= 5) keep their small-complete behaviour untouched.
        # Shares DEFICIT_FILL_MAX_MASK_FRACTION and the deficit-fill kill switch.
        if (_guided_mask_floods(expected, _mask_fraction, _deficit_fill_enabled())
                and (_enough or _small_complete)):
            # Independent on-target check before refusing (§4.2): a dense lot
            # of large buttons legitimately reads as a "flooded" mask — the
            # difference from turf/carpet is that its accepted circles EXPLAIN
            # the mask foreground.  A failed/erroring check degrades to the
            # shipped refusal (never to a silent accept).
            _explained = 0.0
            try:
                _explained = _circles_explain_mask(mask, cleaned)
            except Exception as _ex_err:
                print(f">>> DETECT: flood explained-check failed "
                      f"({_ex_err}) — refusing as before.", flush=True)
            _flood_decision = _flood_refusal_decision(
                expected, _mask_fraction, _explained, _deficit_fill_enabled())
            if diag_out is not None:
                diag_out["guided_flood_explained"] = round(_explained, 3)
            if _flood_decision == "accept_dense":
                print(f">>> DETECT: flooded mask ({_mask_fraction:.0%}) sits "
                      f"almost entirely under the {len(cleaned)} accepted "
                      f"circles (residual "
                      f"{_mask_fraction * (1 - _explained):.0%} <= "
                      f"{FLOOD_RESIDUAL_MAX:.0%}) — dense lot, keeping "
                      f"guided circles.", flush=True)
            else:
                print(f">>> DETECT: guided acceptance REFUSED — mask floods "
                      f"{_mask_fraction:.0%} of frame (> {DEFICIT_FILL_MAX_MASK_FRACTION:.0%}) "
                      f"on a {expected}-button lot with residual "
                      f"{_mask_fraction * (1 - _explained):.0%} outside the "
                      f"circles (> {FLOOD_RESIDUAL_MAX:.0%}); guided circles "
                      f"are background hits, routing to projection.",
                      flush=True)
                if diag_out is not None:
                    diag_out["guided_refused_mask_fraction"] = round(_mask_fraction, 3)
                _enough = False
                _small_complete = False

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
            # A deficit-filled result is still hough-anchored (every accepted
            # circle came from Hough pass 1/radius-correction; the fill-ins are
            # phantom-vetoed proposals on top), so the label keeps the "hough"
            # prefix detect_gate.demote_auto_on_detector_bailout trusts — it
            # only demotes when the guided detector bailed to the projection
            # grid, which this path by definition did not.
            _det_label = dmask.detector_label("hough", _blob_used)
            if _deficit_filled:
                _det_label = _det_label + "+deficit"
            if diag_out is not None:
                diag_out["detector_used"] = _det_label
            _mpx = (h * w) / 1_000_000
            _bb  = "very_bright" if _bg_mean_v >= 192 else "bright" if _bg_mean_v >= 128 else "medium" if _bg_mean_v >= 64 else "dark"
            _sb  = "high_sat" if _bg_mean_s >= 128 else "medium_sat" if _bg_mean_s >= 64 else "low_sat"
            _rej = det_raw_hough - (det_count_noinput or 0)
            _telem = {
                "det_path":                   _det_label,
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
    _grid_is_user = rows is not None and cols is not None
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
        """Row/col centres for an n-band grid, anchored to the BUTTON EXTENT.

        Band the span where the mask is actually active — not the full frame —
        because on a bordered board or a margined page the buttons are inset, so
        equal-frame bands slide into the margins and every cell slips off its
        button (the blue-on-blue display-board case).  Within each extent band
        keep the even-lattice centre unless the mask shows a *clear* local
        maximum (a saturated blue-on-blue mask is flat, so its argmax is noise —
        the even centre is right; a clean mask refines to the real peak)."""
        _thr = float(proj.max()) * 0.15
        _nz = np.where(proj > _thr)[0]
        lo, hi = (int(_nz[0]), int(_nz[-1])) if len(_nz) else (0, length)
        span = max(1, hi - lo)
        cell = span / n
        centers = []
        for i in range(n):
            even_c = lo + (i + 0.5) * cell
            s = lo + int(i * span / n)
            e = lo + int((i + 1) * span / n)
            seg = proj[s:e]
            # Refine the even centre to the mask peak ONLY when the peak is a
            # clear local max AND lands near the even centre — a light nudge for
            # a clean mask.  A flat/saturated blue-on-blue mask has no real peak
            # (or a far, noisy one), so the even lattice centre stands: that is
            # what aligns the display-board grid to its buttons.
            if seg.size and seg.max() > 0:
                pk = s + int(np.argmax(seg))
                strong = float(seg.max()) > 2.2 * (float(np.median(seg)) + 1e-6)
                centers.append(pk if (strong and abs(pk - even_c) < 0.15 * cell)
                               else int(even_c))
            else:
                centers.append(int(even_c))
        return centers

    row_centers = _band_peaks(row_proj, rows, h)
    col_centers = _band_peaks(col_proj, cols, w)

    # Anchor the grid to the buttons detection DID find, not just the mask
    # extent: the even-extent guess drifts with the extent estimate (and can
    # split a real circle), but the reliably-detected buttons — the high-contrast
    # white buttons on a navy board especially — are exact.  Fit the lattice to
    # them (robust to off-grid mask-Hough noise) so every cell lands on a button.
    # ``cleaned`` is unset when Hough was never run (a trivial lot); guard it.
    try:
        _cleaned_src = cleaned
    except NameError:
        _cleaned_src = []
    _anchor = [c for c in (_cleaned_src or [])
               if isinstance(c, (list, tuple)) and len(c) >= 2]
    if len(_anchor) >= 3:
        row_centers = _fit_lattice([c[1] for c in _anchor], rows, row_centers)
        col_centers = _fit_lattice([c[0] for c in _anchor], cols, col_centers)

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

            # Skip cells with almost no button pixels (background-only cells) —
            # but ONLY when the grid was inferred from the count.  When the
            # operator supplied rows x cols, they assert a button is in every
            # cell, so we trust that and emit all cells: the fill test would drop
            # real buttons whose mask is weak (a blue-on-blue board, a glare-
            # washed cell).  Any genuine background cell is rejected downstream
            # with "Is this a button? = No".
            if not _grid_is_user:
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


def _cluster_row_centers(ys, med_r):
    """Row-line y-positions for a set of button center-y values.

    Average-linkage 1-D clustering: seed every y as its own cluster, then
    repeatedly merge the two closest cluster CENTERS while they sit within ~1.3x a
    button radius, recomputing each merged center as its members' mean.  Merging by
    center distance (not by nearest raw neighbour, as a y-gap walk does) is what
    stops the single-linkage CHAINING that collapses a staggered board into one
    giant "row": on a hand-arranged board an intermediate-y button bridges two
    visual rows, and a gap walk then chains the whole board together.  Returns the
    sorted list of row-center y's."""
    pts = sorted(ys)
    if not pts:
        return []
    thresh = 1.3 * med_r
    if thresh <= 0:
        return [sum(pts) / len(pts)]
    clusters = [[y] for y in pts]                 # seeded in y-sorted order
    while len(clusters) > 1:
        centers = [sum(c) / len(c) for c in clusters]
        # clusters stay y-sorted, so the globally closest pair is always adjacent
        gap, i = min((centers[j + 1] - centers[j], j)
                     for j in range(len(clusters) - 1))
        if gap > thresh:
            break
        clusters[i] += clusters[i + 1]
        del clusters[i + 1]
    return [sum(c) / len(c) for c in clusters]


def reading_order(circle_info):
    """Indices of ``circle_info`` in human reading order: top-to-bottom by row,
    left-to-right within each row.

    Detection/reconcile append recovered ("missed") buttons to the end of the crop
    list, so their crop numbers land last regardless of board position.  The Gemini
    pipeline numbers each crop by list position and draws that number on the
    annotated image, so without this the numbers jump around the board.  Sorting the
    crop + circle_info lists by this permutation renumbers everything into the order
    an operator actually scans.

    Rows are found by clustering center-y into row lines (``_cluster_row_centers``)
    and assigning each button to its nearest row line, then sorting by (row, x).
    This survives the vertical STAGGER of a hand-arranged board, where the older
    "start a new row on the first y-gap > tolerance" walk chained every row into one
    (a single bridging button merged neighbours, then the whole board collapsed into
    one left-to-right sweep).  Centers/radii come from _circle_center_radius so both
    circle (x/y) and rect (cx/cy) shapes work.  Falls back to a plain (y, x) sort
    when no radius is available, and returns list(range(n)) for n <= 1."""
    n = len(circle_info)
    if n <= 1:
        return list(range(n))
    centers, radii = [], []
    for c in circle_info:
        (cx, cy), r = _circle_center_radius(c)
        centers.append((cx, cy))
        if r:
            radii.append(r)
    med_r = sorted(radii)[len(radii) // 2] if radii else 0
    if not med_r:
        # No radius signal ⇒ nothing to scale rows by; a plain reading sort is the floor.
        return sorted(range(n), key=lambda i: (centers[i][1], centers[i][0]))
    row_centers = _cluster_row_centers([c[1] for c in centers], med_r)

    def _row_of(i):
        y = centers[i][1]
        return min(range(len(row_centers)), key=lambda k: abs(row_centers[k] - y))

    return sorted(range(n), key=lambda i: (_row_of(i), centers[i][0]))


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


def _circle_fill(mask, cx, cy, r):
    """Fraction of the button mask under a circle's inner disc — the photometric
    'is this actually on a button' signal for the reconcile swap.  Returns 0..1."""
    rr = max(1, int(round(r * 0.8)))
    probe = np.zeros(mask.shape, dtype=np.uint8)
    cv2.circle(probe, (int(round(cx)), int(round(cy))), rr, 255, -1)
    area = cv2.countNonZero(probe)
    if not area:
        return 0.0
    return cv2.countNonZero(cv2.bitwise_and(mask, mask, mask=probe)) / area


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
        frame_fit=_frame_fit_enabled(),
    )

    # Two-signal swap/drop: fire whenever Hough has unmatched phantom-candidate
    # circles — pay for the button-mask + off-mask fill probe (the second signal
    # beside coverage geometry, gating the risky DROP) and re-plan.  An unbacked
    # off-mask circle is a Hough false-positive whether or not an uncovered Gemini
    # button can take its place, so DON'T gate on n_uncovered > deficit: a lone
    # real button + one carpet phantom has no button to recover yet must still drop
    # the phantom.  Kill switch: reconcile-swap flag.  On a flooded mask the phantom
    # scores high fill, so the off-mask test fails and no drop fires (fill only acts
    # where it is meaningful).
    _pt = plan["telemetry"]
    if (_reconcile_swap_enabled() and detected_centers and gemini_slogans
            and _pt.get("n_unmatched_crops")):
        try:
            _mask = _prepare_detection_image(image_bgr)["mask"]
            _mr = median_r or ggeo.median_radius(detected_radii) or max(1.0, 0.04 * min(h, w))
            _det_fills = [_circle_fill(_mask, cx, cy, (r or _mr))
                          for (cx, cy), r in zip(detected_centers, detected_radii)]
            plan = ggeo.plan_reconciliation(
                detected_centers, detected_radii, gemini_slogans, w, h,
                cover_factor=cover_factor, median_r=median_r,
                detected_fills=_det_fills,
                frame_fit=_frame_fit_enabled(),
            )
        except Exception as _swap_err:
            print(f">>> RECONCILE: swap fill probe skipped: {_swap_err}", flush=True)

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

    # A fill-gated swap drops the phantom crops the plan traded away.  Associate
    # over the SAME final set the caller will build — kept detected (in order),
    # then recovered — and reindex unmatched_crop_indices into that post-drop
    # space.  The caller drops ``dropped_crop_indices`` before appending recovered.
    dropped = set(plan.get("dropped_crop_indices") or [])
    kept_idx = [i for i in range(len(detected_centers)) if i not in dropped]
    _old2new = {old: new for new, old in enumerate(kept_idx)}

    final_centers = [detected_centers[i] for i in kept_idx]
    for rc in recovered_circle_info:
        final_centers.append((rc["x"], rc["y"]))
    crop_to_slogan, unmatched_gemini = ggeo.associate_slogans(
        final_centers, plan["gemini_px"], gemini_slogans,
    )

    # Anchor-gated recovery (1979-front): an unanchored association is the
    # evidence the fill-gated swap lacks on a flooded mask — a crop no Gemini
    # point explains, holding a slogan no crop explains.  Synthesize a crop at
    # that slogan's own point (same recipe/trust as the deficit misses) and
    # re-associate; the phantom crop is NOT dropped (the anchoring gate demotes
    # it to a manual card), so a wrong fire costs one extra card, never a lost
    # button.  Kill switch: BUTTONMATCHER_ANCHOR_RECOVERY=0.
    n_anchor_recovered = 0
    _ar_median = plan["median_r"] or ggeo.median_radius(detected_radii)
    if _anchor_recovery_enabled() and crop_to_slogan and _ar_median:
        final_radii = [detected_radii[i] for i in kept_idx] + \
                      [rc["r"] for rc in recovered_circle_info]
        _ar_fallback = _ar_median if _ar_median else max(1.0, 0.04 * min(h, w))
        for gi in ggeo.plan_anchor_recovery(
                final_centers, final_radii, crop_to_slogan,
                plan["gemini_px"], gemini_slogans, _ar_median):
            s = gemini_slogans[gi]
            gx, gy = plan["gemini_px"][gi]
            edge_r = ggeo.radius_from_edge(s, gx, gy, w, h)
            if edge_r is not None:
                r_px = min(3.0 * _ar_fallback, max(0.25 * _ar_fallback, edge_r))
            else:
                r_px = ggeo.size_to_radius_px(s.get("size"), w, h) or _ar_fallback
            x1, y1, x2, y2 = ggeo.synth_box(gx, gy, r_px, w, h)
            crop = image_bgr[y1:y2, x1:x2]
            if crop is None or crop.size == 0:
                continue
            recovered_crops.append(crop)
            recovered_circle_info.append({
                "shape": "circle",
                "x": int((x1 + x2) / 2),
                "y": int((y1 + y2) / 2),
                "r": int(r_px),
                "source": "gemini_recovered",
                "slogan": s.get("slogan"),
                "gemini_index": s.get("index"),
                "confidence": s.get("confidence"),
            })
            final_centers.append((int((x1 + x2) / 2), int((y1 + y2) / 2)))
            n_anchor_recovered += 1
        if n_anchor_recovered:
            crop_to_slogan, unmatched_gemini = ggeo.associate_slogans(
                final_centers, plan["gemini_px"], gemini_slogans,
            )
            print(f">>> RECONCILE ANCHOR_RECOVERY: {n_anchor_recovered} "
                  f"unanchored slogan(s) re-anchored to synthesized crops at "
                  f"their Gemini points.", flush=True)

    telemetry = dict(plan["telemetry"])
    telemetry["n_anchor_recovered"] = n_anchor_recovered
    telemetry["unmatched_gemini_slogans"] = [
        gemini_slogans[i].get("slogan") for i in unmatched_gemini
    ]
    _um = plan["unmatched_crops"]
    telemetry["unmatched_crop_indices"] = (
        [_old2new[i] for i in _um if i not in dropped] if _um is not None else None
    )
    telemetry["dropped_crop_indices"] = sorted(dropped)
    _ff = telemetry.get("frame_fit") or {}
    if _ff.get("applied"):
        print(f">>> RECONCILE FRAME_FIT: Gemini frame corrected "
              f"(x*{_ff['ax']}+{_ff['bx']}, y*{_ff['ay']}+{_ff['by']}) — "
              f"anchored {_ff['anchored_identity']}→{_ff['anchored_fit']}.",
              flush=True)
    print(
        f">>> RECONCILE: hough={telemetry['hough_count']} "
        f"gemini={telemetry['gemini_count']} recovered={len(recovered_crops)} "
        f"swapped={telemetry.get('n_swapped', 0)} "
        f"misses={telemetry['misses']} "
        f"unmatched_gemini={telemetry['unmatched_gemini_slogans']}",
        flush=True,
    )
    return recovered_crops, recovered_circle_info, crop_to_slogan, telemetry
