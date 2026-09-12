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


# --- the quota retry (2026-09-09) --------------------------------------------
# The Sheets write quota is per PROJECT, so the Logger competes with the
# inventory sheet for the same 60 writes/minute.  Its writes had no retry, so a
# batch that came back 429 was printed and dropped: 3 of the 196 lots logged
# lost their whole match_log batch that way.  A big lot is both the most likely
# to trip the quota and the most expensive to lose.

class _Flaky:
    """Fails the first ``n`` calls with ``exc``, then succeeds."""

    def __init__(self, n, exc):
        self.n, self.exc, self.calls, self.rows = n, exc, 0, []

    def _maybe_fail(self):
        self.calls += 1
        if self.calls <= self.n:
            raise self.exc

    def append_rows(self, rows, value_input_option=None):
        self._maybe_fail()
        self.rows.extend(rows)

    def append_row(self, row, value_input_option=None):
        self._maybe_fail()
        self.rows.append(row)


def _no_sleep(monkey=[]):
    """Retry delays are real seconds; tests must not actually wait 12 of them."""
    import time as _t
    orig = _t.sleep
    ml.time.sleep = lambda *_a, **_k: None
    return orig


def test_a_rate_limited_match_batch_is_retried_not_dropped():
    orig = _no_sleep()
    try:
        ws = _Flaky(2, Exception("APIError: [429]: Quota exceeded for quota "
                                 "metric 'Write requests'"))
        logger = ml.SheetLogger(ws, _FakeWS(), service="b")
        logger.log_image_crops("j", [{"detection": {}}])
        assert ws.calls == 3, f"gave up after {ws.calls} attempt(s)"
        assert len(ws.rows) == 1, "the batch was dropped instead of retried"
    finally:
        ml.time.sleep = orig


def test_a_rate_limited_confirm_is_retried_too():
    orig = _no_sleep()
    try:
        ws = _Flaky(1, Exception("[429] RESOURCE_EXHAUSTED"))
        logger = ml.SheetLogger(_FakeWS(), ws, service="b")
        logger.log_confirmation("k", {"ts": "x"})
        assert len(ws.rows) == 1
    finally:
        ml.time.sleep = orig


def test_only_the_quota_error_is_retried():
    """A deleted tab or a revoked token will fail again just as fast; sleeping
    on it only delays the report."""
    orig = _no_sleep()
    try:
        ws = _Flaky(99, RuntimeError("Worksheet not found"))
        logger = ml.SheetLogger(ws, _FakeWS(), service="b")
        logger.log_image_crops("j", [{"detection": {}}])
        assert ws.calls == 1, f"a non-quota error was retried {ws.calls} times"
    finally:
        ml.time.sleep = orig


def test_the_retry_gives_up_and_still_never_raises():
    orig = _no_sleep()
    try:
        ws = _Flaky(99, Exception("[429] Quota exceeded"))
        logger = ml.SheetLogger(ws, ws, service="b")
        logger.log_image_crops("j", [{"detection": {}}])   # must not raise
        logger.log_confirmation("k", {"ts": "x"})          # must not raise
        assert ws.rows == []
    finally:
        ml.time.sleep = orig


def test_the_retry_uses_the_one_shared_definition_of_a_rate_limit():
    """gspread has moved its error classes across versions; that judgement must
    not fork between the inventory write and the Logger."""
    import inspect
    src = inspect.getsource(ml.SheetLogger._append)
    assert "_retry.is_rate_limited(" in src
    assert "_retry.retry_delays()" in src


def test_the_first_write_banner_only_prints_on_a_write_that_landed():
    orig = _no_sleep()
    try:
        ws = _Flaky(99, Exception("[429] Quota exceeded"))
        logger = ml.SheetLogger(ws, _FakeWS(), service="b")
        logger.log_image_crops("j", [{"detection": {}}])
        assert logger._logged_first_write is False, (
            "a dropped batch announced itself as the first successful write")
    finally:
        ml.time.sleep = orig


