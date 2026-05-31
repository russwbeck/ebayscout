"""Unit tests for match_logging — pure-python, no torch/cv2/gspread needed.

Run with the bundled harness (pytest may be unavailable in some envs):
    python tests/run_match_logging_tests.py
"""

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import match_logging as ml


# --- helpers mirroring the live scoring helpers -----------------------------

def _normalize(score, min_s=0.15, max_s=0.35):
    return max(0.0, min(1.0, (score - min_s) / (max_s - min_s)))


def _tokenize(text):
    import re
    return re.findall(r"[a-z0-9]+", str(text).lower())


def _rarity(word):
    return 1.0  # uniform rarity for deterministic tests


STOPWORDS = {"the", "a", "psu"}


def _fixture():
    text_phrases = ["Stop Stanford", "Stomp Ohio", "Beat Pitt"]
    text_years = ["1973", "1972", "1974"]
    text_types = ["Football", "Football", "Basketball"]
    text_sims = [0.20, 0.22, 0.34]
    year_scores = {"1972": 0.80, "1973": 0.70, "1974": 0.60}
    return text_sims, year_scores, text_years, text_phrases, text_types


def _lb(**kw):
    text_sims, year_scores, ty, tp, tt = _fixture()
    return ml.build_leaderboard(
        text_sims, year_scores, ty, tp, tt,
        normalize_fn=_normalize, tokenize_fn=_tokenize, rarity_fn=_rarity,
        stopwords=STOPWORDS, **kw,
    )


# --- build_leaderboard -------------------------------------------------------

def test_leaderboard_scores_every_year_and_sorts():
    lb = _lb()
    assert {r["year"] for r in lb} == {"1972", "1973", "1974"}
    overalls = [r["overall"] for r in lb]
    assert overalls == sorted(overalls, reverse=True)


def test_leaderboard_type_filter_excludes_basketball():
    lb = _lb(allowed_types={"Football"})
    years = {r["year"] for r in lb}
    assert "1974" not in years
    assert years == {"1972", "1973"}


def test_leaderboard_year_filter():
    lb = _lb(allowed_years={"1972"})
    assert [r["year"] for r in lb] == ["1972"]


def test_leaderboard_top_n_trims():
    assert len(_lb(top_n=1)) == 1


def test_leaderboard_matches_int_keyed_year_scores():
    # year_scores keyed by int should still join to string text years.
    lb = ml.build_leaderboard(
        [0.35], {1980: 1.0}, [1980], ["Unique Slogan Words"], ["Football"],
        normalize_fn=_normalize, tokenize_fn=_tokenize, rarity_fn=_rarity,
        stopwords=STOPWORDS,
    )
    assert lb[0]["image_score"] == 1.0


def test_overall_formula_matches_live_pipeline():
    lb = ml.build_leaderboard(
        [0.35], {"1980": 1.0}, ["1980"], ["Unique Slogan Words"], ["Football"],
        normalize_fn=_normalize, tokenize_fn=_tokenize, rarity_fn=_rarity,
        stopwords=STOPWORDS,
    )
    r = lb[0]
    norm_text = _normalize(0.35)  # == 1.0
    expected = min(1.0, 0.7 * 1.0 + 0.3 * norm_text + 0.04)
    assert math.isclose(r["overall"], round(expected, 5), abs_tol=1e-6)


# --- rank_of -----------------------------------------------------------------

def test_rank_of_finds_position():
    lb = [{"year": "1972"}, {"year": "1973"}, {"year": "1974"}]
    assert ml.rank_of("1972", lb) == 1
    assert ml.rank_of("1974", lb) == 3
    assert ml.rank_of(1973, lb) == 2
    assert ml.rank_of("1999", lb) is None


def test_rank_of_accepts_year_strings():
    assert ml.rank_of("b", ["a", "b", "c"]) == 2


# --- record builders ---------------------------------------------------------

def test_build_detection_diag_shapes_and_types():
    d = ml.build_detection_diag(
        h=100, w=200, bg_brightness=170.4, bg_is_white=True, mask_path="blue_only",
        hough_pass1_count=8, hough_retry_count=11, final_count_user=12,
        final_count_noinput=9, user_count="12", detector_used="hough", n_crops=12,
    )
    assert d["bg_is_white"] is True
    assert d["user_count"] == 12
    assert d["hough_retry_count"] == 11
    assert d["final_count_user"] == 12 and d["final_count_noinput"] == 9


