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


def _expected_unified(img, text_sim, phrase):
    """Recompute the unified score the way build_leaderboard / score_slogans do:
    0.5/0.5 blend + near-certain-text boost + weak-text penalty + rarity bonus."""
    norm_text = _normalize(text_sim)
    o = 0.5 * img + 0.5 * norm_text
    if norm_text > 0.9:
        o += (norm_text - 0.9) * 2.5
    if norm_text < 0.3:
        o *= 0.7
    words = set(_tokenize(phrase)) - set(STOPWORDS)
    if words:
        o = min(1.0, o + min(0.04 * sum(_rarity(w) for w in words) / len(words), 0.04))
    return round(o, 5)


def test_overall_formula_matches_live_pipeline():
    # Mid-range text (norm 0.5 → no boost, no penalty) exercises the 0.5/0.5 blend.
    img, text_sim, phrase = 0.60, 0.25, "Unique Slogan Words"
    lb = ml.build_leaderboard(
        [text_sim], {"1980": img}, ["1980"], [phrase], ["Football"],
        normalize_fn=_normalize, tokenize_fn=_tokenize, rarity_fn=_rarity,
        stopwords=STOPWORDS,
    )
    assert math.isclose(lb[0]["overall"], _expected_unified(img, text_sim, phrase),
                        abs_tol=1e-6)


def test_overall_formula_applies_near_certain_text_boost():
    # text_sim 0.35 → norm 1.0 (>0.9) triggers the +(norm-0.9)*2.5 boost.
    img, text_sim, phrase = 0.40, 0.35, "Unique Slogan Words"
    lb = ml.build_leaderboard(
        [text_sim], {"1980": img}, ["1980"], [phrase], ["Football"],
        normalize_fn=_normalize, tokenize_fn=_tokenize, rarity_fn=_rarity,
        stopwords=STOPWORDS,
    )
    assert math.isclose(lb[0]["overall"], _expected_unified(img, text_sim, phrase),
                        abs_tol=1e-6)


def test_overall_formula_applies_weak_text_penalty():
    # text_sim 0.18 → norm 0.15 (<0.3) triggers the *0.7 penalty.
    img, text_sim, phrase = 0.50, 0.18, "Unique Slogan Words"
    lb = ml.build_leaderboard(
        [text_sim], {"1980": img}, ["1980"], [phrase], ["Football"],
        normalize_fn=_normalize, tokenize_fn=_tokenize, rarity_fn=_rarity,
        stopwords=STOPWORDS,
    )
    assert math.isclose(lb[0]["overall"], _expected_unified(img, text_sim, phrase),
                        abs_tol=1e-6)


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
    assert rec["typed_slogan"] is None        # default when not a typed path


def test_confirm_record_logs_typed_slogan():
    rec = ml.build_confirm_record(
        service="buttonmatcher", command="/sort", job_id=None, thread_ts="t",
        crop_num=3, check_id="k", user_id="u", chosen_year=1990,
        chosen_phrase="Panthers' Pittfall", chosen_type="Football",
        source="typed_search", rank_restricted=None, rank_shadow=2,
        shadow_leaderboard_size=40, typed_slogan="panthers pitfall",
    )
    assert rec["typed_slogan"] == "panthers pitfall"
    assert rec["source"] == "typed_search"
    assert "typed_slogan" in ml.CONFIRM_HEADER
    row = ml.flatten_confirm_record(rec)
    assert len(row) == len(ml.CONFIRM_HEADER)
    assert row[ml.CONFIRM_HEADER.index("typed_slogan")] == "panthers pitfall"


