"""
Tests for ebayscout/pipeline_classify.py — the Gemini-pipeline autoconfirmation
decision tree. Pure-python (config + scoring only; no torch/cv2/GCS).

    python tests/run_pipeline_classify_tests.py
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import pipeline_classify as pc


def _diag(year, slogan, overall, gap):
    """One crop's diagnostics: a single top candidate + its #1-vs-#2 gap."""
    return {"candidates": [{"year": year, "slogan": slogan, "overall": overall}],
            "gap": gap}


# --- Gemini works: autoconfirm-or-ignore, no yellow -------------------------

def test_green_autoconfirms_clip_only():
    # No Gemini resolution; CLIP green → confirm as clip_green.
    diags = [_diag("1984", "Stop Stanford", 0.90, 0.20)]
    auto, yellow = pc.classify_crops(diags, {}, gemini_ok=True, job_id="j")
    assert len(auto) == 1 and yellow == []
    assert auto[0]["source"] == "clip_green"
    assert auto[0]["year"] == "1984"
    assert auto[0]["n"] == 1 and auto[0]["crop_idx"] == 0


def test_gemini_slogan_lower_rank_confirms():
    # CLIP top-1 is not green, but Gemini confirmed this crop (res.auto, e.g. its
    # slogan sat at a lower rank within the top-10) → confirm with the resolver's
    # year/slogan, not CLIP's top-1.
    diags = [_diag("1990", "Beat Pitt", 0.70, 0.02)]
    resolution = {0: {"year": "1984", "slogan": "Stop Stanford",
                      "source": "gemini_auto", "auto": True}}
    auto, yellow = pc.classify_crops(diags, resolution, gemini_ok=True, job_id="j")
    assert len(auto) == 1 and yellow == []
    assert auto[0]["source"] == "gemini_auto"
    assert auto[0]["year"] == "1984" and auto[0]["slogan"] == "Stop Stanford"


def test_gemini_low_confidence_ignored_not_yellow():
    # Gemini agreed but conf<0.70 / flagged → res.auto False; CLIP not green;
    # gemini_ok → IGNORE (never yellow when Gemini works).
    diags = [_diag("1990", "Beat Pitt", 0.70, 0.02)]
    resolution = {0: {"year": "1990", "slogan": "Beat Pitt",
                      "source": "gemini_auto", "auto": False}}
    auto, yellow = pc.classify_crops(diags, resolution, gemini_ok=True, job_id="j")
    assert auto == [] and yellow == []


def test_gemini_works_no_match_ignored():
    # No resolution for the crop and CLIP not green → ignore (no yellow).
    diags = [_diag("1990", "Beat Pitt", 0.72, 0.03)]
    auto, yellow = pc.classify_crops(diags, {}, gemini_ok=True, job_id="j")
    assert auto == [] and yellow == []


# --- Gemini fails: green→auto, yellow→ask, red→ignore -----------------------

def test_gemini_fails_yellow_and_ignore_and_green():
    diags = [
        _diag("1984", "Stop Stanford", 0.70, 0.02),  # >=RED, not green → yellow
        _diag("1990", "Beat Pitt",     0.50, 0.01),  # <RED            → ignore
        _diag("1995", "We Are",        0.90, 0.20),  # green           → confirm
    ]
    auto, yellow = pc.classify_crops(diags, {}, gemini_ok=False, job_id="job")
    assert [b["year"] for b in auto] == ["1995"]
    assert len(yellow) == 1
    assert yellow[0]["year"] == "1984"
    assert yellow[0]["overall"] == 0.70
    assert yellow[0]["check_id"] == "pipeline:job:0"


def test_gemini_fails_below_red_no_yellow():
    diags = [_diag("1990", "Beat Pitt", 0.60, 0.01)]
    auto, yellow = pc.classify_crops(diags, {}, gemini_ok=False, job_id="j")
    assert auto == [] and yellow == []