def test_build_detection_diag_handles_none():
    d = ml.build_detection_diag(
        h=1, w=1, bg_brightness=10, bg_is_white=False, mask_path="blue_or_white",
        hough_pass1_count=0, hough_retry_count=None, final_count_user=0,
        final_count_noinput=None, user_count=None, detector_used="grid", n_crops=12,
    )
    assert d["user_count"] is None
    assert d["hough_retry_count"] is None
    assert d["final_count_noinput"] is None


def test_build_match_record_join_key_and_shadow_flag():
    diag = ml.build_detection_diag(
        h=1, w=1, bg_brightness=10, bg_is_white=False, mask_path="blue_only",
        hough_pass1_count=1, hough_retry_count=None, final_count_user=1,
        final_count_noinput=1, user_count=1, detector_used="hough", n_crops=1,
    )
    rec = ml.build_match_record(
        service="buttonmatcher", command="/inventory", mode="inventory",
        job_id="job1", thread_ts="t1", channel_id="c1", user_id="u1",
        crop_num=0, check_id="chk1", detection=diag, bank="mellon",
        restricted_top=[{"year": "1990"}], shadow_top=[{"year": "1990"}],
        shadow_enabled=True,
    )
    assert rec["schema"] == ml.SCHEMA_MATCH
    assert rec["check_id"] == "chk1"
    assert rec["shadow_enabled"] is True
    assert rec["bank"] == "mellon"


def test_build_confirm_record():
    rec = ml.build_confirm_record(
        service="buttonmatcher", command="/inventory", job_id="job1",
        thread_ts="t1", crop_num=0, check_id="chk1", user_id="u1",
        chosen_year=1990, chosen_phrase="X", chosen_type="Football",
        source="manual", rank_restricted=2, rank_shadow=5,
        shadow_leaderboard_size=54,
    )
    assert rec["schema"] == ml.SCHEMA_CONFIRM
    assert rec["chosen_year"] == "1990"
    assert rec["rank_shadow"] == 5


# --- flatteners --------------------------------------------------------------

def test_flatten_match_row_matches_header_width():
    diag = ml.build_detection_diag(
        h=10, w=20, bg_brightness=170.4, bg_is_white=True, mask_path="blue_only",
        hough_pass1_count=8, hough_retry_count=None, final_count_user=12,
        final_count_noinput=9, user_count=12, detector_used="hough", n_crops=12,
    )
    rec = ml.build_match_record(
        service="s", command="/c", mode="inventory", job_id="j", thread_ts="t",
        channel_id="ch", user_id="u", crop_num=1, check_id="k", detection=diag,
        bank="mellon", restricted_top=[{"year": "1990", "overall": 0.9}],
        shadow_top=[{"year": "1990"}], shadow_enabled=True,
    )
    row = ml.flatten_match_record(rec)
    assert len(row) == len(ml.MATCH_HEADER)
    # bools rendered as sheet-friendly text
    assert "TRUE" in row
    # restricted_top serialized to JSON in one cell
    assert json.loads(row[ml.MATCH_HEADER.index("restricted_top_json")])[0]["year"] == "1990"
    # None retry rendered as empty string
    assert row[ml.MATCH_HEADER.index("det_hough_retry")] == ""


def test_detection_diag_includes_bg_saturation():
    d = ml.build_detection_diag(
        h=10, w=20, bg_brightness=180.0, bg_saturation=22.5, bg_is_white=True,
        mask_path="blue_only", hough_pass1_count=8, hough_retry_count=None,
        final_count_user=12, final_count_noinput=9, user_count=12,
        detector_used="hough", n_crops=12,
    )
    assert d["bg_saturation"] == 22.5
    # saturation is optional — defaults to None when the sampler didn't report it
    d2 = ml.build_detection_diag(
        h=10, w=20, bg_brightness=180.0, bg_is_white=True, mask_path="blue_only",
        hough_pass1_count=8, hough_retry_count=None, final_count_user=12,
        final_count_noinput=9, user_count=12, detector_used="hough", n_crops=12,
    )
    assert d2["bg_saturation"] is None


def test_bg_saturation_column_present_and_flattened():
    assert "det_bg_saturation" in ml.MATCH_HEADER
    diag = ml.build_detection_diag(
        h=1, w=1, bg_brightness=180.0, bg_saturation=22.5, bg_is_white=True,
        mask_path="blue_only", hough_pass1_count=1, hough_retry_count=None,
        final_count_user=1, final_count_noinput=1, user_count=1,
        detector_used="hough", n_crops=1,
    )
    rec = ml.build_match_record(
        service="s", command="/c", mode="m", job_id="j", thread_ts="t",
        channel_id="c", user_id="u", crop_num=1, check_id="k", detection=diag,
        bank="all", restricted_top=[], shadow_top=[], shadow_enabled=True,
    )
    row = ml.flatten_match_record(rec)
    assert len(row) == len(ml.MATCH_HEADER)
    assert row[ml.MATCH_HEADER.index("det_bg_saturation")] == 22.5


