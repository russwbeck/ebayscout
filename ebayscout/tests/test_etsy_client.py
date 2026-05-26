"""
Tests for ebayscout/etsy_client.py

Mocks requests.get to return canned Etsy API JSON responses so no real
network calls are made.
"""

import sys
import os
import json
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import etsy_client


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_item(listing_id, title, shop_name, amount=250, currency="USD",
               image_url="https://i.etsy.com/img.jpg", listing_url="https://etsy.com/listing"):
    """Build a minimal Etsy listing dict matching the API response shape."""
    return {
        "listing_id": listing_id,
        "title":      title,
        "url":        listing_url,
        "price": {
            "amount":        amount,
            "divisor":       100,
            "currency_code": currency,
        },
        "images": [
            {
                "url_570xN":     image_url,
                "url_fullxfull": image_url.replace(".jpg", "_full.jpg"),
            }
        ],
        "shop": {"shop_name": shop_name},
    }


def _mock_response(items, total_count=None):
    """Return a mock requests.Response whose .json() returns an Etsy results envelope."""
    if total_count is None:
        total_count = len(items)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"results": items, "count": total_count}
    mock_resp.raise_for_status.return_value = None
    return mock_resp


# ---------------------------------------------------------------------------
# find_listings — basic fetching
# ---------------------------------------------------------------------------

class TestFindListings:
    def test_returns_list_of_dicts(self):
        items = [_make_item(1, "PSU Button 1977", "ShopA")]
        with patch("requests.get", return_value=_mock_response(items)):
            results = etsy_client.find_listings("key", "PSU button")
        assert isinstance(results, list)
        assert len(results) == 1

    def test_item_id_prefixed_etsy(self):
        items = [_make_item(42, "Penn State Pin", "ShopB")]
        with patch("requests.get", return_value=_mock_response(items)):
            results = etsy_client.find_listings("key", "Penn State pin")
        assert results[0]["item_id"] == "etsy_42"

    def test_price_calculated_correctly(self):
        # amount=375, divisor=100 → $3.75
        items = [_make_item(1, "Test Button", "ShopC", amount=375)]
        with patch("requests.get", return_value=_mock_response(items)):
            results = etsy_client.find_listings("key", "button")
        assert results[0]["current_price"] == 3.75

    def test_gallery_url_uses_fullxfull(self):
        items = [_make_item(1, "Test", "ShopD",
                            image_url="https://i.etsy.com/thumb.jpg")]
        with patch("requests.get", return_value=_mock_response(items)):
            results = etsy_client.find_listings("key", "button")
        assert results[0]["gallery_url"] == "https://i.etsy.com/thumb_full.jpg"

    def test_seller_field_populated(self):
        items = [_make_item(1, "Test", "MyShop")]
        with patch("requests.get", return_value=_mock_response(items)):
            results = etsy_client.find_listings("key", "button")
        assert results[0]["seller"] == "MyShop"

    def test_empty_results_returns_empty_list(self):
        with patch("requests.get", return_value=_mock_response([])):
            results = etsy_client.find_listings("key", "nothing")
        assert results == []

    def test_excluded_seller_filtered_out(self):
        items = [
            _make_item(1, "Keep",   "GoodShop"),
            _make_item(2, "Filter", "BadShop"),
        ]
        with patch("requests.get", return_value=_mock_response(items)):
            results = etsy_client.find_listings("key", "button",
                                                excluded_sellers=["badshop"])
        assert len(results) == 1
        assert results[0]["seller"] == "GoodShop"

    def test_excluded_seller_case_insensitive(self):
        items = [_make_item(1, "Test", "BADSHOP")]
        with patch("requests.get", return_value=_mock_response(items)):
            results = etsy_client.find_listings("key", "button",
                                                excluded_sellers=["badshop"])
        assert results == []

    def test_dedup_by_item_id(self):
        """Same listing_id returned twice should only appear once."""
        items = [
            _make_item(99, "Dup 1", "ShopX"),
            _make_item(99, "Dup 2", "ShopX"),
        ]
        with patch("requests.get", return_value=_mock_response(items)):
            results = etsy_client.find_listings("key", "button")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# find_listings — error handling
# ---------------------------------------------------------------------------

class TestFindListingsErrors:
    def test_http_error_returns_empty_list(self):
        import requests
        with patch("requests.get", side_effect=requests.HTTPError("404")):
            results = etsy_client.find_listings("key", "button")
        assert results == []

    def test_malformed_item_skipped(self):
        """An item missing 'listing_id' should be skipped, others still returned."""
        good = _make_item(1, "Good", "ShopA")
        bad  = {"title": "No ID"}   # missing listing_id
        with patch("requests.get", return_value=_mock_response([good, bad])):
            results = etsy_client.find_listings("key", "button")
        # Only the good one returned
        assert len(results) == 1
        assert results[0]["item_id"] == "etsy_1"

    def test_no_images_falls_back_gracefully(self):
        item = _make_item(1, "Test", "ShopA")
        item["images"] = []   # no images
        with patch("requests.get", return_value=_mock_response([item])):
            results = etsy_client.find_listings("key", "button")
        assert results[0]["gallery_url"] == ""

    def test_rate_limit_retries_then_succeeds(self):
        """First call returns 429, second returns 200 — should succeed."""
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.raise_for_status.side_effect = None

        good_resp = _mock_response([_make_item(1, "Test", "ShopA")])

        with patch("requests.get", side_effect=[rate_limited, good_resp]):
            with patch("time.sleep"):   # don't actually sleep in tests
                results = etsy_client.find_listings("key", "button",
                                                    max_results=1)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# find_all_listings
# ---------------------------------------------------------------------------

class TestFindAllListings:
    def test_deduplicates_across_queries(self):
        """Same Etsy listing returned by multiple queries should appear once."""
        item = _make_item(100, "PSU Button", "ShopZ")

        with patch.object(etsy_client, "find_listings",
                          return_value=[{
                              "item_id":       "etsy_100",
                              "title":         "PSU Button",
                              "current_price": 2.50,
                              "currency":      "USD",
                              "listing_url":   "https://etsy.com",
                              "gallery_url":   "https://img.etsy.com/a.jpg",
                              "seller":        "ShopZ",
                          }]):
            results = etsy_client.find_all_listings(
                api_key="key",
                queries=["PSU button", "Penn State button"],
            )

        assert len(results) == 1
        assert results[0]["item_id"] == "etsy_100"

    def test_uses_default_queries_from_config(self):
        """When queries=None, config.EBAY_SEARCH_QUERIES is used."""
        from ebayscout import config

        call_count = []

        def fake_find(api_key, keywords, excluded_sellers=None, max_results=100):
            call_count.append(keywords)
            return []

        with patch.object(etsy_client, "find_listings", side_effect=fake_find):
            etsy_client.find_all_listings(api_key="key")

        assert len(call_count) == len(config.EBAY_SEARCH_QUERIES)
