"""
Tests for the on-demand2 /crawl500 search: the OR-expanded query list (pure,
runs anywhere) and the GCS first-run-state helpers (mocked storage; CI only,
since seen_items imports google.cloud.storage).
"""

import json
import sys, os
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import config


class TestCrawl500Queries:
    def test_or_expansion_covers_all_banks_and_types(self):
        q = config.CRAWL500_QUERIES
        assert len(q) == len(config.CRAWL500_BANKS) * len(config.BUTTON_TYPES)
        for bank in config.CRAWL500_BANKS:
            for btn in config.BUTTON_TYPES:
                assert f"Penn State {bank} {btn}" in q

    def test_includes_all_three_banks(self):
        assert set(config.CRAWL500_BANKS) == {"Citizens", "Mellon", "Central Counties"}

    def test_cap_is_500(self):
        assert config.CRAWL500_MAX_LOTS == 500

    def test_state_blob_path(self):
        assert config.ONDEMAND2_STATE_BLOB == "ebay_scout/ondemand2_state.json"


def _mock_blob(exists=True, text=""):
    blob = MagicMock()
    blob.exists.return_value = exists
    blob.download_as_text.return_value = text
    bucket = MagicMock(); bucket.blob.return_value = blob
    client = MagicMock(); client.bucket.return_value = bucket
    return client, blob


class TestOndemand2State:
    """seen_items imports google.cloud.storage; skipped where it isn't installed."""

    def setup_method(self):
        pytest.importorskip("google.cloud.storage")

    def test_first_run_when_marker_absent(self):
        from ebayscout import seen_items
        client, _ = _mock_blob(exists=False)
        with patch("ebayscout.seen_items.storage.Client", return_value=client):
            assert seen_items.ondemand2_first_run_done("b") is False

    def test_not_first_run_when_marker_true(self):
        from ebayscout import seen_items
        client, _ = _mock_blob(exists=True, text=json.dumps({"first_run_done": True}))
        with patch("ebayscout.seen_items.storage.Client", return_value=client):
            assert seen_items.ondemand2_first_run_done("b") is True

    def test_marker_write_payload(self):
        from ebayscout import seen_items
        client, blob = _mock_blob(exists=False)
        with patch("ebayscout.seen_items.storage.Client", return_value=client):
            assert seen_items.mark_ondemand2_first_run_done("b") is True
        written = blob.upload_from_string.call_args[0][0]
        assert json.loads(written)["first_run_done"] is True
