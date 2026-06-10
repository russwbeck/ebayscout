"""Unit tests for detect_mask — pure-python, no numpy/cv2/torch needed.

Run with the bundled harness (pytest may be unavailable in some envs):
    python tests/run_detect_mask_tests.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import detect_mask as dm


# --- should_use_bg_diff ------------------------------------------------------

def test_uniform_background_enables_bg_diff():
    # A solid green / white / black backdrop has a tiny border spread.
    assert dm.should_use_bg_diff(4.0) is True
    assert dm.should_use_bg_diff(0.0) is True


def test_textured_background_disables_bg_diff():
    # Wood grain / quilt: high border spread → keep the colour mask only.
    assert dm.should_use_bg_diff(40.0) is False


def test_bg_diff_gate_is_inclusive_at_threshold():
    assert dm.should_use_bg_diff(dm.BG_DIFF_MAX_SPREAD) is True
    assert dm.should_use_bg_diff(dm.BG_DIFF_MAX_SPREAD + 0.01) is False


def test_bg_diff_gate_handles_bad_input():
    assert dm.should_use_bg_diff(None) is False
    assert dm.should_use_bg_diff("nan") is False


def test_custom_max_spread_is_respected():
    assert dm.should_use_bg_diff(30.0, max_spread=35.0) is True
    assert dm.should_use_bg_diff(30.0, max_spread=25.0) is False


# --- bg_diff_threshold -------------------------------------------------------

def test_threshold_floors_at_base_on_flat_background():
    # Perfectly flat background → threshold is exactly the base floor.
    assert dm.bg_diff_threshold(0.0) == dm.BG_DIFF_BASE_THRESHOLD


def test_threshold_rises_with_spread():
    # spread * mult overtakes the floor once the background is noisier.
    s = dm.BG_DIFF_BASE_THRESHOLD  # 25 * 3 = 75 > 25
    assert dm.bg_diff_threshold(s) == s * dm.BG_DIFF_SPREAD_MULT
    # always at least the floor
    assert dm.bg_diff_threshold(1.0) >= dm.BG_DIFF_BASE_THRESHOLD


def test_threshold_clamps_negative_spread():
    assert dm.bg_diff_threshold(-5.0) == dm.BG_DIFF_BASE_THRESHOLD


def test_threshold_handles_bad_input():
    assert dm.bg_diff_threshold(None) == dm.BG_DIFF_BASE_THRESHOLD


# --- mask_path_label ---------------------------------------------------------

def test_mask_path_label_annotates_only_when_used():
    assert dm.mask_path_label("blue_only", True) == "blue_only+bgdiff"
    assert dm.mask_path_label("blue_or_white", True) == "blue_or_white+bgdiff"
    assert dm.mask_path_label("blue_only", False) == "blue_only"


def test_mask_path_label_tolerates_empty_base():
    assert dm.mask_path_label("", True) == "+bgdiff"
    assert dm.mask_path_label(None, False) == ""


# --- select_peaks (blob-buster NMS) ------------------------------------------

def test_select_peaks_keeps_well_separated_candidates():
    cands = [(10, 10, 9.0), (100, 100, 8.0), (10, 200, 7.0)]
    kept = dm.select_peaks(cands, min_separation=30)
    assert len(kept) == 3


def test_select_peaks_collapses_overlapping_to_strongest():
    # Three candidates clustered within ~5px → one button; keep the strongest.
    cands = [(50, 50, 6.0), (52, 49, 9.0), (48, 51, 7.0)]
    kept = dm.select_peaks(cands, min_separation=30)
    assert len(kept) == 1
    assert kept[0] == (52, 49, 9.0)


def test_select_peaks_separates_touching_buttons():
    # Two button centres one expected-diameter apart stay separate.
    cands = [(100, 100, 20.0), (140, 100, 20.0)]
    kept = dm.select_peaks(cands, min_separation=30)
    assert len(kept) == 2


def test_select_peaks_is_strongest_first():
    cands = [(0, 0, 1.0), (100, 0, 5.0), (0, 100, 3.0)]
    vals = [v for _, _, v in dm.select_peaks(cands, min_separation=10)]
    assert vals == sorted(vals, reverse=True)


def test_select_peaks_empty():
    assert dm.select_peaks([], min_separation=10) == []


# --- clamp_radius ------------------------------------------------------------

def test_clamp_radius_within_band():
    assert dm.clamp_radius(15.4, 10, 20) == 15


def test_clamp_radius_clamps_low_and_high():
    assert dm.clamp_radius(3, 10, 20) == 10
    assert dm.clamp_radius(99, 10, 20) == 20


def test_clamp_radius_handles_bad_input():
    assert dm.clamp_radius(None, 10, 20) == 10


# --- detector_label ----------------------------------------------------------

def test_detector_label_annotates_only_when_used():
    assert dm.detector_label("hough", True) == "hough+blob"
    assert dm.detector_label("hough", False) == "hough"
    assert dm.detector_label(None, True) == "+blob"
