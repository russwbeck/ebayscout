"""Unit tests for detect_color — colour-cast measurement and correction.

Synthetic images only (no fixtures): a bright "paper" field with dark "buttons"
on it, tinted to order.
"""

import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import detect_color as dcol


def _scene(tint=(1.0, 1.0, 1.0), exposure=1.0, paper=235, button=60):
    """A 400x400 white-paper field with four dark blue-ish buttons on it."""
    img = np.full((400, 400, 3), paper, dtype=np.float32)
    for cy in (110, 290):
        for cx in (110, 290):
            cv2.circle(img, (cx, cy), 60, (button + 90, button, button), -1)
    img *= np.array(tint, dtype=np.float32) * exposure
    return np.clip(img, 0, 255).astype(np.uint8)


# ── bright_region_saturation ──────────────────────────────────────────────

def test_neutral_paper_reads_low_saturation():
    assert dcol.bright_region_saturation(_scene()) < 10


def test_blue_cast_paper_reads_high_saturation():
    sat = dcol.bright_region_saturation(_scene(tint=(1.35, 0.85, 0.80)))
    assert sat > dcol.CAST_SATURATION


def test_a_dim_but_neutral_photo_is_not_a_cast():
    # The failure this module addresses is colour, not brightness — a dark
    # neutral photo must not be "corrected".
    assert not dcol.has_color_cast(_scene(exposure=0.35))


def test_cast_is_detected_regardless_of_exposure():
    assert dcol.has_color_cast(_scene(tint=(1.35, 0.85, 0.80), exposure=0.5))


def test_warm_cast_is_detected_too():
    assert dcol.has_color_cast(_scene(tint=(0.70, 0.90, 1.35)))


# ── neutralize_color_cast ─────────────────────────────────────────────────

def test_correction_neutralizes_the_paper():
    fixed = dcol.neutralize_color_cast(_scene(tint=(1.35, 0.85, 0.80)))
    assert dcol.bright_region_saturation(fixed) < 10


def test_correction_brightens_the_paper_to_target():
    # Neutral alone is not enough: white_bg needs bg_mean_v > 170, so the paper
    # has to come back bright as well.
    fixed = dcol.neutralize_color_cast(_scene(tint=(1.35, 0.85, 0.80),
                                              exposure=0.5))
    v = cv2.cvtColor(fixed, cv2.COLOR_BGR2HSV)[:, :, 2]
    paper = v[v >= np.percentile(v, 85)]
    assert paper.mean() > 170


def test_correction_of_a_neutral_photo_is_near_identity_in_hue():
    src = _scene()
    fixed = dcol.neutralize_color_cast(src)
    assert dcol.bright_region_saturation(fixed) < 10


def test_correction_does_not_move_geometry():
    # Circles found in the corrected frame map 1:1 onto the original, so the
    # correction must not resize, shift or crop.
    src = _scene(tint=(1.35, 0.85, 0.80))
    fixed = dcol.neutralize_color_cast(src)
    assert fixed.shape == src.shape
    assert fixed.dtype == src.dtype


def test_correction_leaves_the_input_unmodified():
    src = _scene(tint=(1.35, 0.85, 0.80))
    before = src.copy()
    dcol.neutralize_color_cast(src)
    assert np.array_equal(src, before)


def test_buttons_stay_darker_than_paper_after_correction():
    fixed = dcol.neutralize_color_cast(_scene(tint=(1.6, 0.75, 0.70)))
    gray = cv2.cvtColor(fixed, cv2.COLOR_BGR2GRAY)
    assert gray[110, 110] < gray[10, 10]      # button centre vs paper corner


def test_gains_are_clamped_on_a_degenerate_frame():
    # An almost-black frame has no paper to reference; the clamp must keep the
    # correction bounded instead of amplifying noise to white.
    black = np.zeros((64, 64, 3), dtype=np.uint8)
    out = dcol.neutralize_color_cast(black)
    assert out.shape == black.shape
    assert out.max() <= 255


def test_uniform_frame_does_not_crash():
    flat = np.full((64, 64, 3), 200, dtype=np.uint8)
    out = dcol.neutralize_color_cast(flat)
    assert out.shape == flat.shape


def test_threshold_separates_the_measured_real_photos():
    # Measured on the pair that prompted this: the neutral photo's paper sits
    # at ~22 saturation, the blue-cast photo of the SAME buttons at ~83.
    assert 22.5 < dcol.CAST_SATURATION < 82.5