def test_trim_top_defaults_to_ten():
    rows = [{"year": str(1900 + i), "overall": 1.0 - i * 0.01} for i in range(15)]
    assert len(ml.trim_top(rows)) == 10            # bulk slogan detection = top 10
    assert len(ml.trim_top(rows, 5)) == 5          # explicit n still honoured


def test_flatten_confirm_row_matches_header_width():
    rec = ml.build_confirm_record(
        service="s", command="/c", job_id="j", thread_ts="t", crop_num=1,
        check_id="k", user_id="u", chosen_year=1990, chosen_phrase="X",
        chosen_type="Football", source="pick", rank_restricted=1, rank_shadow=None,
        shadow_leaderboard_size=0,
    )
    row = ml.flatten_confirm_record(rec)
    assert len(row) == len(ml.CONFIRM_HEADER)
    assert row[ml.CONFIRM_HEADER.index("rank_shadow")] == ""  # None → ""


# --- SheetLogger -------------------------------------------------------------

class _FakeWS:
    def __init__(self):
        self.rows = []

    def append_rows(self, rows, value_input_option=None):
        self.rows.extend(rows)

    def append_row(self, row, value_input_option=None):
        self.rows.append(row)


def test_logger_batches_one_call_per_image():
    mws, cws = _FakeWS(), _FakeWS()
    logger = ml.SheetLogger(mws, cws, service="buttonmatcher")
    diag = ml.build_detection_diag(
        h=1, w=1, bg_brightness=1, bg_is_white=False, mask_path="blue_only",
        hough_pass1_count=1, hough_retry_count=None, final_count_user=2,
        final_count_noinput=2, user_count=2, detector_used="hough", n_crops=2,
    )
    recs = [
        ml.build_match_record(
            service="b", command="/inventory", mode="inventory", job_id="j",
            thread_ts="t", channel_id="c", user_id="u", crop_num=i, check_id=f"k{i}",
            detection=diag, bank="all", restricted_top=[], shadow_top=[],
            shadow_enabled=True,
        )
        for i in range(2)
    ]
    logger.log_image_crops("j", recs)
    assert len(mws.rows) == 2  # two crops, both written


def test_logger_confirmation_appends_one_row():
    mws, cws = _FakeWS(), _FakeWS()
    logger = ml.SheetLogger(mws, cws, service="b")
    rec = ml.build_confirm_record(
        service="b", command="/inventory", job_id="j", thread_ts="t", crop_num=1,
        check_id="k", user_id="u", chosen_year=1990, chosen_phrase="X",
        chosen_type="Football", source="pick", rank_restricted=1, rank_shadow=1,
        shadow_leaderboard_size=50,
    )
    logger.log_confirmation("k", rec)
    assert len(cws.rows) == 1


def test_logger_never_raises_on_ws_failure():
    class _Boom:
        def append_rows(self, *a, **k):
            raise RuntimeError("sheets down")

        def append_row(self, *a, **k):
            raise RuntimeError("sheets down")

    logger = ml.SheetLogger(_Boom(), _Boom(), service="b")
    logger.log_image_crops("j", [{"detection": {}}])     # must not raise
    logger.log_confirmation("k", {"ts": "x"})            # must not raise


def test_logger_disabled_when_ws_none():
    logger = ml.SheetLogger(None, None, service="b")
    assert logger.enabled is False
    logger.log_image_crops("j", [{"detection": {}}])     # no-op, no raise
    logger.log_confirmation("k", {})                     # no-op, no raise


def test_logger_empty_records_no_write():
    mws = _FakeWS()
    logger = ml.SheetLogger(mws, _FakeWS(), service="b")
    logger.log_image_crops("j", [])
    assert mws.rows == []


def test_shadow_pass_enabled_env():
    os.environ.pop("BUTTONMATCHER_SHADOW_PASS", None)
    assert ml.shadow_pass_enabled() is True
    os.environ["BUTTONMATCHER_SHADOW_PASS"] = "0"
    assert ml.shadow_pass_enabled() is False
    os.environ["BUTTONMATCHER_SHADOW_PASS"] = "1"
    assert ml.shadow_pass_enabled() is True
    os.environ.pop("BUTTONMATCHER_SHADOW_PASS", None)