def test_confirm_record_preserves_originals_and_typed_top():
    # Bug 3: a typed-slogan round must NOT clobber the match-time top-10s. The
    # original restricted_top/shadow_top are preserved; typed results go to a
    # separate typed_top column.
    orig_restricted = [{"year": "1995", "phrase": "A", "overall": 0.8}]
    orig_shadow = [{"year": "1995", "phrase": "A", "overall": 0.7}]
    typed = [{"year": "1990", "phrase": "B", "overall": 0.9}]
    rec = ml.build_confirm_record(
        service="buttonmatcher", command="/sort", job_id=None, thread_ts="t",
        crop_num=3, check_id="k", user_id="u", chosen_year=1990,
        chosen_phrase="B", chosen_type="Football", source="typed_search",
        rank_restricted=4, rank_shadow=2, shadow_leaderboard_size=40,
        typed_slogan="b slogan", restricted_top=orig_restricted,
        shadow_top=orig_shadow, typed_top=typed,
    )
    assert rec["restricted_top"] == orig_restricted     # originals intact
    assert rec["shadow_top"] == orig_shadow
    assert rec["typed_top"] == typed
    assert "typed_top_json" in ml.CONFIRM_HEADER
    row = ml.flatten_confirm_record(rec)
    assert len(row) == len(ml.CONFIRM_HEADER)
    assert json.loads(row[ml.CONFIRM_HEADER.index("typed_top_json")])[0]["year"] == "1990"
    assert json.loads(row[ml.CONFIRM_HEADER.index("restricted_top_json")])[0]["year"] == "1995"


def test_confirm_record_typed_top_defaults_empty():
    rec = ml.build_confirm_record(
        service="s", command="/sort", job_id=None, thread_ts="t", crop_num=1,
        check_id="k", user_id="u", chosen_year=1990, chosen_phrase="X",
        chosen_type="Football", source="pick", rank_restricted=1, rank_shadow=1,
        shadow_leaderboard_size=10,
    )
    assert rec["typed_top"] == []


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


def test_localization_quality_fields_present_and_flattened():
    # The quality fields are joinable in the Sheet (not just the print line).
    for col in ("det_raw_hough", "det_circles_rejected", "det_rejection_rate",
                "det_radius_min", "det_radius_max", "det_radius_mean",
                "det_radius_std", "det_buttons_per_megapixel",
                "det_expected_radius", "det_mask_components",
                # Priority 5 (per-stage filter breakdown) + Priority 4 (whole-image)
                "det_border_removed", "det_fill_removed", "det_overlap_removed",
                "det_edge_density", "det_brightness_std"):
        assert col in ml.MATCH_HEADER, col

    diag = ml.build_detection_diag(
        h=600, w=800, bg_brightness=170.0, bg_is_white=True, mask_path="blue_only",
        hough_pass1_count=31, hough_retry_count=12, final_count_user=12,
        final_count_noinput=19, user_count=12, detector_used="hough", n_crops=12,
        raw_hough=31, circles_rejected=12, rejection_rate=0.387,
        radius_min=58, radius_max=64, radius_mean=61.2, radius_std=1.8,
        buttons_per_megapixel=25.0, expected_radius=61, mask_components=14,
        border_removed=3, fill_removed=7, overlap_removed=2,
        edge_density=0.12345, brightness_std=42.756,
    )
    assert diag["radius_std"] == 1.8
    assert diag["mask_components"] == 14
    assert diag["expected_radius"] == 61
    assert diag["border_removed"] == 3
    assert diag["fill_removed"] == 7
    assert diag["overlap_removed"] == 2
    assert diag["edge_density"] == 0.1235     # rounded to 4 dp
    assert diag["brightness_std"] == 42.76    # rounded to 2 dp

    rec = ml.build_match_record(
        service="s", command="/c", mode="inventory", job_id="j", thread_ts="t",
        channel_id="ch", user_id="u", crop_num=1, check_id="k", detection=diag,
        bank="mellon", restricted_top=[], shadow_top=[], shadow_enabled=True,
    )
    row = ml.flatten_match_record(rec)
    assert len(row) == len(ml.MATCH_HEADER)
    assert row[ml.MATCH_HEADER.index("det_radius_std")] == 1.8
    assert row[ml.MATCH_HEADER.index("det_mask_components")] == 14
    assert row[ml.MATCH_HEADER.index("det_rejection_rate")] == 0.387
    assert row[ml.MATCH_HEADER.index("det_border_removed")] == 3
    assert row[ml.MATCH_HEADER.index("det_overlap_removed")] == 2
    assert row[ml.MATCH_HEADER.index("det_edge_density")] == 0.1235
    assert row[ml.MATCH_HEADER.index("det_brightness_std")] == 42.76


