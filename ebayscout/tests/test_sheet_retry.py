"""Unit tests for sheet_retry — surviving the Sheets per-minute write quota.

The 2026-09-05 run lost five buttons to [429] Quota exceeded, so what counts as
"retry this" is pinned here rather than left to a substring check in main.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sheet_retry as sretry


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _APIError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        if status is not None:
            self.response = _Resp(status)


def test_the_status_code_settles_it_when_the_error_carries_one():
    assert sretry.is_rate_limited(_APIError("whatever", status=429)) is True
    assert sretry.is_rate_limited(_APIError("whatever", status=500)) is False


def test_the_real_message_from_the_live_failure_is_recognised():
    """Verbatim from the Bot Writes tab, 2026-09-06 02:43:48."""
    msg = ("APIError: [429]: Quota exceeded for quota metric 'Write requests' "
           "and limit 'Write requests per minute per user' of service "
           "'sheets.googleapis.com' for consumer 'project_number:404960106109'.")
    assert sretry.is_rate_limited(Exception(msg)) is True


def test_the_other_spellings_sheets_uses_are_recognised():
    assert sretry.is_rate_limited(Exception("RESOURCE_EXHAUSTED")) is True
    assert sretry.is_rate_limited(Exception("Quota exceeded")) is True


def test_errors_that_will_not_fix_themselves_are_not_retried():
    """A bad range or a revoked token fails again just as fast; sleeping on it
    only delays the failure and holds the sheet lock while it does."""
    for msg in ("APIError: [404]: Requested entity was not found",
                "APIError: [403]: The caller does not have permission",
                "APIError: [400]: Unable to parse range"):
        assert sretry.is_rate_limited(Exception(msg)) is False, msg


def test_a_status_code_of_none_falls_back_to_the_text():
    assert sretry.is_rate_limited(_APIError("[429] Quota exceeded")) is True


def test_the_schedule_ends_in_none_so_the_caller_knows_to_give_up():
    delays = list(sretry.retry_delays())
    assert delays[-1] is None
    assert all(isinstance(d, float) for d in delays[:-1])


def test_four_attempts_by_default_and_the_waits_grow():
    delays = list(sretry.retry_delays())
    assert len(delays) == 4                      # 1 try + 3 retries
    finite = [d for d in delays if d is not None]
    assert finite == sorted(finite) and finite[0] < finite[-1]


def test_the_whole_schedule_fits_inside_the_quota_window():
    """The limit is per MINUTE, so waiting longer than one buys nothing."""
    assert sum(d for d in sretry.retry_delays() if d is not None) < 60
