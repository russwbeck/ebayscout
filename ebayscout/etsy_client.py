"""
ebayscout/etsy_client.py

Etsy API v3 integration for listing discovery.

Auth: x-api-key header only — no OAuth needed for public listing searches.
Endpoint: https://openapi.etsy.com/v3/application/listings/active

Item IDs are prefixed with "etsy_" to prevent collisions with eBay numeric IDs
in seen_items.json.
"""

import time
import requests

from . import config
from .utils import title_has_excluded_keyword

_ETSY_BASE = "https://openapi.etsy.com/v3/application"


def find_listings(
    api_key: str,
    keywords: str,
    excluded_sellers: list[str] | None = None,
    excluded_keywords: list[str] | None = None,
    max_results: int = 100,
) -> list[dict]:
    """
    Search Etsy active listings for one keyword string.

    Returns a list of dicts matching the same shape as ebay_client.find_listings():
        {item_id, title, current_price, currency, listing_url, gallery_url, seller}

    item_id is prefixed "etsy_" to avoid collisions with eBay IDs.
    Applies excluded_sellers and excluded_keywords filters client-side
    (Etsy API has no server-side exclude).
    """
    if excluded_sellers is None:
        excluded_sellers = []
    if excluded_keywords is None:
        excluded_keywords = config.EXCLUDED_KEYWORDS

    excluded_lower = {s.lower() for s in excluded_sellers}
    results: dict[str, dict] = {}
    offset = 0
    page_size = min(max_results, 100)   # Etsy max per page is 100

    while len(results) < max_results:
        params = {
            "keywords":     keywords,
            "limit":        page_size,
            "offset":       offset,
            "sort_on":      "created",
            "sort_order":   "desc",
            "includes[]":   ["Images", "Shop"],   # embed images + shop in one call
        }

        try:
            resp = _get_with_retry(
                f"{_ETSY_BASE}/listings/active",
                api_key=api_key,
                params=params,
            )
            data = resp.json()
        except Exception as exc:
            print(f"!!! ETSY: HTTP error for '{keywords}': {exc}", flush=True)
            break

        items = data.get("results", [])
        if not items:
            break

        for item in items:
            try:
                listing_id = str(item["listing_id"])
                etsy_id    = f"etsy_{listing_id}"

                # Shop / seller
                shop   = item.get("shop") or {}
                seller = shop.get("shop_name", "")

                # Skip excluded sellers (client-side filter)
                if seller.lower() in excluded_lower:
                    continue

                # Price
                price_data    = item.get("price") or {}
                amount        = price_data.get("amount", 0)
                divisor       = price_data.get("divisor", 100) or 100
                current_price = amount / divisor
                currency      = price_data.get("currency_code", "USD")

                # Images — use url_fullxfull for the primary photo, url_570xN as gallery
                images      = item.get("images") or []
                gallery_url = images[0].get("url_570xN", "")   if images else ""
                full_url    = images[0].get("url_fullxfull", gallery_url) if images else ""

                listing_url = item.get("url", "")
                title       = item.get("title", "")

                if etsy_id not in results:
                    if title_has_excluded_keyword(title, excluded_keywords):
                        continue
                    results[etsy_id] = {
                        "item_id":       etsy_id,
                        "title":         title,
                        "current_price": current_price,
                        "currency":      currency,
                        "listing_url":   listing_url,
                        "gallery_url":   full_url or gallery_url,
                        "seller":        seller,
                    }

            except (KeyError, TypeError, ZeroDivisionError) as exc:
                print(f"!!! ETSY: Skipping malformed item: {exc}", flush=True)
                continue

        # Pagination
        total_count = data.get("count", 0)
        offset += len(items)
        if offset >= total_count or len(items) < page_size:
            break

    print(f">>> ETSY: '{keywords}' → {len(results)} unique listings", flush=True)
    return list(results.values())


def find_all_listings(
    api_key: str,
    queries: list[str] | None = None,
    excluded_sellers: list[str] | None = None,
    excluded_keywords: list[str] | None = None,
    max_results: int = 100,
) -> list[dict]:
    """
    Run find_listings() for each query, deduplicate by item_id.
    Mirrors ebay_client.find_all_listings() so callers can treat both the same.
    """
    if queries is None:
        queries = config.EBAY_SEARCH_QUERIES   # reuse same query list
    if excluded_sellers is None:
        excluded_sellers = config.ETSY_EXCLUDED_SELLERS
    if excluded_keywords is None:
        excluded_keywords = config.EXCLUDED_KEYWORDS

    seen_ids: set[str]    = set()
    all_listings: list[dict] = []

    for query in queries:
        batch = find_listings(api_key, query, excluded_sellers, excluded_keywords, max_results)
        for listing in batch:
            iid = listing["item_id"]
            if iid not in seen_ids:
                seen_ids.add(iid)
                all_listings.append(listing)

    print(f">>> ETSY: Total unique listings across all queries: {len(all_listings)}", flush=True)
    return all_listings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_with_retry(
    url: str,
    api_key: str,
    params: dict,
    max_retries: int = 2,
) -> requests.Response:
    """GET with x-api-key header and one retry on 429."""
    headers = {"x-api-key": api_key}
    backoff = 5

    for attempt in range(max_retries):
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        if resp.status_code == 429:
            if attempt < max_retries - 1:
                print(f"!!! ETSY: Rate limited — sleeping {backoff}s", flush=True)
                time.sleep(backoff)
                backoff *= 2
                continue
        resp.raise_for_status()
        return resp

    raise requests.HTTPError(f"Persistent failure after {max_retries} attempts")