def test_localization_quality_fields_default_blank():
    # Projection path / callers that don't supply them → blank cells, never crash.
    diag = ml.build_detection_diag(
        h=1, w=1, bg_brightness=10, bg_is_white=False, mask_path="blue_or_white",
        hough_pass1_count=0, hough_retry_count=None, final_count_user=0,
        final_count_noinput=None, user_count=None, detector_used="grid", n_crops=12,
        mask_components=7,   # still available on the projection path
    )
    assert diag["radius_std"] is None
    assert diag["raw_hough"] is None
    assert diag["mask_components"] == 7
    rec = ml.build_match_record(
        service="s", command="/c", mode="inventory", job_id="j", thread_ts="t",
        channel_id="ch", user_id="u", crop_num=1, check_id="k", detection=diag,
        bank="all", restricted_top=[], shadow_top=[], shadow_enabled=False,
    )
    row = ml.flatten_match_record(rec)
    assert row[ml.MATCH_HEADER.index("det_radius_std")] == ""
    assert row[ml.MATCH_HEADER.index("det_mask_components")] == 7


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


def test_extract_spreadsheet_key_from_url_and_bare():
    key = "1AbCdEf-GhIjKlMnOpQrStUvWxYz0123456789_ABC"
    url = f"https://docs.google.com/spreadsheets/d/{key}/edit#gid=0"
    assert ml._extract_spreadsheet_key(url) == key
    assert ml._extract_spreadsheet_key(f"  {key}\n") == key   # strips whitespace
    assert ml._extract_spreadsheet_key(key) == key
    assert ml._extract_spreadsheet_key("") == ""
    assert ml._extract_spreadsheet_key(None) == ""


def test_disabled_logger_warns_once(capsys=None):
    import io as _io
    import contextlib as _ctx
    logger = ml.SheetLogger(None, None, service="x")
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        logger.log_image_crops("j", [{"detection": {}}])
        logger.log_image_crops("j2", [{"detection": {}}])  # should NOT warn again
    out = buf.getvalue()
    assert out.count("logging is DISABLED") == 1   # warned exactly once


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


def test_confirm_record_logs_rank_image_only():
    # The apples-to-apples "before" baseline for the slogan-id experiment.
    assert "rank_image_only" in ml.CONFIRM_HEADER
    rec = ml.build_confirm_record(
        service="buttonmatcher", command="/sort", job_id="j", thread_ts="t",
        crop_num=1, check_id="k", user_id="u", chosen_year=1976,
        chosen_phrase="Batter The Bucks", chosen_type="Football", source="pick",
        rank_restricted=7, rank_shadow=15, shadow_leaderboard_size=54,
        rank_image_only=9,
    )
    assert rec["rank_image_only"] == 9
    row = ml.flatten_confirm_record(rec)
    assert len(row) == len(ml.CONFIRM_HEADER)
    assert row[ml.CONFIRM_HEADER.index("rank_image_only")] == 9


def test_confirm_record_rank_image_only_defaults_none():
    rec = ml.build_confirm_record(
        service="s", command="/c", job_id="j", thread_ts="t", crop_num=1,
        check_id="k", user_id="u", chosen_year=1990, chosen_phrase="X",
        chosen_type="Football", source="pick", rank_restricted=1, rank_shadow=1,
        shadow_leaderboard_size=10,
    )
    assert rec["rank_image_only"] is None
    row = ml.flatten_confirm_record(rec)
    assert row[ml.CONFIRM_HEADER.index("rank_image_only")] == ""  # None → blank


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


