"""
Tests for ebayscout/ebay_client.py
"""

import json
import pytest
from unittest.mock import patch, MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import ebay_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_finding_response(items: list[dict], total_pages: int = 1):
    """Build a minimal eBay Finding API JSON response."""
    return {
        "findItemsAdvancedResponse": [{
            "ack": ["Success"],
            "paginationOutput": [{"totalPages": [str(total_pages)]}],
            "searchResult": [{
                "item": items
            }]
        }]
    }


def _make_item(item_id="123", title="Penn State Button", seller="testseller",
               price="5.00", listing_url="https://ebay.com/123",
               gallery_url="https://thumbs.ebay.com/123"):
    return {
        "itemId": [item_id],
        "title": [title],
        "sellerInfo": [{"sellerUserName": [seller]}],
        "sellingStatus": [{"currentPrice": [{"__value__": price, "@currencyId": "USD"}]}],
        "viewItemURL": [listing_url],
        "galleryURL": [gallery_url],
    }


# ---------------------------------------------------------------------------
# find_listings
# ---------------------------------------------------------------------------

class TestFindListings:
    def test_parses_item_fields_correctly(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_finding_response([
            _make_item("111", "Lot of PSU buttons", "seller_a", "10.00")
        ])

        with patch("ebayscout.ebay_client.requests.get", return_value=mock_resp):
            results = ebay_client.find_listings("FAKE_APP_ID", "PSU button", [])

        assert len(results) == 1
        r = results[0]
        assert r["item_id"] == "111"
        assert r["title"] == "Lot of PSU buttons"
        assert r["seller"] == "seller_a"
        assert r["current_price"] == 10.0
        assert r["currency"] == "USD"

    def test_deduplicates_within_query(self):
        """Same item_id appearing twice should only appear once in results."""
        item = _make_item("dupe_id")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_finding_response([item, item])

        with patch("ebayscout.ebay_client.requests.get", return_value=mock_resp):
            results = ebay_client.find_listings("APP", "PSU button", [])

        assert len(results) == 1
        assert results[0]["item_id"] == "dupe_id"

    def test_exclude_seller_param_sent(self):
        """ExcludeSeller filter parameters must be present in the request."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_finding_response([])

        with patch("ebayscout.ebay_client.requests.get", return_value=mock_resp) as mock_get:
            ebay_client.find_listings("APP", "keyword", ["badguy", "anotherbad"])

        call_kwargs = mock_get.call_args
        params = call_kwargs[1].get("params", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else {})
        # params may be a dict or the second positional arg
        if isinstance(call_kwargs, tuple):
            # check via the mock's call_args
            params = mock_get.call_args.kwargs.get("params") or mock_get.call_args.args[1] if len(mock_get.call_args.args) > 1 else mock_get.call_args.kwargs.get("params", {})

        # Reconstruct params from the actual call
        actual_params = mock_get.call_args[1].get("params", {})
        if not actual_params:
            actual_params = mock_get.call_args[0][1] if len(mock_get.call_args[0]) > 1 else {}

        assert "itemFilter(0).name" in actual_params
        assert actual_params["itemFilter(0).name"] == "ExcludeSeller"
        assert actual_params["itemFilter(0).value"] == "badguy"
        assert actual_params["itemFilter(1).value"] == "anotherbad"

    def test_api_error_returns_empty_list(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "findItemsAdvancedResponse": [{
                "ack": ["Failure"],
                "errorMessage": [{"error": [{"message": ["Invalid appId"]}]}],
            }]
        }

        with patch("ebayscout.ebay_client.requests.get", return_value=mock_resp):
            results = ebay_client.find_listings("BAD_APP", "keyword", [])

        assert results == []

    def test_http_error_returns_empty_list(self):
        import requests as req
        with patch("ebayscout.ebay_client.requests.get", side_effect=req.HTTPError("500")):
            results = ebay_client.find_listings("APP", "keyword", [])
        assert results == []


# ---------------------------------------------------------------------------
# find_all_listings
# ---------------------------------------------------------------------------

class TestFindAllListings:
    def test_deduplicates_across_queries(self):
        """Same item appearing in two query results should only appear once."""
        shared_item = _make_item("shared_001")
        unique_item = _make_item("unique_002")

        responses = [
            _make_finding_response([shared_item]),
            _make_finding_response([shared_item, unique_item]),
        ]
        mock_resps = [MagicMock(status_code=200) for _ in responses]
        for mr, data in zip(mock_resps, responses):
            mr.json.return_value = data

        with patch("ebayscout.ebay_client.requests.get", side_effect=mock_resps):
            results = ebay_client.find_all_listings(
                app_id="APP",
                queries=["query_a", "query_b"],
                excluded_sellers=[],
            )

        ids = [r["item_id"] for r in results]
        assert "shared_001" in ids
        assert "unique_002" in ids
        assert ids.count("shared_001") == 1   # deduped


# ---------------------------------------------------------------------------
# get_item_pictures
# ---------------------------------------------------------------------------

class TestGetItemPictures:
    def test_returns_picture_urls(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "Item": {
                "PictureURL": [
                    "https://i.ebayimg.com/photo1.jpg",
                    "https://i.ebayimg.com/photo2.jpg",
                ]
            }
        }

        with patch("ebayscout.ebay_client.requests.get", return_value=mock_resp):
            urls = ebay_client.get_item_pictures("APP", "123456")

        assert len(urls) == 2
        assert urls[0].startswith("https://")

    def test_returns_empty_on_error(self):
        import requests as req
        with patch("ebayscout.ebay_client.requests.get", side_effect=req.HTTPError("404")):
            urls = ebay_client.get_item_pictures("APP", "999")
        assert urls == []

    def test_handles_single_string_picture_url(self):
        """eBay sometimes returns a single string instead of a list."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "Item": {"PictureURL": "https://i.ebayimg.com/only.jpg"}
        }

        with patch("ebayscout.ebay_client.requests.get", return_value=mock_resp):
            urls = ebay_client.get_item_pictures("APP", "555")

        assert urls == ["https://i.ebayimg.com/only.jpg"]


