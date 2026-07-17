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
    # the labeled phantom example: the dropped circle's geometry + off-mask fill
    sw = out["telemetry"]["swaps"]
    assert len(sw) == 1 and sw[0]["slogan"] == "BLUE"
    assert (sw[0]["phantom_x"], sw[0]["phantom_y"]) == (700, 700)
    assert sw[0]["phantom_fill"] == 0.1


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


def test_swap_drops_phantom_unpaired_when_miss_low_confidence():
    # An off-mask phantom is a Hough false-positive regardless of whether a
    # confident Gemini miss can replace it.  A low-confidence miss (0.3 < 0.70) is
    # NOT recovered, but the phantom is still dropped outright — it only inflated
    # the count.  The swap is logged with recovered=False and no slogan.
    det_c, det_r, det_fill, gem = _swap_scene(blue_conf=0.3)
    out = gg.plan_reconciliation(det_c, det_r, gem, 1000, 1000, detected_fills=det_fill)
    assert out["misses"] == []
    assert out["dropped_crop_indices"] == [4]
    assert out["telemetry"]["n_swapped"] == 1
    sw = out["telemetry"]["swaps"]
    assert len(sw) == 1
    assert sw[0]["recovered"] is False
    assert sw[0]["slogan"] is None
    assert (sw[0]["phantom_x"], sw[0]["phantom_y"]) == (700, 700)


def test_swap_drops_lone_phantom_no_uncovered_button():
    # Real regression — pipeline lot job c0f97de7 (IMG-20251014-WA0004, 449x800):
    # Gemini read exactly ONE button ("No Free Launch Here" at 39%,46%), Hough
    # found two circles: the real button (174,363,r102) and a huge phantom on the
    # carpet (323,181,r116).  deficit = max(0, 1-2) = 0 and the covered button
    # leaves zero uncovered, so the OLD `n_uncovered > deficit` gate never probed
    # and the phantom shipped as a 2nd button.  The phantom is off-mask (carpet),
    # so the two-signal drop must remove it even with nothing to recover.
    det_c = [(174, 363), (323, 181)]         # idx0 real button, idx1 carpet phantom
    det_r = [102, 116]
    det_fill = [0.9, 0.04]                    # idx1 scores ~0 on the button mask
    gem = [{"index": 1, "slogan": "No Free Launch Here",
            "x": 39.0, "y": 46.0, "confidence": 0.9}]
    out = gg.plan_reconciliation(det_c, det_r, gem, 449, 800, detected_fills=det_fill)
    assert out["misses"] == []
    assert out["dropped_crop_indices"] == [1]
    assert out["telemetry"]["n_swapped"] == 1
    assert out["telemetry"]["swaps"][0]["recovered"] is False


# --- assoc_anchored (2026-07-16 shifted-lot incident) --------------------------

def test_assoc_anchored_separates_correct_from_wrong_neighbor():
    # Fleet-wide correct associations measure ~0.07x radius
    # (det_gemini_anchored_json snap_frac_median); wrong-neighbor pairs ~2x.
    assert gg.assoc_anchored(3.5, 50) is True        # 0.07x — typical correct
    assert gg.assoc_anchored(100.0, 50) is False     # 2x — wrong neighbor
    # gate boundary: dist <= 0.75 * r
    assert gg.assoc_anchored(37.5, 50) is True
    assert gg.assoc_anchored(37.6, 50) is False


def test_assoc_anchored_1979_front_regression():
    # Real incident lot (job 822d38f1, "1979 front.jpg", 449x800): detection
    # dropped 3 circles on blank bag and missed 3 real buttons; the count
    # matched (12=12) so nothing was recovered, and unlimited nearest-neighbor
    # association paired the orphaned slogans onto the blank crops.  Distances
    # below are computed from the lot's detect_labels record.
    correct_pairs = [  # (dist, r) — all nine detected-on-button associations
        (5.9, 46), (5.0, 47), (8.1, 51), (8.6, 42), (6.0, 62),
        (8.4, 51), (31.6, 44), (4.7, 49), (10.1, 58),
    ]
    wrong_pairs = [    # blank-bag crops that got a real slogan and auto-confirmed
        (142.0, 54),   # c11 <- "Wave Good-bye"   (2.6x r)
        (177.0, 42),   # c0  <- "Turtle Soup"     (4.2x r)
        (324.0, 49),   # c10 <- "Can the Juice"   (6.6x r)
    ]
    assert all(gg.assoc_anchored(d, r) for d, r in correct_pairs)
    assert not any(gg.assoc_anchored(d, r) for d, r in wrong_pairs)


