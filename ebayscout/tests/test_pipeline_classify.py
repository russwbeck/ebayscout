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


def test_staging_keeps_modest_clip_when_gemini_agrees():
    # New save policy: agreement is the gate, not the CLIP score. A Gemini-agreed
    # real-Hough crop with a modest score (below the OLD 0.85 gate) is now KEPT —
    # these low-CLIP-but-agreed crops are the most valuable new references.
    auto = [{"crop_idx": 0, "year": "1984", "slogan": "X", "overall": 0.70}]
    out = pc.staging_candidates(auto, [_hough()], {0: {"auto": True}}, stage_conf=0.50)
    assert [b["crop_idx"] for b in out] == [0]


def test_staging_excludes_only_junk_below_sanity_floor():
    # The remaining score check is only a tiny junk floor (~0.5), not a gate.
    junk = [{"crop_idx": 0, "year": "1984", "slogan": "X", "overall": 0.40}]
    assert pc.staging_candidates(junk, [_hough()], {0: {"auto": True}}, stage_conf=0.50) == []


def test_staging_handles_missing_overall_and_bad_index():
    auto = [{"crop_idx": 0, "year": "1984", "slogan": "X", "overall": None},
            {"crop_idx": 9, "year": "1990", "slogan": "Y", "overall": 0.99}]
    out = pc.staging_candidates(auto, [_hough()], {0: {"auto": True}, 9: {"auto": True}}, 0.85)
    assert out == []   # first: no overall; second: crop_idx out of range


# --- DB-direct agreement tier ----------------------------------------------
# The candidate pool is YEAR-FOLDED (one slogan per candidate year), so a slogan
# shadowed by a sibling of its OWN year is absent at every depth. These tests
# pin the un-shadowing behaviour and its guards.

def _nk(s):
    """normalize.normalize_key, inlined so this file stays dependency-free."""
    import re
    return re.sub(r"[^\w]", "", str(s).lower())


_DB_ROWS = [
    (1995, "Michigan Impossible", "Football"),
    (1995, "I-owa Doubt It", "Football"),
    (2005, "I-owa Doubt It", "Football"),
    (1984, "Stop Stanford", "Football"),
]


def test_db_direct_appends_shadowed_slogan():
    # CLIP's 1995 row is the sibling that outscored the pun; the pun itself is
    # nowhere in the pool, so the resolver could never agree with it.
    pool = [{"year": 1995, "slogan": "Michigan Impossible", "overall": 0.72}]
    out = pc.gemini_db_candidates("I-owa Doubt It", 0.93, _DB_ROWS, _nk, pool)
    assert [(r["year"], r["slogan"]) for r in out] == [
        (1995, "I-owa Doubt It"), (2005, "I-owa Doubt It")]
    assert all(r["db_direct"] and r["overall"] is None for r in out)


def test_db_direct_dedups_against_existing_pool():
    pool = [{"year": 1995, "slogan": "I-Owa  Doubt-It!", "overall": 0.61}]
    out = pc.gemini_db_candidates("I-owa Doubt It", 0.93, _DB_ROWS, _nk, pool)
    assert [(r["year"], r["slogan"]) for r in out] == [(2005, "I-owa Doubt It")]


def test_db_direct_refuses_low_confidence():
    # Stricter than the resolver's 0.70 gate — this tier has no CLIP rank behind it.
    assert pc.gemini_db_candidates("I-owa Doubt It", 0.80, _DB_ROWS, _nk, []) == []
    assert pc.gemini_db_candidates("I-owa Doubt It", 0.85, _DB_ROWS, _nk, []) != []


def test_db_direct_missing_confidence_fails_open():
    assert pc.gemini_db_candidates("Stop Stanford", None, _DB_ROWS, _nk, []) != []


