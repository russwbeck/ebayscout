"""
Tests for ebayscout/ebay_client.py (eBay Browse API).
"""

import pytest
from unittest.mock import patch, MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import ebay_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _browse_response(items: list[dict], total: int | None = None):
    """Build a minimal Browse API item_summary/search JSON response."""
    return {
        "itemSummaries": items,
        "total": total if total is not None else len(items),
    }


def _summary(item_id="v1|111|0", title="Penn State Button", seller="testseller",
             price="5.00", currency="USD", image="https://i.ebayimg.com/x.jpg",
             url="https://ebay.com/itm/111"):
    return {
        "itemId":     item_id,
        "title":      title,
        "seller":     {"username": seller},
        "price":      {"value": price, "currency": currency},
        "image":      {"imageUrl": image},
        "itemWebUrl": url,
    }


def _mock_get(json_payload):
    resp = MagicMock(status_code=200)
    resp.json.return_value = json_payload
    return resp


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------

class TestAppToken:
    def setup_method(self):
        ebay_client._token_cache["token"] = None
        ebay_client._token_cache["expires_at"] = 0.0

    def test_fetches_and_caches_token(self):
        post_resp = MagicMock(status_code=200)
        post_resp.json.return_value = {"access_token": "TOK123", "expires_in": 7200}

        with patch("ebayscout.ebay_client.requests.post", return_value=post_resp) as mock_post:
            t1 = ebay_client._get_app_token("id", "secret")
            t2 = ebay_client._get_app_token("id", "secret")

        assert t1 == "TOK123"
        assert t2 == "TOK123"
        assert mock_post.call_count == 1   # second call served from cache


# ---------------------------------------------------------------------------
# find_listings
# ---------------------------------------------------------------------------

class TestFindListings:
    def test_parses_item_fields_correctly(self):
        resp = _mock_get(_browse_response([
            _summary("v1|1|0", "Lot of PSU buttons", "seller_a", "10.00")
        ]))
        with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
             patch("ebayscout.ebay_client.requests.get", return_value=resp):
            results = ebay_client.find_listings("id", "sec", "PSU button", [])

        assert len(results) == 1
        r = results[0]
        assert r["item_id"] == "v1|1|0"
        assert r["title"] == "Lot of PSU buttons"
        assert r["seller"] == "seller_a"
        assert r["current_price"] == 10.0
        assert r["currency"] == "USD"
        assert r["gallery_url"] == "https://i.ebayimg.com/x.jpg"

    def test_deduplicates_within_query(self):
        item = _summary("dupe_id")
        resp = _mock_get(_browse_response([item, item], total=2))
        with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
             patch("ebayscout.ebay_client.requests.get", return_value=resp):
            results = ebay_client.find_listings("id", "sec", "PSU button", [])

        assert len(results) == 1
        assert results[0]["item_id"] == "dupe_id"

    def test_excludes_seller_client_side(self):
        resp = _mock_get(_browse_response([
            _summary("v1|1|0", "PSU button", seller="badguy"),
            _summary("v1|2|0", "PSU button", seller="goodseller"),
        ]))
        with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
             patch("ebayscout.ebay_client.requests.get", return_value=resp):
            results = ebay_client.find_listings("id", "sec", "PSU button", ["BadGuy"])

        assert len(results) == 1
        assert results[0]["seller"] == "goodseller"

    def test_falls_back_to_thumbnail_image(self):
        item = _summary("v1|1|0")
        del item["image"]
        item["thumbnailImages"] = [{"imageUrl": "https://thumb/x.jpg"}]
        resp = _mock_get(_browse_response([item]))
        with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
             patch("ebayscout.ebay_client.requests.get", return_value=resp):
            results = ebay_client.find_listings("id", "sec", "PSU button", [])

        assert results[0]["gallery_url"] == "https://thumb/x.jpg"

    def test_http_error_returns_empty_list(self):
        import requests as req
        with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
             patch("ebayscout.ebay_client.requests.get", side_effect=req.HTTPError("500")):
            results = ebay_client.find_listings("id", "sec", "keyword", [])
        assert results == []

    def test_auth_failure_returns_empty_list(self):
        with patch("ebayscout.ebay_client._get_app_token", side_effect=Exception("bad creds")):
            results = ebay_client.find_listings("id", "sec", "keyword", [])
        assert results == []