def test_assoc_anchored_fails_open_on_missing_or_bad_data():
    # Pre-telemetry lots (no dist) and rect crops (no radius) must not break.
    assert gg.assoc_anchored(None, 50) is True
    assert gg.assoc_anchored(10.0, None) is True
    assert gg.assoc_anchored("bad", 50) is True
    assert gg.assoc_anchored(10.0, "bad") is True


# --- plan_anchor_recovery (1979-front, second half) ----------------------------

def _ar_scene():
    """Miniature of the incident: 2 good crops, 1 blank-bag phantom; 3 Gemini
    points; the phantom holds the orphaned slogan at ~4x radius."""
    final_centers = [(100, 100), (300, 100), (150, 400)]   # c2 = phantom
    final_radii = [50, 50, 50]
    gemini_px = [(102, 103), (297, 101), (300, 300)]       # g2 = real missed button
    gem = [{"index": 1, "slogan": "A", "confidence": 0.9},
           {"index": 2, "slogan": "B", "confidence": 0.9},
           {"index": 3, "slogan": "C", "confidence": 0.9}]
    c2s = {0: {"gemini_idx": 0, "slogan": "A", "dist": 3.6},
           1: {"gemini_idx": 1, "slogan": "B", "dist": 3.2},
           2: {"gemini_idx": 2, "slogan": "C", "dist": 180.3}}  # unanchored
    return final_centers, final_radii, gemini_px, gem, c2s


def test_anchor_recovery_recovers_the_orphaned_slogan():
    fc, fr, gpx, gem, c2s = _ar_scene()
    assert gg.plan_anchor_recovery(fc, fr, c2s, gpx, gem, 50) == [2]


def test_anchor_recovery_skips_anchored_pairs():
    fc, fr, gpx, gem, c2s = _ar_scene()
    c2s[2]["dist"] = 30.0                      # 0.6x r — anchored
    assert gg.plan_anchor_recovery(fc, fr, c2s, gpx, gem, 50) == []


def test_anchor_recovery_requires_gemini_confidence():
    # Same trust gate as the fill-gated swap (SWAP_MIN_CONFIDENCE).
    fc, fr, gpx, gem, c2s = _ar_scene()
    gem[2]["confidence"] = 0.3
    assert gg.plan_anchor_recovery(fc, fr, c2s, gpx, gem, 50) == []
    gem[2]["confidence"] = None
    assert gg.plan_anchor_recovery(fc, fr, c2s, gpx, gem, 50) == []


def test_anchor_recovery_never_double_counts_a_sloppy_pair():
    # A correct-but-sloppy pair (dist just over the gate) has its point ON the
    # crop, well inside median_r of an existing center — must NOT synthesize a
    # duplicate crop next to the real one.
    fc, fr, gpx, gem, c2s = _ar_scene()
    gpx[2] = (155, 430)                        # 30px from the crop at (150,400)
    c2s[2]["dist"] = 40.0                      # 0.8x r — unanchored by a hair
    assert gg.plan_anchor_recovery(fc, fr, c2s, gpx, gem, 50) == []


def test_anchor_recovery_fails_open_on_missing_geometry():
    fc, fr, gpx, gem, c2s = _ar_scene()
    assert gg.plan_anchor_recovery(fc, fr, c2s, gpx, gem, None) == []
    gpx[2] = None
    assert gg.plan_anchor_recovery(fc, fr, c2s, gpx, gem, 50) == []


# --- fit_frame_map (1987-front dual incident) ----------------------------------

# The live sidecar's 9 real hough circles (job efb99c29, "1987 front.jpg",
# 599x800): detection found rows at y~166/300/437 (+1 table phantom at 689),
# while Gemini reported the same grid stretched over the full frame
# (rows at 19/48/76% -> y 152/384/608).
DET_1987 = [(99, 168, 67), (223, 163, 58), (89, 304, 64), (215, 298, 63),
            (365, 297, 62), (109, 445, 64), (231, 432, 65), (369, 434, 63),
            (34, 689, 74)]


