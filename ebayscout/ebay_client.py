"""
ebayscout/ebay_client.py

eBay Browse API — listing discovery + photos.

The legacy Finding API (svcs.ebay.com/.../FindingService) and Shopping API
(open.api.ebay.com/shopping) were both decommissioned by eBay on 2025-02-05.
This module uses the modern Browse API, which requires an OAuth application
access token obtained via the client-credentials grant.

Credentials (from GCP Secret Manager):
  EBAY_APP_ID   — OAuth client id (App ID)
  EBAY_CERT_ID  — OAuth client secret (Cert ID)
"""

import time
import base64
import requests
from urllib.parse import quote

from . import config
from .utils import title_has_excluded_keyword


# Cached client-credentials token, shared across calls within a process.
_token_cache: dict = {"token": None, "expires_at": 0.0}

# Standard headers for Browse API requests (auth added per-call).
_MARKETPLACE = "EBAY_US"


def _get_app_token(client_id: str, client_secret: str) -> str:
    """
    Return a client-credentials access token, fetching a fresh one only when
    the cache is empty or within 60s of expiry. Raises on auth failure.
    """
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        config.EBAY_OAUTH_URL,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": config.EBAY_OAUTH_SCOPE},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    _token_cache["token"] = payload["access_token"]
    _token_cache["expires_at"] = now + float(payload.get("expires_in", 7200))
    return _token_cache["token"]


def find_listings(
    client_id: str,
    client_secret: str,
    keywords: str,
    excluded_sellers: list[str],
    excluded_keywords: list[str] | None = None,
    max_results: int = 100,
    category_ids: str | None = None,
    search_year: int | None = None,
    search_era: str | None = None,
) -> list[dict]:
    """
    Search the Browse API item_summary/search for one keyword string.

    Returns a list of dicts:
        {item_id, title, current_price, currency, listing_url, gallery_url,
         seller, search_year, search_era}

    search_year / search_era: stamped onto every returned listing (None for
    general queries). When set, the caller knows these results came from a
    year- or era-specific query and can restrict CLIP matching accordingly.

    Excluded sellers, excluded categories (e.g. Clothing/Shoes/Accessories),
    and apparel keywords are filtered client-side (the Browse API has no
    server-side exclude equivalent).
    """
    if excluded_keywords is None:
        excluded_keywords = config.EXCLUDED_KEYWORDS
    excluded_lower = {s.lower() for s in excluded_sellers}
    excluded_cats  = {str(c) for c in config.EXCLUDED_CATEGORY_IDS}

    try:
        token = _get_app_token(client_id, client_secret)
    except Exception as exc:
        print(f"!!! EBAY AUTH: token request failed: {exc}", flush=True)
        return []

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": _MARKETPLACE,
    }

    # A single page of the newest listings is plenty for a daily scan; the
    # Browse API rejects deep offsets for application tokens, so we don't
    # paginate. limit caps at the Browse page maximum of 200.
    params = {
        "q":     keywords,
        "limit": str(min(200, max_results)),
        "sort":  "newlyListed",
    }
    if category_ids:
        params["category_ids"] = category_ids
    try:
        resp = _get_with_retry(config.EBAY_BROWSE_SEARCH_URL, params, headers)
    except Exception as exc:
        print(f"!!! EBAY FIND: HTTP error for '{keywords}': {exc}", flush=True)
        return []

    try:
        data = resp.json()
    except Exception:
        print(f"!!! EBAY FIND: JSON parse error for '{keywords}'", flush=True)
        return []

    results: dict[str, dict] = {}
    for item in data.get("itemSummaries") or []:
        item_id = item.get("itemId")
        if not item_id or item_id in results:
            continue

        title  = item.get("title") or ""
        seller = (item.get("seller") or {}).get("username", "") or ""
        if seller.lower() in excluded_lower:
            continue
        if any(str((c or {}).get("categoryId")) in excluded_cats
               for c in (item.get("categories") or [])):
            continue
        if title_has_excluded_keyword(title, excluded_keywords):
            continue

        price_data = item.get("price") or {}
        try:
            price = float(price_data.get("value", "0"))
        except (TypeError, ValueError):
            price = 0.0

        image = (item.get("image") or {}).get("imageUrl", "") or ""
        if not image:
            thumbs = item.get("thumbnailImages") or []
            if thumbs:
                image = thumbs[0].get("imageUrl", "") or ""

        results[item_id] = {
            "item_id":       item_id,
            "title":         title,
            "current_price": price,
            "currency":      price_data.get("currency", "USD"),
            "listing_url":   item.get("itemWebUrl", "") or "",
            "gallery_url":   image,
            "seller":        seller,
            "search_year":   search_year,
            "search_era":    search_era,
            # Market-analysis signals (Browse summary fields we previously dropped).
            # buyingOptions: ["FIXED_PRICE"] / ["AUCTION"] / ["AUCTION","FIXED_PRICE"].
            # bidCount + currentBidPrice are present only for auctions.
            "buying_options": item.get("buyingOptions") or [],
            "condition":      item.get("condition", "") or "",
            "bid_count":      item.get("bidCount"),
        }

    print(f">>> EBAY FIND: '{keywords}' → {len(results)} unique listings", flush=True)
    return list(results.values())


