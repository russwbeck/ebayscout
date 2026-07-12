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


def test_plan_reconciliation_unmatched_crops_happy_path():
    # Three detected circles; Gemini only backs two of them → the third is a
    # Hough placement/non-button blind spot.
    detected_centers = [(100, 100), (300, 300), (700, 700)]
    detected_radii = [20, 20, 20]
    gemini = [
        {"index": 1, "slogan": "A", "x": 10, "y": 10, "size": None, "confidence": 0.9},
        {"index": 2, "slogan": "B", "x": 30, "y": 30, "size": None, "confidence": 0.9},
    ]
    out = gg.plan_reconciliation(detected_centers, detected_radii, gemini, 1000, 1000)
    assert out["unmatched_crops"] == [2]
    assert out["telemetry"]["n_unmatched_crops"] == 1


def test_plan_reconciliation_unmatched_crops_unknown_when_match_cannot_run():
    detected_centers = [(100, 100), (300, 300), (700, 700)]
    gemini = [
        {"index": 1, "slogan": "A", "x": 10, "y": 10, "size": None, "confidence": 0.9},
    ]
    # No median radius (no detected radii) → cover_dist is None → unknown, not
    # "all circles unmatched".
    out = gg.plan_reconciliation(detected_centers, [], gemini, 1000, 1000)
    assert out["unmatched_crops"] is None
    assert out["telemetry"]["n_unmatched_crops"] is None

    # Zero Gemini points with valid coordinates → unknown, not "all unmatched".
    gemini_no_coords = [{"index": 1, "slogan": "A", "x": None, "y": None, "confidence": 0.9}]
    out2 = gg.plan_reconciliation(detected_centers, [20, 20, 20], gemini_no_coords, 1000, 1000)
    assert out2["unmatched_crops"] is None
    assert out2["telemetry"]["n_unmatched_crops"] is None


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


# --- Two-signal reconcile swap ------------------------------------------------
# A Hough false-positive (carpet phantom) fills the count and the deficit cap then
# suppresses a real Gemini miss.  The swap drops the phantom (unbacked AND off the
# mask — two independent signals) and recovers the high-confidence miss in its
# place, count invariant.

def _swap_scene(phantom_fill=0.1, blue_conf=0.9):
    # 4 covered pairs + 1 carpet phantom (det idx4) + 1 uncovered blue (gem idx4)
    det_c = [(100, 100), (300, 100), (100, 300), (300, 300), (700, 700)]
    det_r = [50, 50, 50, 50, 50]
    det_fill = [0.9, 0.9, 0.9, 0.9, phantom_fill]
    gem = [{"index": 1, "slogan": "A", "x": 10, "y": 10, "confidence": 0.9},
           {"index": 2, "slogan": "B", "x": 30, "y": 10, "confidence": 0.9},
           {"index": 3, "slogan": "C", "x": 10, "y": 30, "confidence": 0.9},
           {"index": 4, "slogan": "D", "x": 30, "y": 30, "confidence": 0.9},
           {"index": 5, "slogan": "BLUE", "x": 90, "y": 10, "confidence": blue_conf}]
    return det_c, det_r, det_fill, gem


def test_swap_recovers_miss_and_drops_phantom():
    det_c, det_r, det_fill, gem = _swap_scene()
    out = gg.plan_reconciliation(det_c, det_r, gem, 1000, 1000, detected_fills=det_fill)
    assert [m["slogan"] for m in out["misses"]] == ["BLUE"]
    assert out["dropped_crop_indices"] == [4]
    assert out["telemetry"]["n_swapped"] == 1


def test_swap_skips_without_fills_backcompat():
    # No fills → old behaviour: deficit 0 recovers nothing, phantom kept.
    det_c, det_r, _f, gem = _swap_scene()
    out = gg.plan_reconciliation(det_c, det_r, gem, 1000, 1000)
    assert out["misses"] == [] and out["dropped_crop_indices"] == []


def test_swap_skips_when_phantom_on_mask():
    # Flooded/ambiguous mask: the unbacked circle scores HIGH fill, so it is NOT a
    # confident phantom → no drop, no swap (self-limiting where fill is unreliable).
    det_c, det_r, _f, gem = _swap_scene(phantom_fill=0.9)
    out = gg.plan_reconciliation(det_c, det_r, gem, 1000, 1000,
                                 detected_fills=[0.9] * 5)
    assert out["misses"] == [] and out["dropped_crop_indices"] == []


def test_swap_skips_low_confidence_miss():
    det_c, det_r, det_fill, gem = _swap_scene(blue_conf=0.3)
    out = gg.plan_reconciliation(det_c, det_r, gem, 1000, 1000, detected_fills=det_fill)
    assert out["misses"] == [] and out["dropped_crop_indices"] == []