# ---------------------------------------------------------------------------
# find_all_listings
# ---------------------------------------------------------------------------

class TestFindAllListings:
    def test_deduplicates_across_queries(self):
        shared = _summary("shared_001")
        unique = _summary("unique_002")
        resps = [
            _mock_get(_browse_response([shared])),
            _mock_get(_browse_response([shared, unique], total=2)),
        ]
        with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
             patch("ebayscout.ebay_client.requests.get", side_effect=resps):
            results = ebay_client.find_all_listings(
                client_id="id", client_secret="sec",
                queries=["query_a", "query_b"], excluded_sellers=[],
            )

        ids = [r["item_id"] for r in results]
        assert "shared_001" in ids
        assert "unique_002" in ids
        assert ids.count("shared_001") == 1


# ---------------------------------------------------------------------------
# get_item_pictures
# ---------------------------------------------------------------------------

class TestGetItemPictures:
    def test_returns_primary_and_additional(self):
        resp = _mock_get({
            "image": {"imageUrl": "https://i.ebayimg.com/photo1.jpg"},
            "additionalImages": [
                {"imageUrl": "https://i.ebayimg.com/photo2.jpg"},
                {"imageUrl": "https://i.ebayimg.com/photo3.jpg"},
            ],
        })
        with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
             patch("ebayscout.ebay_client.requests.get", return_value=resp):
            urls = ebay_client.get_item_pictures("id", "sec", "v1|123|0")

        assert urls == [
            "https://i.ebayimg.com/photo1.jpg",
            "https://i.ebayimg.com/photo2.jpg",
            "https://i.ebayimg.com/photo3.jpg",
        ]

    def test_returns_empty_on_error(self):
        import requests as req
        with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
             patch("ebayscout.ebay_client.requests.get", side_effect=req.HTTPError("404")):
            urls = ebay_client.get_item_pictures("id", "sec", "v1|999|0")
        assert urls == []


# ---------------------------------------------------------------------------
# Keyword exclusion
# ---------------------------------------------------------------------------

class TestKeywordExclusion:
    def test_apparel_title_excluded(self):
        resp = _mock_get(_browse_response([
            _summary("v1|1|0", "Penn State Embroidered Hoodie"),
            _summary("v1|2|0", "PSU Football Button 1987"),
        ]))
        with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
             patch("ebayscout.ebay_client.requests.get", return_value=resp):
            results = ebay_client.find_listings(
                "id", "sec", "Penn State button", [],
                excluded_keywords=["hoodie", "embroidered"],
            )
        assert len(results) == 1
        assert results[0]["item_id"] == "v1|2|0"

    def test_keyword_match_is_case_insensitive(self):
        resp = _mock_get(_browse_response([_summary("v1|1|0", "PSU DRIFIT Polo Shirt")]))
        with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
             patch("ebayscout.ebay_client.requests.get", return_value=resp):
            results = ebay_client.find_listings(
                "id", "sec", "PSU polo", [],
                excluded_keywords=["polo", "drifit"],
            )
        assert results == []

    def test_empty_excluded_keywords_keeps_all(self):
        resp = _mock_get(_browse_response([_summary("v1|1|0", "Penn State Hoodie Pin")]))
        with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
             patch("ebayscout.ebay_client.requests.get", return_value=resp):
            results = ebay_client.find_listings(
                "id", "sec", "Penn State pin", [], excluded_keywords=[],
            )
        assert len(results) == 1

    def test_uses_config_defaults_when_none(self):
        from ebayscout import config
        resp = _mock_get(_browse_response([
            _summary("v1|1|0", f"PSU {config.EXCLUDED_KEYWORDS[0].title()} Shirt")
        ]))
        with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
             patch("ebayscout.ebay_client.requests.get", return_value=resp):
            results = ebay_client.find_listings("id", "sec", "PSU button", [])
        assert results == []