def test_green_autoconfirms_even_in_fallback():
    diags = [_diag("1984", "X", 0.88, 0.15)]   # >=AUTO threshold
    auto, yellow = pc.classify_crops(diags, {}, gemini_ok=False, job_id="j")
    assert len(auto) == 1 and yellow == []
    assert auto[0]["source"] == "clip_green"


def test_empty_diagnostics():
    auto, yellow = pc.classify_crops([], {}, gemini_ok=True, job_id="j")
    assert auto == [] and yellow == []


# --- lot_value_and_deal -----------------------------------------------------

_PRICES = {("1984", "Stop Stanford"): 30.0, ("1990", "Beat Pitt"): 12.0}


def _price_of(year, slogan):
    return _PRICES.get((year, slogan), 0.0)


def test_lot_value_sums_matched_prices():
    auto = [{"year": "1984", "slogan": "Stop Stanford"},
            {"year": "1990", "slogan": "Beat Pitt"}]
    value, undervalued, margin = pc.lot_value_and_deal(auto, _price_of, asking=25.0)
    assert value == 42.0
    assert undervalued is True
    assert margin == 17.0


def test_lot_value_not_undervalued_when_asking_exceeds_value():
    auto = [{"year": "1990", "slogan": "Beat Pitt"}]   # 12.0
    value, undervalued, margin = pc.lot_value_and_deal(auto, _price_of, asking=25.0)
    assert value == 12.0 and undervalued is False and margin == -13.0


def test_lot_value_unpriced_buttons_count_zero():
    auto = [{"year": "2099", "slogan": "Unknown"}]      # no price rule
    value, undervalued, _ = pc.lot_value_and_deal(auto, _price_of, asking=5.0)
    assert value == 0.0 and undervalued is False


def test_lot_value_no_asking_never_undervalued():
    auto = [{"year": "1984", "slogan": "Stop Stanford"}]
    value, undervalued, _ = pc.lot_value_and_deal(auto, _price_of, asking=None)
    assert value == 30.0 and undervalued is False


# --- staging_candidates -----------------------------------------------------

def _hough(): return {"shape": "circle", "x": 1, "y": 1, "r": 5}          # real detection
def _synthetic(): return {"shape": "circle", "source": "gemini_recovered"}  # not real


def test_staging_includes_hough_gemini_confirmed_high_conf():
    auto = [{"crop_idx": 0, "year": "1984", "slogan": "X", "overall": 0.90}]
    circle_info = [_hough()]
    resolution  = {0: {"auto": True}}
    out = pc.staging_candidates(auto, circle_info, resolution, stage_conf=0.85)
    assert [b["crop_idx"] for b in out] == [0]


def test_staging_excludes_synthetic_crop():
    auto = [{"crop_idx": 0, "year": "1984", "slogan": "X", "overall": 0.95}]
    out = pc.staging_candidates(auto, [_synthetic()], {0: {"auto": True}}, stage_conf=0.85)
    assert out == []


def test_staging_excludes_when_gemini_not_confirmed():
    auto = [{"crop_idx": 0, "year": "1984", "slogan": "X", "overall": 0.95}]
    # res.auto False (clip-green confirmed), and also the no-resolution case.
    assert pc.staging_candidates(auto, [_hough()], {0: {"auto": False}}, 0.85) == []
    assert pc.staging_candidates(auto, [_hough()], {}, 0.85) == []


def test_staging_excludes_below_conf():
    auto = [{"crop_idx": 0, "year": "1984", "slogan": "X", "overall": 0.84}]
    out = pc.staging_candidates(auto, [_hough()], {0: {"auto": True}}, stage_conf=0.85)
    assert out == []


def test_staging_handles_missing_overall_and_bad_index():
    auto = [{"crop_idx": 0, "year": "1984", "slogan": "X", "overall": None},
            {"crop_idx": 9, "year": "1990", "slogan": "Y", "overall": 0.99}]
    out = pc.staging_candidates(auto, [_hough()], {0: {"auto": True}, 9: {"auto": True}}, 0.85)
    assert out == []   # first: no overall; second: crop_idx out of range
