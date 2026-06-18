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
