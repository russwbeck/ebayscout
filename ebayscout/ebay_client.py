"""
ebayscout/ebay_client.py

eBay Finding API (listing discovery) + Shopping API (full-size photos).
Both APIs are free with just an AppID — no OAuth required.
"""

import time
import json
import requests
import xml.etree.ElementTree as ET

from . import config


def find_listings(
    app_id: str,
    keywords: str,
    excluded_sellers: list[str],
    max_results: int = 100,
) -> list[dict]:
    """
    Call eBay Finding API findItemsAdvanced for one keyword string.

    Returns a list of dicts:
        {item_id, title, current_price, currency, listing_url, gallery_url, seller}

    Applies ExcludeSeller itemFilter for each seller in excluded_sellers.
    Paginates if max_results > 100 (eBay page size hard limit is 100).
    """
    results: dict[str, dict] = {}  # item_id → listing dict (deduped within this query)
    page_size = min(max_results, 100)
    page_num  = 1
    total_pages = 1  # updated after first response

    while page_num <= total_pages and len(results) < max_results:
        params = _build_params(app_id, keywords, excluded_sellers, page_num, page_size)

        try:
            resp = _get_with_retry(config.EBAY_FINDING_URL, params)
        except Exception as exc:
            print(f"!!! EBAY FIND: HTTP error for '{keywords}': {exc}", flush=True)
            break

        try:
            data = resp.json()
        except Exception:
            print(f"!!! EBAY FIND: JSON parse error for '{keywords}'", flush=True)
            break

        root = data.get("findItemsAdvancedResponse", [{}])[0]
        ack  = root.get("ack", [""])[0]
        if ack != "Success":
            err = root.get("errorMessage", [{}])[0].get("error", [{}])[0].get("message", ["(unknown)"])[0]
            print(f"!!! EBAY FIND: API error for '{keywords}': {err}", flush=True)
            break

        # Update pagination info from first page
        pagination = root.get("paginationOutput", [{}])[0]
        try:
            total_pages = int(pagination.get("totalPages", ["1"])[0])
        except (ValueError, IndexError):
            total_pages = 1

        items = root.get("searchResult", [{}])[0].get("item", [])
        for item in items:
            try:
                item_id     = item["itemId"][0]
                title       = item.get("title", [""])[0]
                seller      = item.get("sellerInfo", [{}])[0].get("sellerUserName", [""])[0]
                price_data  = item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
                price       = float(price_data.get("__value__", "0"))
                currency    = price_data.get("@currencyId", "USD")
                listing_url = item.get("viewItemURL", [""])[0]
                gallery_url = item.get("galleryURL", [""])[0]

                if item_id and item_id not in results:
                    results[item_id] = {
                        "item_id":       item_id,
                        "title":         title,
                        "current_price": price,
                        "currency":      currency,
                        "listing_url":   listing_url,
                        "gallery_url":   gallery_url,
                        "seller":        seller,
                    }
            except (KeyError, IndexError, ValueError) as exc:
                print(f"!!! EBAY FIND: Skipping malformed item: {exc}", flush=True)
                continue

        page_num += 1

    print(f">>> EBAY FIND: '{keywords}' → {len(results)} unique listings", flush=True)
    return list(results.values())


def find_all_listings(
    app_id: str,
    queries: list[str] | None = None,
    excluded_sellers: list[str] | None = None,
    max_results: int = 100,
) -> list[dict]:
    """
    Run find_listings() for each query in EBAY_SEARCH_QUERIES,
    deduplicate by item_id, return combined unique list.
    """
    if queries is None:
        queries = config.EBAY_SEARCH_QUERIES
    if excluded_sellers is None:
        excluded_sellers = config.EXCLUDED_SELLERS

    seen_ids: set[str] = set()
    all_listings: list[dict] = []

    for query in queries:
        batch = find_listings(app_id, query, excluded_sellers, max_results)
        for listing in batch:
            iid = listing["item_id"]
            if iid not in seen_ids:
                seen_ids.add(iid)
                all_listings.append(listing)

    print(f">>> EBAY FIND: Total unique listings across all queries: {len(all_listings)}", flush=True)
    return all_listings


def get_item_pictures(app_id: str, item_id: str) -> list[str]:
    """
    Call eBay Shopping API GetSingleItem?IncludeSelector=Details.
    Returns a list of full-size picture URLs.
    Falls back to empty list on error — caller should use gallery_url as fallback.
    """
    params = {
        "callname":        "GetSingleItem",
        "appid":           app_id,
        "version":         "967",
        "ItemID":          item_id,
        "IncludeSelector": "Details",
        "responseencoding": "JSON",
    }

    try:
        resp = _get_with_retry(config.EBAY_SHOPPING_URL, params)
        data = resp.json()
    except Exception as exc:
        print(f"!!! EBAY SHOP: Failed to get pictures for {item_id}: {exc}", flush=True)
        return []

    item = data.get("Item", {})
    if not item:
        print(f"!!! EBAY SHOP: No Item in response for {item_id}", flush=True)
        return []

    picture_url = item.get("PictureURL", [])
    if isinstance(picture_url, str):
        picture_url = [picture_url]

    print(f">>> EBAY SHOP: {item_id} → {len(picture_url)} picture URL(s)", flush=True)
    return [url for url in picture_url if url]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_params(
    app_id: str,
    keywords: str,
    excluded_sellers: list[str],
    page_num: int,
    page_size: int,
) -> dict:
    """Build query-string parameters for findItemsAdvanced."""
    params: dict[str, str] = {
        "OPERATION-NAME":              "findItemsAdvanced",
        "SERVICE-VERSION":             "1.0.0",
        "SECURITY-APPNAME":            app_id,
        "RESPONSE-DATA-FORMAT":        "JSON",
        "REST-PAYLOAD":                "",
        "keywords":                    keywords,
        "sortOrder":                   "StartTimeNewest",
        "paginationInput.pageNumber":  str(page_num),
        "paginationInput.entriesPerPage": str(page_size),
    }

    for i, seller in enumerate(excluded_sellers):
        params[f"itemFilter({i}).name"]  = "ExcludeSeller"
        params[f"itemFilter({i}).value"] = seller

    return params


def _get_with_retry(url: str, params: dict, max_retries: int = 2) -> requests.Response:
    """
    GET request with one retry on 429 (rate limit).
    Raises requests.HTTPError on persistent failure.
    """
    backoff = 5  # seconds

    for attempt in range(max_retries):
        resp = requests.get(url, params=params, timeout=20)
        if resp.status_code == 429:
            if attempt < max_retries - 1:
                print(f"!!! EBAY: Rate limited — sleeping {backoff}s before retry", flush=True)
                time.sleep(backoff)
                backoff *= 2
                continue
        resp.raise_for_status()
        return resp

    # Should not reach here, but satisfy type checker
    raise requests.HTTPError(f"Persistent failure after {max_retries} attempts")