def find_all_listings(
    client_id: str,
    client_secret: str,
    queries: list[str] | None = None,
    excluded_sellers: list[str] | None = None,
    excluded_keywords: list[str] | None = None,
    max_results: int = 100,
    category_ids: str | None = None,
) -> list[dict]:
    """
    Run find_listings() for each query, deduplicate by item_id, return combined
    unique list. Pass category_ids to restrict all queries to a specific category.
    """
    if queries is None:
        queries = config.EBAY_SEARCH_QUERIES
    if excluded_sellers is None:
        excluded_sellers = config.EXCLUDED_SELLERS
    if excluded_keywords is None:
        excluded_keywords = config.EXCLUDED_KEYWORDS

    seen_ids: set[str] = set()
    all_listings: list[dict] = []

    for query in queries:
        batch = find_listings(
            client_id, client_secret, query,
            excluded_sellers, excluded_keywords, max_results,
            category_ids=category_ids,
        )
        for listing in batch:
            iid = listing["item_id"]
            if iid not in seen_ids:
                seen_ids.add(iid)
                all_listings.append(listing)

    print(f">>> EBAY FIND: Total unique listings across all queries: {len(all_listings)}", flush=True)
    return all_listings


def find_year_augmented_listings(
    client_id: str,
    client_secret: str,
    year_queries: list[tuple[str, int]],
    excluded_sellers: list[str] | None = None,
    excluded_keywords: list[str] | None = None,
    max_results: int = 100,
    category_ids: str | None = None,
) -> list[dict]:
    """
    Run a list of (query, year) pairs (from utils.build_year_queries), tagging
    every returned listing with its search_year, and deduplicate by item_id
    (first occurrence wins its search_year).

    Used by the on-demand needed-year deep crawl: a query like "Penn State
    button 1982" reaches the small, often-complete bucket of that year's
    listings — including old ones the general newest-100 windows never surface.
    """
    if excluded_sellers is None:
        excluded_sellers = config.EXCLUDED_SELLERS
    if excluded_keywords is None:
        excluded_keywords = config.EXCLUDED_KEYWORDS

    seen_ids: set[str] = set()
    all_listings: list[dict] = []

    for query, year in year_queries:
        batch = find_listings(
            client_id, client_secret, query,
            excluded_sellers, excluded_keywords, max_results,
            category_ids=category_ids, search_year=year,
        )
        for listing in batch:
            iid = listing["item_id"]
            if iid not in seen_ids:
                seen_ids.add(iid)
                all_listings.append(listing)

    print(f">>> EBAY YEAR-CRAWL: {len(year_queries)} queries → "
          f"{len(all_listings)} unique listings.", flush=True)
    return all_listings


def find_era_augmented_listings(
    client_id: str,
    client_secret: str,
    era_queries: list[tuple[str, str]],
    excluded_sellers: list[str] | None = None,
    excluded_keywords: list[str] | None = None,
    max_results: int = 100,
    category_ids: str | None = None,
) -> list[dict]:
    """
    Run a list of (query, era_label) pairs (from utils.build_era_queries),
    tagging every returned listing with its search_era, and deduplicate by
    item_id (first occurrence wins its search_era).

    Used by the daily CCB pass and the on-demand era crawl: the bank word in the
    query surfaces era-tagged listings and tells the matcher which era to
    restrict to. PSU-prefixed queries are passed with category_ids by the caller.
    """
    if excluded_sellers is None:
        excluded_sellers = config.EXCLUDED_SELLERS
    if excluded_keywords is None:
        excluded_keywords = config.EXCLUDED_KEYWORDS

    seen_ids: set[str] = set()
    all_listings: list[dict] = []

    for query, era in era_queries:
        batch = find_listings(
            client_id, client_secret, query,
            excluded_sellers, excluded_keywords, max_results,
            category_ids=category_ids, search_era=era,
        )
        for listing in batch:
            iid = listing["item_id"]
            if iid not in seen_ids:
                seen_ids.add(iid)
                all_listings.append(listing)

    print(f">>> EBAY ERA-CRAWL: {len(era_queries)} queries → "
          f"{len(all_listings)} unique listings.", flush=True)
    return all_listings