def test_db_direct_ignores_unknown_and_empty_slogans():
    assert pc.gemini_db_candidates("Beat Nobody", 0.99, _DB_ROWS, _nk, []) == []
    assert pc.gemini_db_candidates("", 0.99, _DB_ROWS, _nk, []) == []
    assert pc.gemini_db_candidates("!!!", 0.99, _DB_ROWS, _nk, []) == []


def test_db_direct_survives_malformed_rows():
    rows = [None, (1995, None, "Football"), (1995, "I-owa Doubt It", "Football")]
    out = pc.gemini_db_candidates("I-owa Doubt It", 0.93, rows, _nk, [])
    assert [(r["year"], r["slogan"]) for r in out] == [(1995, "I-owa Doubt It")]


# --- staging funnel telemetry ----------------------------------------------

def test_staging_funnel_counts_each_drop_reason():
    auto = [
        {"crop_idx": 0, "overall": 0.90},   # stageable
        {"crop_idx": 1, "overall": 0.90},   # synthetic box
        {"crop_idx": 2, "overall": 0.90},   # CLIP-green only, no resolution
        {"crop_idx": 3, "overall": 0.90},   # resolved but not auto
        {"crop_idx": 4, "overall": 0.20},   # below the junk floor
        {"crop_idx": 99, "overall": 0.90},  # no circle_info entry
    ]
    circle_info = [_hough(), _synthetic(), _hough(), _hough(), _hough()]
    resolution = {0: {"auto": True}, 1: {"auto": True}, 3: {"auto": False},
                  4: {"auto": True}, "telemetry": {"n_resolved": 4}}
    f = pc.staging_funnel(6, auto, circle_info, resolution, stage_conf=0.50)
    assert f["crops"] == 6
    assert f["resolved"] == 4          # the "telemetry" string key is not a crop
    assert f["gemini_auto"] == 3
    assert f["auto_confirmed"] == 6
    assert f["stageable"] == 1
    assert f["drop_synthetic"] == 1
    assert f["drop_no_resolution"] == 1
    assert f["drop_not_auto"] == 1
    assert f["drop_below_conf"] == 1
    assert f["drop_no_geometry"] == 1


def test_staging_funnel_stageable_matches_staging_candidates():
    auto = [{"crop_idx": 0, "year": "1984", "slogan": "X", "overall": 0.70},
            {"crop_idx": 1, "year": "1995", "slogan": "Y", "overall": 0.70}]
    circle_info = [_hough(), _synthetic()]
    resolution  = {0: {"auto": True}, 1: {"auto": True}}
    f = pc.staging_funnel(2, auto, circle_info, resolution, stage_conf=0.50)
    assert f["stageable"] == len(
        pc.staging_candidates(auto, circle_info, resolution, 0.50))


def test_staging_funnel_empty_lot():
    f = pc.staging_funnel(0, [], [], {}, stage_conf=0.50)
    assert f["crops"] == 0 and f["stageable"] == 0 and f["resolved"] == 0


# --- DB-direct never auto-stages -------------------------------------------
# ebayscout stages with no human in the loop, so a match that rests on Gemini's
# read alone (a DB-direct row CLIP never ranked) may price a lot but must never
# write into the shared reference library.

def _auto1(overall=0.90):
    return [{"crop_idx": 0, "year": "1995", "slogan": "I-owa Doubt It",
             "overall": overall}]


def test_staging_refuses_db_direct_on_every_rung():
    for src in ("gemini_auto", "gemini_printed_year", "gemini_majority",
                "gemini_clip_fallback"):
        res = {0: {"auto": True, "db_direct": True, "source": src}}
        assert pc.staging_candidates(_auto1(), [_hough()], res, 0.50) == [], src
        f = pc.staging_funnel(1, _auto1(), [_hough()], res, 0.50)
        assert f["drop_db_direct"] == 1 and f["stageable"] == 0, src


