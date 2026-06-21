"""
Tests for ebayscout/seen_items.py
"""

import json
import pytest
from unittest.mock import patch, MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import seen_items


class TestLoadSeen:
    def test_returns_empty_dict_when_blob_missing(self):
        mock_blob = MagicMock()
        mock_blob.exists.return_value = False
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        with patch("ebayscout.seen_items.storage.Client", return_value=mock_client):
            result = seen_items.load_seen("fake-bucket")

        assert result == {}

    def test_returns_parsed_dict_when_blob_exists(self):
        existing = {"item_001": "2025-01-01", "item_002": "2025-01-02"}
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.return_value = json.dumps(existing)
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        with patch("ebayscout.seen_items.storage.Client", return_value=mock_client):
            result = seen_items.load_seen("fake-bucket")

        assert result == existing

    def test_returns_empty_dict_on_exception(self):
        with patch("ebayscout.seen_items.storage.Client", side_effect=Exception("GCS down")):
            result = seen_items.load_seen("fake-bucket")
        assert result == {}


class TestSaveSeen:
    def test_uploads_json_to_gcs(self):
        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        data = {"item_123": "2025-05-26"}

        with patch("ebayscout.seen_items.storage.Client", return_value=mock_client):
            result = seen_items.save_seen(data, "fake-bucket")

        assert result is True
        mock_blob.upload_from_string.assert_called_once()
        call_args = mock_blob.upload_from_string.call_args
        uploaded  = json.loads(call_args[0][0])
        assert uploaded == data

    def test_returns_false_on_exception(self):
        with patch("ebayscout.seen_items.storage.Client", side_effect=Exception("GCS error")):
            result = seen_items.save_seen({}, "fake-bucket")
        assert result is False


class TestIsNew:
    def test_returns_true_for_unknown_id(self):
        assert seen_items.is_new("new_id", {"old_id": "2025-01-01"}) is True

    def test_returns_false_for_known_id(self):
        assert seen_items.is_new("old_id", {"old_id": "2025-01-01"}) is False


class TestMarkSeen:
    def test_adds_item_with_today_date(self):
        seen: dict = {}
        seen_items.mark_seen("item_abc", seen)
        assert "item_abc" in seen
        from datetime import date
        val = seen["item_abc"]
        assert isinstance(val, str)
        date.fromisoformat(val)

    def test_uses_provided_date(self):
        seen: dict = {}
        seen_items.mark_seen("item_xyz", seen, date_str="2024-12-25")
        assert seen["item_xyz"] == "2024-12-25"

    def test_second_mark_promotes_to_list(self):
        seen: dict = {"item_1": "2026-05-01"}
        seen_items.mark_seen("item_1", seen, date_str="2026-06-22")
        assert seen["item_1"] == ["2026-05-01", "2026-06-22"]

    def test_third_mark_appends(self):
        seen: dict = {"item_1": ["2026-05-01", "2026-06-22"]}
        seen_items.mark_seen("item_1", seen, date_str="2026-07-01")
        assert seen["item_1"] == ["2026-05-01", "2026-06-22", "2026-07-01"]

    def test_round_trip(self):
        """load → mark → save → load should preserve state."""
        initial_data = {"existing_item": "2025-01-01"}
        json_payload = [json.dumps(initial_data)]  # mutable for closure

        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.side_effect = lambda: json_payload[0]
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        def capture_upload(content, **kwargs):
            json_payload[0] = content

        mock_blob.upload_from_string.side_effect = capture_upload

        with patch("ebayscout.seen_items.storage.Client", return_value=mock_client):
            seen = seen_items.load_seen("bucket")
            seen_items.mark_seen("new_item", seen, date_str="2025-05-26")
            seen_items.save_seen(seen, "bucket")
            reloaded = seen_items.load_seen("bucket")

        assert reloaded["existing_item"] == "2025-01-01"
        assert reloaded["new_item"] == "2025-05-26"

    def test_round_trip_with_list_values(self):
        """Lists survive JSON round-trip through GCS."""
        initial_data = {"item_1": ["2026-05-01", "2026-06-22"]}
        json_payload = [json.dumps(initial_data)]

        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.side_effect = lambda: json_payload[0]
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        def capture_upload(content, **kwargs):
            json_payload[0] = content

        mock_blob.upload_from_string.side_effect = capture_upload

        with patch("ebayscout.seen_items.storage.Client", return_value=mock_client):
            seen = seen_items.load_seen("bucket")
            seen_items.save_seen(seen, "bucket")
            reloaded = seen_items.load_seen("bucket")

        assert reloaded["item_1"] == ["2026-05-01", "2026-06-22"]


class TestSeenCount:
    def test_absent(self):
        assert seen_items.seen_count("missing", {}) == 0

    def test_string_value(self):
        assert seen_items.seen_count("x", {"x": "2026-01-01"}) == 1

    def test_list_value(self):
        assert seen_items.seen_count("x", {"x": ["2026-01-01", "2026-06-22"]}) == 2


class TestFirstSeenDate:
    def test_absent(self):
        assert seen_items.first_seen_date("missing", {}) is None

    def test_string_value(self):
        assert seen_items.first_seen_date("x", {"x": "2026-05-01"}) == "2026-05-01"

    def test_list_value(self):
        assert seen_items.first_seen_date("x", {"x": ["2026-05-01", "2026-06-22"]}) == "2026-05-01"


class TestIsCrawlUnseen:
    CUTOFF = "2026-06-21"

    def test_never_seen(self):
        assert seen_items.is_crawl_unseen("new", {}, cutoff=self.CUTOFF) is True

    def test_pre_cutoff_seen_once(self):
        seen = {"old": "2026-05-15"}
        assert seen_items.is_crawl_unseen("old", seen, cutoff=self.CUTOFF) is True

    def test_pre_cutoff_seen_twice(self):
        seen = {"old": ["2026-05-15", "2026-06-22"]}
        assert seen_items.is_crawl_unseen("old", seen, cutoff=self.CUTOFF) is False

    def test_post_cutoff_seen_once(self):
        seen = {"new": "2026-06-22"}
        assert seen_items.is_crawl_unseen("new", seen, cutoff=self.CUTOFF) is False

    def test_on_cutoff_date_seen_once(self):
        seen = {"edge": "2026-06-21"}
        assert seen_items.is_crawl_unseen("edge", seen, cutoff=self.CUTOFF) is False

    def test_is_new_unaffected_by_list_values(self):
        seen = {"item": ["2026-05-01", "2026-06-22"]}
        assert seen_items.is_new("item", seen) is False
        assert seen_items.is_new("other", seen) is True