def _gem_1987():
    xs = [15.0, 37.0, 60.0, 82.0]
    ys = [19.5, 48.0, 76.0]
    out = []
    i = 0
    for y in ys:
        for x in xs:
            i += 1
            out.append({"index": i, "slogan": f"s{i}", "x": x, "y": y,
                        "edge_x": x, "edge_y": y - 9.5, "confidence": 0.9})
    return out


def test_frame_fit_1987_regression():
    centers = [(x, y) for x, y, _ in DET_1987]
    radii = [r for _, _, r in DET_1987]
    med = gg.median_radius(radii)
    pts = [gg.pct_to_px(s["x"], s["y"], 599, 800) for s in _gem_1987()]
    fm = gg.fit_frame_map(centers, radii, pts, med)
    assert fm["applied"] is True
    assert abs(fm["ax"] - 1.0) < 1e-9 and abs(fm["bx"]) < 1e-9   # x was fine
    assert 0.55 < fm["ay"] < 0.65                                 # the y stretch
    assert fm["anchored_identity"] <= 3 and fm["anchored_fit"] >= 8


def test_frame_fit_heals_1987_recovery_positions():
    """Through plan_reconciliation: the corrected frame recovers the deficit at
    the REAL button positions (row1 col3/col4, row2 col4) instead of on blank
    table, and the only phantom suspect is the y=689 table circle."""
    centers = [(x, y) for x, y, _ in DET_1987]
    radii = [r for _, _, r in DET_1987]
    plan = gg.plan_reconciliation(centers, radii, _gem_1987(), 599, 800)
    ff = plan["telemetry"]["frame_fit"]
    assert ff["applied"] is True
    assert plan["telemetry"]["deficit"] == 3
    rec = sorted((round(m["gx"]), round(m["gy"])) for m in plan["misses"])
    for gx, gy in rec:
        assert gy < 320, rec           # all recovered in rows 1-2, never y~608
    assert plan["unmatched_crops"] == [8]
    # rim-point radii are measured in the CORRECTED frame (raw 9.5% of h = 76px
    # would blow the crop up; corrected ~0.6x that)
    for m in plan["misses"]:
        assert 35 <= m["r_px"] <= 55, m


def test_frame_fit_keeps_identity_on_healthy_lot():
    """1979-front: phantoms are not a linear-frame problem — no fit beats
    identity decisively, so the raw frame is kept."""
    det = [(328, 174, 42), (75, 237, 46), (184, 245, 47), (275, 246, 51),
           (383, 254, 42), (77, 341, 62), (187, 340, 51), (41, 447, 44),
           (175, 441, 49), (283, 449, 58), (112, 536, 49), (269, 537, 54)]
    xs = [16.0, 41.0, 63.0, 84.0]
    gem = []
    i = 0
    for y in (29.5, 43.0, 55.5):
        for x in xs:
            i += 1
            gem.append({"index": i, "slogan": f"s{i}", "x": x, "y": y,
                        "confidence": 0.9})
    plan = gg.plan_reconciliation([(x, y) for x, y, _ in det],
                                  [r for _, _, r in det], gem, 449, 800)
    ff = plan["telemetry"]["frame_fit"]
    assert ff["applied"] is False
    assert ff["ay"] == 1.0 and ff["by"] == 0.0


def test_frame_fit_fails_closed_on_small_or_degenerate_input():
    ident = {"ax": 1.0, "bx": 0.0, "ay": 1.0, "by": 0.0}
    fm = gg.fit_frame_map([(10, 10), (50, 50)], [5, 5], [(11, 11)], 5)
    assert fm["applied"] is False and {k: fm[k] for k in ident} == ident
    fm = gg.fit_frame_map([], [], [], None)
    assert fm["applied"] is False


def test_frame_fit_skips_when_already_fully_anchored():
    centers = [(100, 100), (300, 100), (100, 300), (300, 300)]
    radii = [50] * 4
    pts = [(102, 101), (297, 99), (99, 303), (301, 298)]
    fm = gg.fit_frame_map(centers, radii, pts, 50)
    assert fm["applied"] is False and fm["anchored_identity"] == 4