def test_staging_unchanged_when_clip_ranked_the_match():
    # The pre-existing bar: CLIP ranked it AND Gemini agreed. Untouched.
    for src in ("gemini_auto", "gemini_majority", "gemini_clip_fallback"):
        res = {0: {"auto": True, "db_direct": False, "source": src}}
        assert len(pc.staging_candidates(_auto1(), [_hough()], res, 0.50)) == 1, src


def test_staging_unaffected_for_resolutions_without_db_direct_key():
    res = {0: {"auto": True, "source": "gemini_clip_fallback"}}
    assert len(pc.staging_candidates(_auto1(), [_hough()], res, 0.50)) == 1


def test_db_direct_still_auto_confirms_for_deal_detection():
    # The resolver improvement stands — the crop is matched and priced, it is
    # only barred from the reference library.
    diags = [{"candidates": [{"year": "1995", "slogan": "Michigan Impossible",
                              "overall": 0.72}], "gap": 0.11}]
    res = {0: {"auto": True, "db_direct": True, "source": "gemini_auto",
               "year": 1995, "slogan": "I-owa Doubt It"}}
    auto, _ = pc.classify_crops(diags, res, gemini_ok=True, job_id="j")
    assert len(auto) == 1 and auto[0]["slogan"] == "I-owa Doubt It"
    assert pc.staging_candidates(auto, [_hough()], res, 0.50) == []


# --- STOP list: the one per-slogan gate on ebayscout staging ----------------

def test_parse_staging_policy_reads_buttonmatcher_format():
    assert pc.parse_staging_policy({"stopped": ["sl_1", "sl_2"]}) == {"sl_1", "sl_2"}
    assert pc.parse_staging_policy({"stopped": [7, "_year_1984"]}) == {"7", "_year_1984"}


def test_parse_staging_policy_empty_and_junk():
    # No stops declared yet — every shape of "nothing" means stage everything.
    assert pc.parse_staging_policy({}) == set()
    assert pc.parse_staging_policy(None) == set()
    assert pc.parse_staging_policy({"stopped": []}) == set()
    # Junk must not raise; the CALLER distinguishes unreadable from empty.
    assert pc.parse_staging_policy({"stopped": 5}) == set()
    assert pc.parse_staging_policy("not a dict") == set()


def _crop(entry_id, slogan="X"):
    return {"gcs_name": f"tmp/{slogan}.jpg", "year": "1984",
            "slogan": slogan, "entry_id": entry_id}


def test_filter_stopped_crops_drops_only_stopped_entries():
    crops = [_crop("sl_1"), _crop("sl_2"), _crop("_year_1984")]
    kept, dropped = pc.filter_stopped_crops(crops, {"sl_2"})
    assert [c["entry_id"] for c in kept] == ["sl_1", "_year_1984"]
    assert [c["entry_id"] for c in dropped] == ["sl_2"]


def test_filter_stopped_crops_no_stops_keeps_everything():
    crops = [_crop("sl_1"), _crop("sl_2")]
    assert pc.filter_stopped_crops(crops, set())[0] == crops
    assert pc.filter_stopped_crops(crops, None)[0] == crops
    assert pc.filter_stopped_crops(crops, None)[1] == []


def test_filter_stopped_crops_handles_empty_and_missing_entry_id():
    assert pc.filter_stopped_crops([], {"sl_1"}) == ([], [])
    assert pc.filter_stopped_crops(None, {"sl_1"}) == ([], [])
    # A crop with no entry_id is not stopped; promote drops it separately.
    kept, dropped = pc.filter_stopped_crops([{"gcs_name": "t.jpg"}], {"sl_1"})
    assert len(kept) == 1 and dropped == []


def test_filter_stopped_crops_stops_every_crop_of_one_slogan():
    # Several crops of the same finished slogan in one lot — all refused.
    crops = [_crop("sl_9"), _crop("sl_9"), _crop("sl_3")]
    kept, dropped = pc.filter_stopped_crops(crops, {"sl_9"})
    assert len(kept) == 1 and len(dropped) == 2