def get_item_pictures(client_id: str, client_secret: str, item_id: str) -> list[str]:
    """
    Fetch the primary + additional image URLs for a listing via Browse getItem.
    Returns [] on error — caller should fall back to the search gallery_url.
    """
    try:
        token = _get_app_token(client_id, client_secret)
    except Exception as exc:
        print(f"!!! EBAY AUTH: token request failed: {exc}", flush=True)
        return []

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": _MARKETPLACE,
    }
    url = f"{config.EBAY_BROWSE_ITEM_URL}/{quote(item_id, safe='')}"

    try:
        resp = _get_with_retry(url, None, headers)
        data = resp.json()
    except Exception as exc:
        print(f"!!! EBAY GETITEM: Failed to get pictures for {item_id}: {exc}", flush=True)
        return []

    urls: list[str] = []
    primary = (data.get("image") or {}).get("imageUrl", "") or ""
    if primary:
        urls.append(primary)
    for img in data.get("additionalImages") or []:
        u = img.get("imageUrl", "") or ""
        if u:
            urls.append(u)

    print(f">>> EBAY GETITEM: {item_id} → {len(urls)} picture URL(s)", flush=True)
    return urls


def get_item(client_id: str, client_secret: str, item_id: str) -> dict | None:
    """
    Fetch one listing by item_id via Browse getItem and return it in the same
    shape find_listings() produces (item_id, title, current_price, currency,
    listing_url, gallery_url, seller, buying_options, condition, bid_count).

    Used to "hunt" specific known IDs (e.g. recovered from a prior run's logs)
    to backfill the market database with each listing's full CURRENT data —
    crucially the asking price, which the scan stdout never recorded. Returns
    None if the item can't be fetched (ended / removed / error); no keyword or
    category exclusion is applied, since the caller chose these IDs deliberately.
    """
    try:
        token = _get_app_token(client_id, client_secret)
    except Exception as exc:
        print(f"!!! EBAY AUTH: token request failed: {exc}", flush=True)
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": _MARKETPLACE,
    }
    url = f"{config.EBAY_BROWSE_ITEM_URL}/{quote(item_id, safe='')}"

    try:
        resp = _get_with_retry(url, None, headers)
        data = resp.json()
    except Exception as exc:
        print(f"!!! EBAY GETITEM: hunt failed for {item_id}: {exc}", flush=True)
        return None

    price_data = data.get("price") or {}
    try:
        price = float(price_data.get("value", "0"))
    except (TypeError, ValueError):
        price = 0.0

    image = (data.get("image") or {}).get("imageUrl", "") or ""
    if not image:
        addl = data.get("additionalImages") or []
        if addl:
            image = addl[0].get("imageUrl", "") or ""

    return {
        "item_id":        data.get("itemId") or item_id,
        "title":          data.get("title") or "",
        "current_price":  price,
        "currency":       price_data.get("currency", "USD"),
        "listing_url":    data.get("itemWebUrl", "") or "",
        "gallery_url":    image,
        "seller":         (data.get("seller") or {}).get("username", "") or "",
        "search_year":    None,
        "search_era":     None,
        "buying_options": data.get("buyingOptions") or [],
        "condition":      data.get("condition", "") or "",
        "bid_count":      data.get("bidCount"),
    }


def find_listings_by_ids(client_id: str, client_secret: str, item_ids) -> list[dict]:
    """
    Hunt a list of specific item_ids via get_item(), skipping any that can't be
    fetched (ended/removed). Deduplicates the input and logs progress. The
    returned listings flow through the normal scan pipeline, so each one gets a
    full scan_log record (with price) — this is how a known ID list rebuilds the
    market database. Composes with chunk mode (/run-scan?limit=N) for big lists.
    """
    seen: set = set()
    unique = [i for i in item_ids if not (i in seen or seen.add(i))]
    print(f">>> EBAY HUNT: fetching {len(unique)} item(s) by ID...", flush=True)
    out: list[dict] = []
    for n, item_id in enumerate(unique, 1):
        listing = get_item(client_id, client_secret, item_id)
        if listing:
            out.append(listing)
        if n % 50 == 0:
            print(f">>> EBAY HUNT: {n}/{len(unique)} fetched ({len(out)} live).", flush=True)
    print(f">>> EBAY HUNT: {len(out)} of {len(unique)} IDs still live.", flush=True)
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_with_retry(
    url: str, params: dict | None, headers: dict, max_retries: int = 3
) -> requests.Response:
    """GET with retry on 429 (rate limit). Raises requests.HTTPError otherwise."""
    backoff = 5  # seconds

    for attempt in range(max_retries):
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        if resp.status_code == 429 and attempt < max_retries - 1:
            print(f"!!! EBAY: Rate limited — sleeping {backoff}s before retry", flush=True)
            time.sleep(backoff)
            backoff *= 2
            continue
        resp.raise_for_status()
        return resp

    raise requests.HTTPError(f"Persistent failure after {max_retries} attempts")
