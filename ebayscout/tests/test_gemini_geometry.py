"""Unit tests for gemini_geometry — pure math, no cv2/numpy needed.

    python tests/run_gemini_geometry_tests.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gemini_geometry as gg


def test_pct_to_px():
    assert gg.pct_to_px(50, 50, 800, 600) == (400.0, 300.0)
    assert gg.pct_to_px(0, 0, 800, 600) == (0.0, 0.0)
    assert gg.pct_to_px(100, 100, 800, 600) == (800.0, 600.0)


def test_size_to_radius_px():
    # 10% of min(800,600)=600 → 60px
    assert gg.size_to_radius_px(10, 800, 600) == 60.0
    assert gg.size_to_radius_px(None, 800, 600) is None
    assert gg.size_to_radius_px(0, 800, 600) is None


def test_median_radius():
    assert gg.median_radius([10, 20, 30]) == 20
    assert gg.median_radius([10, 20, 30, 40]) == 25
    assert gg.median_radius([0, -5, None]) is None
    assert gg.median_radius([]) is None


def test_synth_box_clamps():
    # center near top-left corner clamps to 0
    assert gg.synth_box(5, 5, 10, 800, 600) == (0, 0, 16, 16)
    # center mid-image
    assert gg.synth_box(400, 300, 20, 800, 600) == (378, 278, 422, 322)
    # center near bottom-right clamps to bounds
    assert gg.synth_box(795, 595, 20, 800, 600) == (773, 573, 800, 600)


def test_match_points_one_to_one():
    a = [(0, 0), (100, 0)]
    b = [(2, 0), (98, 0)]
    pairs, ua, ub = gg.match_points(a, b)
    # a0↔b0, a1↔b1
    assert sorted((i, j) for i, j, _ in pairs) == [(0, 0), (1, 1)]
    assert ua == [] and ub == []


def test_match_points_respects_max_dist_and_leaves_unmatched():
    a = [(0, 0)]
    b = [(0, 0), (1000, 0)]
    pairs, ua, ub = gg.match_points(a, b, max_dist=10)
    assert pairs == [(0, 0, 0.0)]
    assert ua == []
    assert ub == [1]  # the far point is unmatched


def test_plan_reconciliation_finds_missed_button():
    # Two detected buttons; Gemini sees three (one missed at far right).
    detected_centers = [(100, 100), (300, 100)]
    detected_radii = [40, 40]
    gemini = [
        {"index": 1, "slogan": "A", "x": 10, "y": 10, "size": None, "confidence": 0.9},   # ~ (100,100)
        {"index": 2, "slogan": "B", "x": 30, "y": 10, "size": None, "confidence": 0.9},   # ~ (300,100)
        {"index": 3, "slogan": "C", "x": 50, "y": 10, "size": None, "confidence": 0.9},   # ~ (500,100) MISS
    ]
    out = gg.plan_reconciliation(detected_centers, detected_radii, gemini, 1000, 1000)
    assert out["median_r"] == 40
    assert len(out["covered"]) == 2
    assert len(out["misses"]) == 1
    miss = out["misses"][0]
    assert miss["slogan"] == "C"
    assert miss["gemini_idx"] == 2
    # synth box centered on (500,100) with r≈median 40
    x1, y1, x2, y2 = miss["box"]
    assert x1 < 500 < x2 and y1 < 100 < y2
    assert out["telemetry"]["n_recovered"] == 1
    assert out["telemetry"]["hough_count"] == 2


def test_plan_reconciliation_uses_gemini_size_when_present():
    detected_centers = []
    detected_radii = []
    gemini = [{"index": 1, "slogan": "Solo", "x": 50, "y": 50, "size": 10, "confidence": 0.8}]
    out = gg.plan_reconciliation(detected_centers, detected_radii, gemini, 1000, 1000)
    # no detections → median_r None → uses size (10% of 1000 = 100px radius)
    miss = out["misses"][0]
    assert miss["r_px"] == 100.0


def test_plan_reconciliation_no_double_count_when_covered():
    detected_centers = [(500, 500)]
    detected_radii = [50]
    gemini = [{"index": 1, "slogan": "A", "x": 50, "y": 50, "size": None, "confidence": 0.9}]
    out = gg.plan_reconciliation(detected_centers, detected_radii, gemini, 1000, 1000)
    assert out["misses"] == []
    assert len(out["covered"]) == 1


def test_deficit_cap_prevents_double_count_when_coverage_misaligns():
    # Image-2 case: Hough found all 5, Gemini reports 5, but the coverage test
    # mis-aligns (tiny median radius → nothing counts as "covered"). The deficit
    # is 5-5=0, so NOTHING is recovered (no phantom double-count).
    detected_centers = [(100, 500), (300, 500), (500, 500), (700, 500), (900, 500)]
    detected_radii = [3, 3, 3, 3, 3]   # tiny → cover_dist tiny → coverage misses
    gemini = [
        {"index": i + 1, "slogan": s, "x": x, "y": 50, "size": None, "confidence": 0.9}
        for i, (s, x) in enumerate([("A", 10), ("B", 30), ("C", 50), ("D", 70), ("E", 90)])
    ]
    out = gg.plan_reconciliation(detected_centers, detected_radii, gemini, 1000, 1000)
    assert out["telemetry"]["deficit"] == 0
    assert out["misses"] == []          # no phantom crops despite coverage misfire


def test_deficit_cap_recovers_only_the_shortfall():
    # Hough found 3, Gemini reports 5 → recover at most 2 (the farthest uncovered).
    detected_centers = [(100, 500), (300, 500), (500, 500)]
    detected_radii = [3, 3, 3]
    gemini = [
        {"index": i + 1, "slogan": s, "x": x, "y": 50, "size": None, "confidence": 0.9}
        for i, (s, x) in enumerate([("A", 10), ("B", 30), ("C", 50), ("D", 70), ("E", 90)])
    ]
    out = gg.plan_reconciliation(detected_centers, detected_radii, gemini, 1000, 1000)
    assert out["telemetry"]["deficit"] == 2
    assert len(out["misses"]) == 2
    # the two farthest-right Gemini points (D@700, E@900) are the recovered ones
    assert {m["slogan"] for m in out["misses"]} == {"D", "E"}


def test_associate_slogans_per_button():
    final_centers = [(100, 100), (500, 100)]
    gemini = [
        {"index": 1, "slogan": "Near1", "x": 10, "y": 10},
        {"index": 2, "slogan": "Near2", "x": 50, "y": 10},
    ]
    gemini_px = [gg.pct_to_px(s["x"], s["y"], 1000, 1000) for s in gemini]
    crop_to_slogan, unmatched = gg.associate_slogans(final_centers, gemini_px, gemini)
    assert crop_to_slogan[0]["slogan"] == "Near1"
    assert crop_to_slogan[1]["slogan"] == "Near2"
    assert unmatched == []


def test_associate_handles_extra_gemini_slogan():
    final_centers = [(100, 100)]
    gemini = [
        {"index": 1, "slogan": "Matched", "x": 10, "y": 10},
        {"index": 2, "slogan": "Orphan", "x": 90, "y": 90},
    ]
    gemini_px = [gg.pct_to_px(s["x"], s["y"], 1000, 1000) for s in gemini]
    crop_to_slogan, unmatched = gg.associate_slogans(final_centers, gemini_px, gemini)
    assert crop_to_slogan[0]["slogan"] == "Matched"
    assert unmatched == [1]  # the orphan gemini slogan matched no crop


def test_radius_from_edge():
    # center (500,500)px, rim point 5% right → (550,500) → 50px
    assert gg.radius_from_edge({"edge_x": 55, "edge_y": 50}, 500, 500, 1000, 1000) == 50.0
    # missing rim point → None (caller falls back)
    assert gg.radius_from_edge({}, 500, 500, 1000, 1000) is None
    # degenerate (edge == center) → None
    assert gg.radius_from_edge({"edge_x": 50, "edge_y": 50}, 500, 500, 1000, 1000) is None


def test_plan_reconciliation_prefers_rim_point_over_size():
    # No detections → median None → fallback_r = 0.04*min = 40, clamp [10,120].
    # Rim point (5% right of center) → 50px should WIN over size=10 (→100px).
    gemini = [{"index": 1, "slogan": "Solo", "x": 50, "y": 50,
               "size": 10, "edge_x": 55, "edge_y": 50, "confidence": 0.8}]
    out = gg.plan_reconciliation([], [], gemini, 1000, 1000)
    assert out["misses"][0]["r_px"] == 50.0
