"""Unit tests for rerank (pure two-level reference re-rank — no cv2/torch)."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rerank as rr


def _tokenize(text):
    return re.findall(r"\b[a-z0-9]+\b", text.lower())


STOPWORDS = {"a", "an", "the", "and", "or", "of"}


def test_year_score_positive_when_year_stands_out():
    sims = {1973: 0.85, 1972: 0.74, 1974: 0.76, 1975: 0.73}
    s = rr.year_score(1973, sims, era_years={1972, 1973, 1974, 1975})
    assert s == 1.0   # 0.10+ gap saturates the contrast


def test_year_score_negative_when_year_lags_era():
    sims = {1973: 0.70, 1972: 0.80, 1974: 0.82}
    s = rr.year_score(1973, sims, era_years={1972, 1973, 1974})
    assert s < 0


def test_year_score_neutral_without_era_context():
    assert rr.year_score(1973, {1973: 0.8}, era_years={1973}) == 0.0
    assert rr.year_score(1999, {}, era_years={1998, 1999}) == 0.0


def test_year_score_ignores_years_outside_era():
    # A high-similarity year in a DIFFERENT era must not drag the contrast.
    sims = {1973: 0.80, 1972: 0.75, 2005: 0.95}
    s = rr.year_score(1973, sims, era_years={1972, 1973})
    assert s == 1.0


def test_sloganid_score_neutral_without_photos():
    assert rr.sloganid_score(None, [0.9, 0.8]) == 0.0


def test_sloganid_score_beats_peers():
    assert rr.sloganid_score(0.92, [0.80, 0.84]) == 1.0
    assert rr.sloganid_score(0.80, [0.92]) < 0


def test_sloganid_score_absolute_fallback_half_strength():
    # No peers: high similarity to this button's own photos is weaker evidence.
    s = rr.sloganid_score(0.92, [])
    assert 0 < s <= 0.5


def test_adjustment_bounded():
    assert rr.adjustment(99, 99) == rr.YEAR_WEIGHT + rr.SID_WEIGHT
    assert rr.adjustment(-99, -99) == -(rr.YEAR_WEIGHT + rr.SID_WEIGHT)
    assert rr.adjustment(0, 0) == 0.0


def test_similar_slogan_peers_other_years_only():
    phrases = ["Beat Pitt", "Beat Pitt Again", "Go Lions", "Beat Michigan"]
    years   = [1980, 1985, 1985, 1990]
    peers = rr.similar_slogan_peers("Beat Pitt", 1980, phrases, years,
                                    _tokenize, STOPWORDS)
    # Shares "beat"+"pitt" with idx1 (2 tokens), "beat" with idx3 (1 token);
    # never its own year (idx0) and never the no-overlap idx2.
    assert peers[0] == 1
    assert 3 in peers
    assert 0 not in peers and 2 not in peers


def test_similar_slogan_peers_stopwords_dont_match():
    phrases = ["The Lions", "The Tigers"]
    years   = [1980, 1985]
    assert rr.similar_slogan_peers("The Lions", 1980, phrases, years,
                                   _tokenize, STOPWORDS) == []


def test_rerank_results_promotes_and_resorts():
    results = [
        {"year": 1985, "slogan": "A", "overall": 0.70, "slogan_score": 0.5},
        {"year": 1986, "slogan": "B", "overall": 0.68, "slogan_score": 0.5},
    ]
    # 1986 gets the maximum positive delta, 1985 the maximum negative one.
    scores = {1985: (-1.0, -1.0), 1986: (1.0, 1.0)}
    out = rr.rerank_results(results, lambda r: scores[r["year"]])
    assert out[0]["year"] == 1986
    assert out[0]["overall"] == 0.78
    assert out[1]["overall"] == 0.60
    assert out[0]["rerank_delta"] == 0.10


def test_rerank_results_bounded_delta_cannot_exclude():
    # A wrong inference demotes by at most YEAR_WEIGHT+SID_WEIGHT.
    results = [{"year": 1985, "slogan": "A", "overall": 0.90, "slogan_score": 0.9}]
    out = rr.rerank_results(results, lambda r: (-1.0, -1.0))
    assert out[0]["overall"] >= 0.90 - (rr.YEAR_WEIGHT + rr.SID_WEIGHT) - 1e-9


def test_rerank_disabled_by_default():
    os.environ.pop("BUTTONMATCHER_RERANK", None)
    assert rr.rerank_enabled() is False
    os.environ["BUTTONMATCHER_RERANK"] = "1"
    assert rr.rerank_enabled() is True
    os.environ.pop("BUTTONMATCHER_RERANK", None)
