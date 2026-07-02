"""Unit tests for detect_gate (pure auto/suggest/manual gate — no cv2/numpy)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import detect_gate as dgate


def test_grid_consistent_exact():
    assert dgate.grid_is_consistent(12, 3, 4) is True       # full grid
    assert dgate.grid_is_consistent(9, 3, 4) is True        # missing last-row tail
    assert dgate.grid_is_consistent(8, 3, 4) is False       # too few
    assert dgate.grid_is_consistent(13, 3, 4) is False      # too many


def test_grid_consistent_degenerate():
    assert dgate.grid_is_consistent(0, 2, 2) is False
    assert dgate.grid_is_consistent(4, 0, 2) is False
    assert dgate.grid_is_consistent(None, 2, 2) is False
    assert dgate.grid_is_consistent("x", 2, 2) is False
    assert dgate.grid_is_consistent(1, 1, 1) is True        # single button


def test_gate_auto_requires_all_conditions():
    ok = dict(confidence=0.86, layout_conf=1.0, selected=4, est_rows=2, est_cols=2)
    assert dgate.gate_decision(**ok) == dgate.GATE_AUTO

    assert dgate.gate_decision(**{**ok, "confidence": 0.69}) != dgate.GATE_AUTO
    assert dgate.gate_decision(**{**ok, "layout_conf": 0.5}) != dgate.GATE_AUTO
    assert dgate.gate_decision(**{**ok, "selected": 0}) != dgate.GATE_AUTO
    # 5 circles can't fit a 2x2 grid
    assert dgate.gate_decision(**{**ok, "selected": 5}) != dgate.GATE_AUTO


def test_gate_suggest_band():
    assert dgate.gate_decision(
        confidence=0.60, layout_conf=0.5, selected=9, est_rows=3, est_cols=3
    ) == dgate.GATE_SUGGEST
    # High confidence but broken layout falls to suggest, not manual
    assert dgate.gate_decision(
        confidence=0.80, layout_conf=0.2, selected=9, est_rows=3, est_cols=3
    ) == dgate.GATE_SUGGEST


def test_gate_manual_floor():
    assert dgate.gate_decision(
        confidence=0.40, layout_conf=1.0, selected=4, est_rows=2, est_cols=2
    ) == dgate.GATE_MANUAL
    assert dgate.gate_decision(
        confidence=None, layout_conf=1.0, selected=4, est_rows=2, est_cols=2
    ) == dgate.GATE_MANUAL
    # Zero detections never auto/suggest
    assert dgate.gate_decision(
        confidence=0.9, layout_conf=0.0, selected=0, est_rows=1, est_cols=1
    ) == dgate.GATE_MANUAL


def test_sample_photo_regression():
    # Diagnostics produced by detect_unguided on the user's 4-button sample.
    assert dgate.gate_decision(
        confidence=0.8608, layout_conf=1.0, selected=4, est_rows=2, est_cols=2
    ) == dgate.GATE_AUTO
def test_hough_small_lot_acceptance_floor():
    """Locks the Hough acceptance floor in detect.py / detect_pipeline.py
    (`_enough or _small_complete`). That gate lives inline in the cv2/numpy
    detection path (not importable without cv2), so this mirrors its exact
    predicate as an executable spec — keep the two in sync.

    Logger_5 finding: the old hard floor of 6 forced every <=5-button lot onto
    the projection-grid fallback even when Hough cleanly found the buttons. The
    fix accepts a FEW-button Hough result that already matches expected (within
    1), leaving the >=6 grid branch untouched.
    """
    def accepts(cleaned, expected):
        _enough = cleaned >= max(6, expected - 4)
        _small_complete = expected <= 5 and cleaned >= max(1, expected - 1)
        return _enough or _small_complete

    # NEW: small lots accepted when Hough is essentially complete
    assert accepts(1, 1) is True          # single button, found
    assert accepts(2, 3) is True          # 3-button lot, one miss tolerated
    assert accepts(3, 3) is True
    assert accepts(4, 5) is True
    # small lots still rejected when Hough clearly under-found
    assert accepts(0, 1) is False
    assert accepts(1, 3) is False         # only 1 of 3 -> grid fallback
    assert accepts(3, 5) is False

    # >=6 regime UNCHANGED vs the original `len(cleaned) >= max(6, expected-4)`
    for expected in range(6, 40):
        for cleaned in range(0, expected + 2):
            assert accepts(cleaned, expected) == (cleaned >= max(6, expected - 4))
    # boundary: a 6-button lot with 5 circles still defers to grid
    assert accepts(5, 6) is False
    assert accepts(6, 6) is True


def test_auto_requires_scale_first_when_path_given():
    # Phase 3.5 (Logger_4): scale_first autos were 40/40 exact; every wrong
    # auto came from a fallback path — those now cap at SUGGEST.
    base = dict(confidence=0.9, layout_conf=0.95, selected=6,
                est_rows=2, est_cols=3)
    assert dgate.gate_decision(**base, scale_path="scale_first") == dgate.GATE_AUTO
    assert dgate.gate_decision(**base, scale_path="sweep_fallback") == dgate.GATE_SUGGEST
    assert dgate.gate_decision(**base, scale_path="scale_second_chance") == dgate.GATE_SUGGEST


def test_scale_path_none_keeps_old_behavior():
    base = dict(confidence=0.9, layout_conf=0.95, selected=6,
                est_rows=2, est_cols=3)
    assert dgate.gate_decision(**base) == dgate.GATE_AUTO
    assert dgate.gate_decision(**base, scale_path=None) == dgate.GATE_AUTO


def test_scale_path_does_not_rescue_low_confidence():
    # scale_first is necessary for AUTO, never sufficient.
    assert dgate.gate_decision(confidence=0.5, layout_conf=0.95, selected=6,
                         est_rows=2, est_cols=3,
                         scale_path="scale_first") == dgate.GATE_MANUAL
