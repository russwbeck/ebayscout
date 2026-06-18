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
