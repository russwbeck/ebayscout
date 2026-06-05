"""
Tests for ebayscout/match_logging.py — stdlib-only, so it runs anywhere.
Covers the ebayscout durability delta (per-event append + bounded rate-limit
retry) and the record builders / flatteners used by the scan + /crawl500.
"""

import sys, os
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import match_logging as mlog


class TestRateLimitRetry:
    def test_looks_rate_limited(self):
        assert mlog._looks_rate_limited(Exception("APIError 429 RESOURCE_EXHAUSTED"))
        assert mlog._looks_rate_limited(Exception("Quota exceeded"))
        assert not mlog._looks_rate_limited(Exception("permission denied"))

    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("429 rate_limit")

        with patch("ebayscout.match_logging.time.sleep") as sl:
            mlog._append_with_retry(flaky, retries=4, base_delay=0.01)
        assert calls["n"] == 3
        assert sl.call_count == 2   # slept before each retry

    def test_non_rate_error_raises_immediately(self):
        def boom():
            raise ValueError("bad row")
        with pytest.raises(ValueError):
            mlog._append_with_retry(boom, retries=4, base_delay=0.01)

    def test_gives_up_after_retries(self):
        def always():
            raise Exception("429")
        with patch("ebayscout.match_logging.time.sleep"):
            with pytest.raises(Exception):
                mlog._append_with_retry(always, retries=2, base_delay=0.01)


class TestSheetLoggerPerEvent:
    def test_log_image_crops_appends_once_per_image(self):
        ws = MagicMock()
        logger = mlog.SheetLogger(ws, MagicMock(), service="ebayscout")
        recs = [_match_rec(1), _match_rec(2)]
        logger.log_image_crops("job1", recs)
        ws.append_rows.assert_called_once()
        rows = ws.append_rows.call_args[0][0]
        assert len(rows) == 2     # one row per crop, batched into one write

    def test_log_image_crops_retries_on_rate_limit(self):
        ws = MagicMock()
        ws.append_rows.side_effect = [Exception("429"), None]
        logger = mlog.SheetLogger(ws, None, service="ebayscout")
        with patch("ebayscout.match_logging.time.sleep"):
            logger.log_image_crops("job1", [_match_rec(1)])
        assert ws.append_rows.call_count == 2

    def test_disabled_logger_is_safe(self):
        logger = mlog.SheetLogger(None, None, service="ebayscout")
        logger.log_image_crops("job", [_match_rec(1)])   # no raise
        logger.log_confirmation("c", _confirm_rec())      # no raise


def _match_rec(n):
    det = mlog.build_detection_diag(
        h=100, w=100, bg_brightness=200.0, bg_is_white=True, mask_path="blue_only",
        hough_pass1_count=5, hough_retry_count=None, final_count_user=3,
        final_count_noinput=None, user_count=None, detector_used="hough", n_crops=3)
    return mlog.build_match_record(
        service="ebayscout", command="/crawl500", mode="crawl500", job_id="job1",
        thread_ts=None, channel_id="C1", user_id=None, crop_num=n, check_id=f"c{n}",
        detection=det, bank="Mellon",
        restricted_top=[{"year": "1990", "phrase": "No Bull", "overall": 0.9}],
        shadow_top=[{"year": "1990", "phrase": "No Bull", "overall": 0.9}],
        shadow_enabled=True)


def _confirm_rec():
    return mlog.build_confirm_record(
        service="ebayscout", command="/crawl500", job_id="job1", thread_ts=None,
        crop_num=1, check_id="c1", user_id=None, chosen_year="1990",
        chosen_phrase="No Bull", chosen_type="Football", source="green",
        rank_restricted=1, rank_shadow=1, shadow_leaderboard_size=40)


class TestFlatteners:
    def test_match_row_width_matches_header(self):
        row = mlog.flatten_match_record(_match_rec(1))
        assert len(row) == len(mlog.MATCH_HEADER)

    def test_confirm_row_width_matches_header(self):
        row = mlog.flatten_confirm_record(_confirm_rec())
        assert len(row) == len(mlog.CONFIRM_HEADER)