def test_the_retries_are_serialized():
    """sheet_retry's delays are un-jittered on the promise that its callers
    cannot stampede each other; this module has to keep that promise."""
    logger = ml.SheetLogger(_FakeWS(), _FakeWS(), service="b")
    assert hasattr(logger, "_write_lock")


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
    # summed per-blob DT-peak count.  Position-robust to later appends
    # (det_gem_unmatched, det_n_swapped, … now follow).
    _i = ml.MATCH_HEADER.index("det_mask_blobs_raw")
    assert ml.MATCH_HEADER[_i:_i + 4] == ["det_mask_blobs_raw", "det_dt_peaks_total",
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


def test_gem_unmatched_columns_are_the_header_tail():
    # gem_unmatched → reconcile swap → Gemini-anchored A/B shadow → full-res
    # match shadow (Logger_19 A/B) → text-variant match shadow → within-year
    # scoring, the final appended column.
    # Still one contiguous run in this order, now with the FIVE columns
    # appended 2026-09-12 behind it (hence [-13:-5], not [-8:]).
    assert ml.MATCH_HEADER[-13:-5] == ["det_gem_unmatched", "det_gem_unmatched_json",
                                       "det_n_swapped", "det_reconcile_swaps_json",
                                       "det_gemini_anchored_json", "fullres_top_json",
                                       "variant_top_json", "within_year_json"]


def test_variant_top_column_flattens():
    import json
    rec = ml.build_match_record(
        service="buttonmatcher", command="/sort", mode="sort", job_id="j",
        thread_ts="t", channel_id="c", user_id="u", crop_num=1, check_id="",
        detection={}, bank="all",
        restricted_top=[{"year": "1980", "phrase": "Happy 125th"}],
        shadow_top=[], shadow_enabled=True,
        variant_top=[{"year": "1984", "phrase": "I-O-Was", "overall": 0.91}],
    )
    flat = ml.flatten_match_record(rec)
    _vt = ml.MATCH_HEADER.index("variant_top_json")
    assert len(flat) == len(ml.MATCH_HEADER)
    assert json.loads(flat[_vt])[0]["phrase"] == "I-O-Was"
    # defaults to empty JSON list when the shadow didn't run
    rec2 = ml.build_match_record(
        service="s", command="/sort", mode="sort", job_id="j", thread_ts="t",
        channel_id="c", user_id="u", crop_num=1, check_id="", detection={},
        bank="all", restricted_top=[], shadow_top=[], shadow_enabled=False,
    )
    assert ml.flatten_match_record(rec2)[_vt] == "[]"


def test_within_year_column_is_final_and_flattens():
    import json
    wy = {"year": 1992, "image_score": 0.961, "n_slogans": 12,
          "runner_up_margin": 0.0088, "winner_is_top1": True,
          "top": [{"phrase": "Penn State and Proud of it", "type": "Football",
                   "text_sim": 0.2814, "text_norm": 0.657},
                  {"phrase": "Eers to Penn State", "type": "Football",
                   "text_sim": 0.2812, "text_norm": 0.648}]}
    rec = ml.build_match_record(
        service="buttonmatcher", command="/sort", mode="sort", job_id="j",
        thread_ts="t", channel_id="c", user_id="u", crop_num=2, check_id="",
        detection={}, bank="mellon", restricted_top=[], shadow_top=[],
        shadow_enabled=True, within_year=wy,
    )
    flat = ml.flatten_match_record(rec)
    assert len(flat) == len(ml.MATCH_HEADER)
    assert ml.MATCH_HEADER[-6] == "within_year_json"   # five appended behind it
    got = json.loads(flat[ml.MATCH_HEADER.index("within_year_json")])
    # The losing same-year sibling is recorded even though no leaderboard
    # column can hold it — that is the point of this column.
    assert [r["phrase"] for r in got["top"]] == [
        "Penn State and Proud of it", "Eers to Penn State"]
    assert got["runner_up_margin"] == 0.0088


def test_within_year_defaults_to_empty_object():
    rec = ml.build_match_record(
        service="s", command="/sort", mode="sort", job_id="j", thread_ts="t",
        channel_id="c", user_id="u", crop_num=1, check_id="", detection={},
        bank="all", restricted_top=[], shadow_top=[], shadow_enabled=False,
    )
    _flat = ml.flatten_match_record(rec)
    assert _flat[ml.MATCH_HEADER.index("within_year_json")] == "{}"


def test_gemini_anchored_shadow_column_flattens():
    ga = {"n_gemini": 5, "n_agree": 4, "snap_px_median": 8.0, "snap_px_max": 13.0,
          "snap_frac_median": 0.1, "n_gemini_only": 1, "n_hough_only": 1}
    diag = ml.build_detection_diag(
        h=592, w=800, bg_brightness=120.0, bg_is_white=False, mask_path="blue_or_white",
        hough_pass1_count=5, hough_retry_count=None, final_count_user=5,
        final_count_noinput=2, user_count=5, detector_used="hough", n_crops=5,
        mask_components=4, gemini_anchored=ga,
    )
    assert diag["gemini_anchored"] == ga
    rec = ml.build_match_record(
        service="buttonmatcher", command="/pipeline", mode="pipeline", job_id="j",
        thread_ts=None, channel_id="ch", user_id=None, crop_num=1, check_id="k",
        detection=diag, bank=None, restricted_top=[], shadow_top=[], shadow_enabled=True,
    )
    row = ml.flatten_match_record(rec)
    assert len(row) == len(ml.MATCH_HEADER)
    cell = row[ml.MATCH_HEADER.index("det_gemini_anchored_json")]
    assert '"snap_frac_median": 0.1' in cell and '"n_hough_only": 1' in cell
    # default blank → empty object
    diag2 = ml.build_detection_diag(
        h=600, w=800, bg_brightness=170.0, bg_is_white=True, mask_path="blue_only",
        hough_pass1_count=3, hough_retry_count=None, final_count_user=3,
        final_count_noinput=3, user_count=3, detector_used="hough", n_crops=3,
        mask_components=3)
    rec2 = ml.build_match_record(
        service="buttonmatcher", command="/pipeline", mode="pipeline", job_id="j",
        thread_ts=None, channel_id="ch", user_id=None, crop_num=1, check_id="k",
        detection=diag2, bank=None, restricted_top=[], shadow_top=[], shadow_enabled=True)
    assert ml.flatten_match_record(rec2)[ml.MATCH_HEADER.index("det_gemini_anchored_json")] == "{}"


def test_reconcile_swap_columns_flatten():
    # n_swapped + the labeled phantom pool round-trip into the row at the tail.
    swaps = [{"slogan": "Too late Sooners", "confidence": 0.9,
              "phantom_x": 124, "phantom_y": 480, "phantom_r": 88, "phantom_fill": 0.0}]
    diag = ml.build_detection_diag(
        h=592, w=800, bg_brightness=120.0, bg_is_white=False, mask_path="blue_or_white",
        hough_pass1_count=5, hough_retry_count=None, final_count_user=5,
        final_count_noinput=2, user_count=5, detector_used="hough", n_crops=5,
        mask_components=4, n_swapped=1, reconcile_swaps=swaps,
    )
    assert diag["n_swapped"] == 1 and diag["reconcile_swaps"] == swaps
    rec = ml.build_match_record(
        service="buttonmatcher", command="/pipeline", mode="pipeline", job_id="j",
        thread_ts=None, channel_id="ch", user_id=None, crop_num=1, check_id="k",
        detection=diag, bank=None, restricted_top=[], shadow_top=[], shadow_enabled=True,
    )
    row = ml.flatten_match_record(rec)
    assert len(row) == len(ml.MATCH_HEADER)
    assert row[ml.MATCH_HEADER.index("det_n_swapped")] == 1
    assert "Too late Sooners" in row[ml.MATCH_HEADER.index("det_reconcile_swaps_json")]


def test_reconcile_swap_columns_default_blank():
    diag = ml.build_detection_diag(
        h=600, w=800, bg_brightness=170.0, bg_is_white=True, mask_path="blue_only",
        hough_pass1_count=3, hough_retry_count=None, final_count_user=3,
        final_count_noinput=3, user_count=3, detector_used="hough", n_crops=3,
        mask_components=3,
    )
    assert diag["n_swapped"] is None and diag["reconcile_swaps"] is None
    rec = ml.build_match_record(
        service="buttonmatcher", command="/pipeline", mode="pipeline", job_id="j",
        thread_ts=None, channel_id="ch", user_id=None, crop_num=1, check_id="k",
        detection=diag, bank=None, restricted_top=[], shadow_top=[], shadow_enabled=True,
    )
    row = ml.flatten_match_record(rec)
    assert row[ml.MATCH_HEADER.index("det_n_swapped")] == ""
    assert row[ml.MATCH_HEADER.index("det_reconcile_swaps_json")] == "[]"


def test_gem_unmatched_columns_flatten_known_count():
    diag = ml.build_detection_diag(
        h=600, w=800, bg_brightness=170.0, bg_is_white=True, mask_path="blue_only",
        hough_pass1_count=3, hough_retry_count=None, final_count_user=3,
        final_count_noinput=3, user_count=None, detector_used="hough", n_crops=3,
        gem_unmatched=1, gem_unmatched_indices=[2],
    )
    assert diag["gem_unmatched"] == 1
    assert diag["gem_unmatched_indices"] == [2]

    rec = ml.build_match_record(
        service="ebayscout", command="/crawl-pipeline", mode="pipeline", job_id="j",
        thread_ts=None, channel_id="ch", user_id=None, crop_num=1, check_id="k",
        detection=diag, bank=None, restricted_top=[], shadow_top=[],
        shadow_enabled=True,
    )
    row = ml.flatten_match_record(rec)
    assert len(row) == len(ml.MATCH_HEADER)
    assert row[ml.MATCH_HEADER.index("det_gem_unmatched")] == 1
    assert row[ml.MATCH_HEADER.index("det_gem_unmatched_json")] == json.dumps([2])


def test_gem_unmatched_columns_blank_when_unknown():
    # Match couldn't meaningfully run (no median radius / no Gemini coords) →
    # None, flattened as a BLANK cell — never mistaken for a real zero.
    diag = ml.build_detection_diag(
        h=1, w=1, bg_brightness=10, bg_is_white=False, mask_path="blue_or_white",
        hough_pass1_count=0, hough_retry_count=None, final_count_user=1,
        final_count_noinput=None, user_count=None, detector_used="grid", n_crops=1,
    )
    assert diag["gem_unmatched"] is None
    assert diag["gem_unmatched_indices"] is None
    rec = ml.build_match_record(
        service="s", command="/c", mode="inventory", job_id="j", thread_ts="t",
        channel_id="ch", user_id="u", crop_num=1, check_id="k", detection=diag,
        bank="all", restricted_top=[], shadow_top=[], shadow_enabled=False,
    )
    row = ml.flatten_match_record(rec)
    assert row[ml.MATCH_HEADER.index("det_gem_unmatched")] == ""
    assert row[ml.MATCH_HEADER.index("det_gem_unmatched_json")] == "[]"


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


def test_rank_centered_flattens_at_confirm_tail():
    """rank_centered (per-slogan baseline-centered shadow, Logger_14 layer 3)
    is the appended last confirm column; blank when unavailable."""
    rec = ml.build_confirm_record(
        service="buttonmatcher", command="/sort", job_id="j", thread_ts="t",
        crop_num=1, check_id="c", user_id="u", chosen_year="2008",
        chosen_phrase="I-O-Wasn't", chosen_type="Football", source="pick",
        rank_restricted=10, rank_shadow=10, shadow_leaderboard_size=100,
        rank_centered=2,
    )
    row = ml.flatten_confirm_record(rec)
    assert len(row) == len(ml.CONFIRM_HEADER)
    assert row[ml.CONFIRM_HEADER.index("rank_centered")] == 2
    # default None -> blank
    rec2 = ml.build_confirm_record(
        service="buttonmatcher", command="/sort", job_id="j", thread_ts="t",
        crop_num=1, check_id="c", user_id="u", chosen_year="2008",
        chosen_phrase="x", chosen_type="Football", source="pick",
        rank_restricted=None, rank_shadow=None, shadow_leaderboard_size=None,
    )
    row2 = ml.flatten_confirm_record(rec2)
    assert row2[ml.CONFIRM_HEADER.index("rank_centered")] == ""


def test_build_centered_leaderboard_subtracts_slogan_baseline():
    """A hot-embedding slogan (high baseline) must lose its de-facto advantage:
    raw sims tie, but the cold slogan's EVIDENCE (sim above its own baseline)
    is stronger, so centering flips the order."""
    kw = dict(normalize_fn=lambda x: max(0.0, min(1.0, (x - 0.15) / 0.20)),
              tokenize_fn=lambda t: t.lower().split(),
              rarity_fn=lambda w: 0.0, stopwords=set())
    years   = ["1995", "2008"]
    phrases = ["Hot Slogan", "Cold Pun"]
    types   = ["Football", "Football"]
    sims    = [0.28, 0.27]                     # raw: hot wins
    scores  = {"1995": 0.70, "2008": 0.70}     # image tied
    raw = ml.build_leaderboard(sims, scores, years, phrases, types, **kw)
    assert raw[0]["phrase"] == "Hot Slogan"
    centered = ml.build_centered_leaderboard(
        sims, scores, years, phrases, types, [0.30, 0.20], **kw)
    assert centered[0]["phrase"] == "Cold Pun"   # evidence -0.02 vs +0.07


def test_build_centered_leaderboard_requires_aligned_baselines():
    kw = dict(normalize_fn=lambda x: x, tokenize_fn=lambda t: [],
              rarity_fn=lambda w: 0.0, stopwords=set())
    assert ml.build_centered_leaderboard(
        [0.3], {"1995": 0.5}, ["1995"], ["p"], ["Football"], None, **kw) == []
    assert ml.build_centered_leaderboard(
        [0.3, 0.2], {"1995": 0.5}, ["1995", "1996"], ["p", "q"],
        ["Football", "Football"], [0.25], **kw) == []


def test_rank_of_slogan_fixes_year_collision_bug():
    """Logger_19 rank_restricted bug: a typed off-board slogan whose YEAR
    collides with the #1 candidate's year was logged rank 1 by the year-only
    rank_of.  rank_of_slogan matches the slogan identity (phrase+year), so it
    returns None for the off-board slogan and the true rank for on-board ones."""
    import match_logging as m
    board = [{"phrase": "All Helmet, No Heart", "year": "2024", "overall": 0.70},
             {"phrase": "Deflategate II", "year": "2021", "overall": 0.62},
             {"phrase": "We Owe One", "year": "2024", "overall": 0.56}]
    # the bug: rank_of (year only) falsely reports 1 for the off-board slogan
    assert m.rank_of("2024", board) == 1
    # the fix: slogan-aware → None (off board)
    assert m.rank_of_slogan("2024", "SM WHO", board) is None
    # on-board slogans get their true rank
    assert m.rank_of_slogan("2024", "All Helmet, No Heart", board) == 1
    assert m.rank_of_slogan("2024", "We Owe One", board) == 3       # year shared, slogan distinct
    # year-label variants ('1984' vs '1984 (Mellon)') still match by 4-digit run
    b2 = [{"phrase": "Turtle", "year": "1984 (Mellon)"}]
    assert m.rank_of_slogan("1984", "Turtle", b2) == 1
    # custom normalize_fn (punctuation/case folding) honored
    assert m.rank_of_slogan("2024", "sm-who!", [{"phrase": "SM WHO", "year": "2024"}]) == 1


def test_retired_shadows_keep_their_columns_and_write_empty():
    """A retired shadow gives up its computation, never its column.

    A1 (full-res match) and A12 (text-baseline centering) were refuted and
    switched off 2026-09-07.  Their columns MUST stay in place — the Progress
    Trackers workbook addresses match_log/confirm_log by letter and every
    pooled export was pasted under the current header, so a removal silently
    re-points the formulas in 69 front tabs.  With the producers off the cells
    simply carry the empty value they already carried whenever the shadow did
    not run.
    """
    # match_log is 92 columns since 2026-09-12 (five appended: four for B4/B2,
    # with fullres_top_json still exactly where it has always been — an append
    # must never shift a position, which is what these three indices pin.
    # (The workbook's pasted tab pads two helper cells after the header.)
    assert len(ml.MATCH_HEADER) == 92, len(ml.MATCH_HEADER)
    assert ml.MATCH_HEADER.index("fullres_top_json") == 84   # column CG
    assert ml.MATCH_HEADER.index("variant_top_json") == 85   # column CH
    assert ml.MATCH_HEADER.index("within_year_json") == 86   # column CI
    assert ml.MATCH_HEADER.index("restricted_top_json") == 51  # column AZ

    rec = ml.build_match_record(
        service="buttonmatcher", command="/sort", mode="sort", job_id="j",
        thread_ts="t", channel_id="c", user_id="u", crop_num=1, check_id="",
        detection={}, bank="all",
        restricted_top=[{"year": "1995", "phrase": "Hoo's Sorry Now"}],
        shadow_top=[], shadow_enabled=True,
        fullres_top=None,               # A1 retired — pass nothing
    )
    flat = ml.flatten_match_record(rec)
    assert len(flat) == len(ml.MATCH_HEADER)
    assert flat[ml.MATCH_HEADER.index("fullres_top_json")] == "[]"
    # the live board is untouched by the retirement
    assert "Hoo's Sorry Now" in flat[ml.MATCH_HEADER.index("restricted_top_json")]

    # confirm_log stays 23 columns, rank_centered last and blank when off
    assert len(ml.CONFIRM_HEADER) == 23, len(ml.CONFIRM_HEADER)
    assert ml.CONFIRM_HEADER.index("rank_centered") == len(ml.CONFIRM_HEADER) - 1
    crec = ml.build_confirm_record(
        service="buttonmatcher", command="/sort", job_id="j", thread_ts="t",
        crop_num=1, check_id="", user_id="u", chosen_year="1995",
        chosen_phrase="Hoo's Sorry Now", chosen_type="Football",
        typed_slogan="", source="pick", rank_restricted=1, rank_shadow=1,
        shadow_leaderboard_size=40,
        rank_centered=ml.rank_of("1995", []),   # A12 retired — empty board
    )
    cflat = ml.flatten_confirm_record(crec)
    assert len(cflat) == len(ml.CONFIRM_HEADER)
    assert cflat[ml.CONFIRM_HEADER.index("rank_centered")] == ""
    assert cflat[ml.CONFIRM_HEADER.index("rank_restricted")] == 1


def test_stuck_front_columns_are_appended_and_flatten():
    """The four columns added 2026-09-12 for B4 and B2.

    Both fronts were blocked on their own instrument rather than on data: B4's
    gate named `det_overlap_removed`, which belongs to the GUIDED dedup and
    reads 0 however the unguided path behaves, and B2's next reading lived only
    in a Cloud Run stdout line. Appended at the END — the one structural change
    the schema allows (LOGGING.md) — so every existing column keeps its
    position.
    """
    for col in ("det_unguided_band_removed", "det_unguided_concentric_removed",
                "det_satfb_blue_cov", "det_satfb_bright_cov"):
        assert col in ml.MATCH_HEADER, col

    # Appended, in the order hand-added to the Logger's header row (CJ..CM),
    # and nothing was inserted ahead of them.
    assert ml.MATCH_HEADER[-5:] == [
        "det_unguided_band_removed", "det_unguided_concentric_removed",
        "det_satfb_blue_cov", "det_satfb_bright_cov", "det_db_direct"]
    assert ml.MATCH_HEADER.index("within_year_json") == len(ml.MATCH_HEADER) - 6
    assert len(ml.MATCH_HEADER) == 92, len(ml.MATCH_HEADER)

    diag = ml.build_detection_diag(
        h=600, w=800, bg_brightness=170.0, bg_is_white=True,
        mask_path="blue_or_white", hough_pass1_count=9, hough_retry_count=None,
        final_count_user=4, final_count_noinput=5, user_count=None,
        detector_used="hough", n_crops=4,
        mask_coverage=0.83,
        satfb_blue_cov=0.041234, satfb_bright_cov=0.9105,
        noinput_diag={"conservative": 3, "standard": 5, "aggressive": 9,
                      "selected": 4, "band_removed": 1, "concentric_removed": 2},
    )
    assert diag["satfb_blue_cov"] == 0.0412          # 4 dp
    assert diag["satfb_bright_cov"] == 0.9105

    rec = ml.build_match_record(
        service="s", command="/c", mode="pipeline", job_id="j", thread_ts=None,
        channel_id="ch", user_id=None, crop_num=1, check_id="k", detection=diag,
        bank="mellon", restricted_top=[], shadow_top=[], shadow_enabled=True,
    )
    row = ml.flatten_match_record(rec)
    assert len(row) == len(ml.MATCH_HEADER)
    assert row[ml.MATCH_HEADER.index("det_unguided_band_removed")] == 1
    assert row[ml.MATCH_HEADER.index("det_unguided_concentric_removed")] == 2
    assert row[ml.MATCH_HEADER.index("det_satfb_blue_cov")] == 0.0412
    assert row[ml.MATCH_HEADER.index("det_satfb_bright_cov")] == 0.9105


def test_dedup_zero_is_a_reading_and_unreached_fork_is_blank():
    """0 removed must not render as blank.

    "Removes ZERO circles on exact-match lots" is half of B4's gate, so a lot
    where the dedup ran and removed nothing has to be distinguishable from a
    lot that never reported. Conversely the saturation fork is only reached on
    a flooded mask, so its two cells stay blank on the majority of lots — blank
    there means "not saturated", never "zero coverage".
    """
    diag = ml.build_detection_diag(
        h=600, w=800, bg_brightness=170.0, bg_is_white=True,
        mask_path="blue_only", hough_pass1_count=9, hough_retry_count=None,
        final_count_user=4, final_count_noinput=4, user_count=None,
        detector_used="hough", n_crops=4,
        noinput_diag={"selected": 4, "band_removed": 0, "concentric_removed": 0},
    )
    rec = ml.build_match_record(
        service="s", command="/c", mode="pipeline", job_id="j", thread_ts=None,
        channel_id="ch", user_id=None, crop_num=1, check_id="k", detection=diag,
        bank="mellon", restricted_top=[], shadow_top=[], shadow_enabled=True,
    )
    row = ml.flatten_match_record(rec)
    assert row[ml.MATCH_HEADER.index("det_unguided_band_removed")] == 0
    assert row[ml.MATCH_HEADER.index("det_unguided_concentric_removed")] == 0
    # the fork was never reached on this lot
    assert row[ml.MATCH_HEADER.index("det_satfb_blue_cov")] == ""
    assert row[ml.MATCH_HEADER.index("det_satfb_bright_cov")] == ""

    # and a caller that supplies neither still produces a full, blank-tailed row
    bare = ml.build_detection_diag(
        h=1, w=1, bg_brightness=10, bg_is_white=False, mask_path="blue_or_white",
        hough_pass1_count=0, hough_retry_count=None, final_count_user=1,
        final_count_noinput=0, user_count=None, detector_used="grid", n_crops=1,
    )
    brec = ml.build_match_record(
        service="s", command="/c", mode="pipeline", job_id="j", thread_ts=None,
        channel_id="ch", user_id=None, crop_num=1, check_id="k", detection=bare,
        bank="mellon", restricted_top=[], shadow_top=[], shadow_enabled=True,
    )
    brow = ml.flatten_match_record(brec)
    assert len(brow) == len(ml.MATCH_HEADER)
    assert brow[-5:] == ["", "", "", "", ""]


def test_db_direct_is_per_crop_and_blank_off_the_pipeline(self=None):
    """Front C7's column. Per CROP, not per lot.

    The mechanism only ever announced itself as `>>> PIPELINE DB_DIRECT:
    appended DB rows for 4/13 crop(s)` — a lot-level count that cannot say
    WHICH crops needed the rescue, printed and discarded. 92.9% of confirmed
    buttons share a year with a sibling in their own lot, which is the
    population a year-folded board can starve, so this is load-bearing rather
    than an edge case.

    Three states, all distinct: 1 rescued, 0 not rescued, blank not a pipeline
    crop.
    """
    def _rec(**kw):
        d = ml.build_detection_diag(
            h=1, w=1, bg_brightness=1, bg_is_white=False, mask_path="blue_only",
            hough_pass1_count=0, hough_retry_count=None, final_count_user=1,
            final_count_noinput=1, user_count=None, detector_used="hough",
            n_crops=1)
        return ml.flatten_match_record(ml.build_match_record(
            service="s", command="/c", mode="pipeline", job_id="j",
            thread_ts=None, channel_id="c", user_id=None, crop_num=1,
            check_id="k", detection=d, bank=None, restricted_top=[],
            shadow_top=[], shadow_enabled=True, **kw))

    i = ml.MATCH_HEADER.index("det_db_direct")
    assert _rec(db_direct=True)[i] == 1,  "rescued must read 1, not TRUE"
    assert _rec(db_direct=False)[i] == 0, "not-rescued is a reading, not a blank"
    assert _rec()[i] == "", "off the pipeline the tier does not exist — blank"
    # and it is the last column, so nothing shifted when it was appended
    assert i == len(ml.MATCH_HEADER) - 1
    for row in (_rec(db_direct=True), _rec()):
        assert len(row) == len(ml.MATCH_HEADER)