# ---------------------------------------------------------------------------
# Keyword exclusion
# ---------------------------------------------------------------------------

class TestKeywordExclusion:
    def _resp(self, items):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_finding_response(items)
        return mock_resp

    def test_apparel_title_excluded(self):
        items = [
            _make_item("1", "Penn State Embroidered Hoodie"),
            _make_item("2", "PSU Football Button 1987"),
        ]
        with patch("ebayscout.ebay_client.requests.get", return_value=self._resp(items)):
            results = ebay_client.find_listings(
                "APP", "Penn State button", [],
                excluded_keywords=["hoodie", "embroidered"],
            )
        assert len(results) == 1
        assert results[0]["item_id"] == "2"

    def test_keyword_match_is_case_insensitive(self):
        items = [_make_item("1", "PSU DRIFIT Polo Shirt")]
        with patch("ebayscout.ebay_client.requests.get", return_value=self._resp(items)):
            results = ebay_client.find_listings(
                "APP", "PSU polo", [],
                excluded_keywords=["polo", "drifit"],
            )
        assert results == []

    def test_empty_excluded_keywords_keeps_all(self):
        items = [_make_item("1", "Penn State Hoodie Pin")]
        with patch("ebayscout.ebay_client.requests.get", return_value=self._resp(items)):
            results = ebay_client.find_listings(
                "APP", "Penn State pin", [],
                excluded_keywords=[],
            )
        assert len(results) == 1

    def test_uses_config_defaults_when_none(self):
        """When excluded_keywords=None, config.EXCLUDED_KEYWORDS is used."""
        from ebayscout import config
        items = [_make_item("1", f"PSU {config.EXCLUDED_KEYWORDS[0].title()} Shirt")]
        with patch("ebayscout.ebay_client.requests.get", return_value=self._resp(items)):
            results = ebay_client.find_listings("APP", "PSU button", [])
        # Should be filtered by the config default
        assert results == []
