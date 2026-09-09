"""Unit tests for detect_scale (pure scale-consensus math — no cv2/numpy)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import detect_scale as dscale


def test_blob_vote_solo_uses_area_radius():
    v = dscale.blob_vote(r_dt=150, r_enc=165, r_area=160, circularity=0.85)
    assert v is not None
    assert v["merged"] is False
    assert v["radius"] == 160
    assert v["weight"] == 0.85


def test_blob_vote_merged_uses_dt_radius():
    # Two touching buttons: enclosing circle ~2x the single-button radius
    v = dscale.blob_vote(r_dt=160, r_enc=330, r_area=230, circularity=0.55)
    assert v is not None
    assert v["merged"] is True
    assert v["radius"] == 160
    assert v["weight"] == dscale.MERGED_VOTE_WEIGHT


def test_blob_vote_low_circularity_gets_floor_weight():
    v = dscale.blob_vote(r_dt=100, r_enc=110, r_area=105, circularity=0.2)
    assert v["weight"] == dscale.MIN_VOTE_WEIGHT


def test_blob_vote_degenerate_inputs_rejected():
    assert dscale.blob_vote(0, 100, 100, 0.9) is None
    assert dscale.blob_vote(100, -1, 100, 0.9) is None
    assert dscale.blob_vote(None, 100, 100, 0.9) is None


def test_weighted_median_basic():
    assert dscale.weighted_median([1, 2, 3], [1, 1, 1]) == 2
    # Heavy weight drags the median
    assert dscale.weighted_median([1, 2, 100], [1, 1, 10]) == 100
    assert dscale.weighted_median([], []) is None


def test_consensus_agreeing_votes_high_confidence():
    votes = [
        {"radius": 160, "weight": 0.9, "merged": False},
        {"radius": 158, "weight": 0.85, "merged": False},
        {"radius": 165, "weight": 0.88, "merged": False},
        {"radius": 162, "weight": 0.8, "merged": False},
    ]
    r, conf, n_merged = dscale.consensus_radius(votes)
    assert 158 <= r <= 165
    assert conf > 0.9
    assert n_merged == 0


def test_consensus_disagreeing_votes_low_confidence():
    votes = [
        {"radius": 40, "weight": 0.9, "merged": False},
        {"radius": 160, "weight": 0.9, "merged": False},
        {"radius": 90, "weight": 0.9, "merged": False},
    ]
    _r, conf, _m = dscale.consensus_radius(votes)
    assert conf < 0.6


def test_consensus_single_vote_capped():
    votes = [{"radius": 160, "weight": 0.9, "merged": False}]
    r, conf, _m = dscale.consensus_radius(votes)
    assert r == 160
    assert conf <= 0.60


def test_consensus_counts_merged_blobs():
    votes = [
        {"radius": 160, "weight": 0.9, "merged": False},
        {"radius": 155, "weight": 0.4, "merged": True},
    ]
    _r, _c, n_merged = dscale.consensus_radius(votes)
    assert n_merged == 1


def test_consensus_empty():
    assert dscale.consensus_radius([]) == (None, 0.0, 0)
    assert dscale.consensus_radius(None) == (None, 0.0, 0)


def test_sample_photo_regression():
    # The four blobs measured on the user's 4-button sample photo (true
    # radius ~165px in the 800px working frame) must produce a consensus
    # well inside the guided Hough window [0.7r, 1.3r] around the truth.
    blobs = [
        (149, 166, 161, 0.78),
        (154, 164, 159, 0.87),
        (174, 186, 181, 0.88),
        (173, 185, 180, 0.89),
    ]
    votes = [dscale.blob_vote(*b) for b in blobs]
    r, conf, n_merged = dscale.consensus_radius(votes)
    assert 165 * 0.85 <= r <= 165 * 1.15
    assert conf >= dscale.SCALE_CONF_MIN
    assert n_merged == 0


# --- Tiny-circle guard ------------------------------------------------------
#
# "Tiny circle syndrome": Hough fires on the round things PRINTED ON a button
# (letter bowls, the Mellon/cb logo roundel, the year) and on half-radius
# accumulator ghosts, and every downstream filter passed them — the fill-ratio
# test rewards them (a small disc inside a blue button is 100% "blue"), the
# overlap dedup scales by the SMALLER radius, and ebayscout's scan mode has no
# radius-consistency filter at all.

def test_contained_rejects_a_circle_inside_a_bigger_one():
    # The Mellon Bank logo roundel, ~0.55 of a button radius below its centre.
    button = (200, 200, 100)
    assert dscale.is_contained(200, 255, 14, [button])


def test_contained_ignores_a_neighbouring_button():
    # Adjacent buttons sit >= ~2r apart; same-size neighbours are the overlap
    # rule's business, never this one.
    assert not dscale.is_contained(400, 200, 100, [(200, 200, 100)])
    assert not dscale.is_contained(370, 200, 100, [(200, 200, 100)])


def test_contained_never_lets_a_small_circle_swallow_a_button():
    # Only a circle at least as large as the candidate can contain it, so a
    # stray speck accepted first can't veto the real button around it.
    assert not dscale.is_contained(200, 200, 100, [(205, 205, 12)])


def test_contained_handles_junk_input():
    assert not dscale.is_contained(1, 1, 1, [])
    assert not dscale.is_contained(1, 1, 1, None)
    assert not dscale.is_contained(None, 1, 1, [(0, 0, 50)])
    assert not dscale.is_contained(1, 1, 1, [(0, 0, 0), ("x", "y", "r"), (0,)])


def test_dominant_radius_is_not_dragged_by_a_minority_of_sub_features():
    # 24 real buttons + 9 sub-button circles: a plain median survives this too,
    # but the modal radius is what stays right as contamination grows.
    r_star, support = dscale.dominant_radius([102] * 24 + [25] * 9)
    assert r_star == 102
    assert support == 24


def test_dominant_radius_needs_containment_to_run_first():
    # THE ORDERING CONTRACT. Fed a raw Hough pass where sub-features OUTNUMBER
    # the buttons, the modal radius picks the wrong cluster and the band would
    # then delete every real button. That is why the caller
    # (_drop_subfeatures) always applies is_contained() first: sub-features sit
    # inside the buttons that carry them, so containment removes them
    # structurally — measured on the user's lot, the loose pass goes 98 -> 32
    # circles and the dominant radius lands on the real buttons (r*=106) — and
    # only the remainder, now a minority, ever reaches the band.
    r_star, _support = dscale.dominant_radius([102] * 24 + [25] * 25)
    assert r_star == 25, "if this ever passes at 102, the ordering note above " \
                         "can be relaxed — until then, containment runs first"


def test_dominant_radius_empty():
    assert dscale.dominant_radius([]) == (None, 0)
    assert dscale.dominant_radius(None) == (None, 0)
    assert dscale.dominant_radius(["x", -3, 0]) == (None, 0)


def test_cohort_band_keeps_real_size_variety_and_drops_sub_features():
    # One genuinely bigger button among 20 same-size ones survives; the
    # letter bowls do not.
    radii = [100] * 20 + [170] + [22, 24, 26]
    band = dscale.cohort_band(radii)
    assert band is not None
    lo, hi, r_star, support = band
    assert r_star == 100
    assert lo <= 170 <= hi          # the odd-sized real button is kept
    assert not (lo <= 26 <= hi)     # printing is not


def test_cohort_band_declines_to_judge_an_ambiguous_set():
    # Too few circles, or no radius a clear majority agree on: leave the set
    # alone rather than guess which size is the button. This is the recall
    # worry that kept scan mode filter-free in the first place.
    assert dscale.cohort_band([100, 50]) is None
    assert dscale.cohort_band([100, 100, 50, 50, 22, 22]) is None
    assert dscale.cohort_band([]) is None
    assert dscale.cohort_band(None) is None


def test_cohort_band_user_photo_regression():
    # The loose (param2=15) pass on the user's 24-button Mellon/CCB lot at the
    # 1400px scan frame: 24 real rims at r~102 plus sub-button circles on the
    # bank logos, letter bowls and year digits. The band must keep the buttons
    # and shed the printing.
    radii = [98, 99, 100, 101, 102, 102, 103, 103, 104, 104, 105, 105,
             106, 106, 107, 107, 108, 108, 109, 110, 110, 110, 102, 102]
    radii += [23, 25, 27, 28, 31, 33, 35, 51, 52, 52, 53, 55]
    band = dscale.cohort_band(radii)
    assert band is not None
    lo, hi, r_star, _support = band
    kept = [r for r in radii if lo <= r <= hi]
    assert len(kept) == 24, kept
    assert min(kept) >= 98