def test_dt_peak_columns_present_and_flattened():
    # Count-free over-merge signal (log_analysis.md gap 5): raw blob count +
    # summed per-blob DT-peak count, appended at the END of the header.
    assert ml.MATCH_HEADER[-4:] == ["det_mask_blobs_raw", "det_dt_peaks_total",
                                    "det_mask_coverage", "det_white_recovered"]

    diag = ml.build_detection_diag(
        h=600, w=800, bg_brightness=170.0, bg_is_white=True, mask_path="blue_only",
        hough_pass1_count=3, hough_retry_count=None, final_count_user=26,
        final_count_noinput=3, user_count=None, detector_used="grid", n_crops=26,
        mask_components=1, mask_blobs_raw=4, dt_peaks_total=24, mask_coverage=0.8931,
    )
    assert diag["mask_blobs_raw"] == 4
    assert diag["dt_peaks_total"] == 24
    assert diag["mask_coverage"] == 0.8931

    rec = ml.build_match_record(
        service="ebayscout", command="/crawl-pipeline", mode="pipeline", job_id="j",
        thread_ts=None, channel_id="ch", user_id=None, crop_num=1, check_id="k",
        detection=diag, bank=None, restricted_top=[], shadow_top=[],
        shadow_enabled=True,
    )
    row = ml.flatten_match_record(rec)
    assert len(row) == len(ml.MATCH_HEADER)
    assert row[ml.MATCH_HEADER.index("det_mask_blobs_raw")] == 4
    assert row[ml.MATCH_HEADER.index("det_dt_peaks_total")] == 24
    assert row[ml.MATCH_HEADER.index("det_mask_coverage")] == 0.8931


def test_dt_peak_columns_default_blank():
    # Callers that don't compute the DT signal (legacy /scout path) → blank cells.
    diag = ml.build_detection_diag(
        h=1, w=1, bg_brightness=10, bg_is_white=False, mask_path="blue_or_white",
        hough_pass1_count=0, hough_retry_count=None, final_count_user=1,
        final_count_noinput=None, user_count=None, detector_used="grid", n_crops=1,
    )
    assert diag["mask_blobs_raw"] is None
    assert diag["dt_peaks_total"] is None
    rec = ml.build_match_record(
        service="s", command="/c", mode="inventory", job_id="j", thread_ts="t",
        channel_id="ch", user_id="u", crop_num=1, check_id="k", detection=diag,
        bank="all", restricted_top=[], shadow_top=[], shadow_enabled=False,
    )
    row = ml.flatten_match_record(rec)
    assert row[ml.MATCH_HEADER.index("det_mask_blobs_raw")] == ""
    assert row[ml.MATCH_HEADER.index("det_dt_peaks_total")] == ""
    assert row[ml.MATCH_HEADER.index("det_mask_coverage")] == ""


def test_noinput_diag_flattens_ni_columns_on_pipeline_record():
    # Gap 1: the pipeline path now passes the unguided shadow diag; its fields
    # must land in the ni_* columns of the flattened row.
    diag = ml.build_detection_diag(
        h=600, w=800, bg_brightness=170.0, bg_is_white=True, mask_path="blue_only",
        hough_pass1_count=1, hough_retry_count=1, final_count_user=1,
        final_count_noinput=1, user_count=None, detector_used="hough", n_crops=1,
        noinput_diag={"conservative": 1, "standard": 1, "aggressive": 3,
                      "selected": 1, "confidence": 0.94, "pass_winner": "standard"},
        count_source="gemini", gemini_button_count=1,
    )
    rec = ml.build_match_record(
        service="ebayscout", command="daily-pipeline", mode="pipeline", job_id="j",
        thread_ts=None, channel_id="ch", user_id=None, crop_num=1, check_id="k",
        detection=diag, bank=None, restricted_top=[], shadow_top=[],
        shadow_enabled=True,
    )
    row = ml.flatten_match_record(rec)
    assert len(row) == len(ml.MATCH_HEADER)
    assert row[ml.MATCH_HEADER.index("ni_selected")] == 1
    assert row[ml.MATCH_HEADER.index("ni_confidence")] == 0.94
    assert row[ml.MATCH_HEADER.index("ni_pass_winner")] == "standard"
    assert row[ml.MATCH_HEADER.index("det_count_noinput")] == 1
