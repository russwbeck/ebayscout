"""
ebayscout/seller_listings.py

One seller's Penn State listings, pulled on demand for price comparison
(2026-10-03).

The operator lists their own buttons and wants to price against another seller
(kling24toys first).  ``/crawl seller <username>`` asks the Browse API for that
seller's listings and writes title, price and shipping to a ``seller_<username>``
tab in the Logger workbook, replacing the previous pull.  The comparison itself
is done from the sheet.

It is cheap on purpose: a few Browse API pages and one sheet write.  No photo is
downloaded, nothing goes to Gemini or CLIP, nothing is marked seen, and nothing
runs on a schedule — the operator asked for no checks beyond the 9 AM scan, so
this only ever runs when someone types it.  EXCLUDED_SELLERS is not applied, so
the seller can be one the daily scan excludes: excluding a seller from the scan
and reading their prices are different jobs.

Pure apart from ``write_tab``, which takes the gspread client from the caller.
"""

import re

from . import price_log

# eBay user IDs: letters, digits, period, underscore, hyphen.  Anything else is a
# typo, and it would otherwise go into the Browse filter string unescaped.
SELLER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Each query is filtered to the seller, so these only have to cover how a
# Penn State title can be worded.  Results are deduplicated by item number.
QUERIES = ("penn state", "psu", "nittany")
PAGE_SIZE = 200          # the Browse page maximum
MAX_PAGES = 5            # per query: up to 1,000 listings, far more than one seller lists

TAB_PREFIX = "seller_"
HEADER = ["pulled_at", "seller", "ebay_id", "title", "price", "shipping",
          "format", "bids", "condition", "listing_url"]


def parse_command(text):
    """Read the text after ``/crawl``.

    Returns ``(is_seller, seller)``: ``(False, None)`` for anything that is not
    the seller form (``/crawl 800`` keeps its meaning), ``(True, name)`` for
    ``seller kling24toys`` or ``seller:kling24toys``, and ``(True, None)`` when
    the seller form carries no valid username.
    """
    m = re.match(r"(?i)^\s*seller\b[\s:]*(.*?)\s*$", text or "")
    if not m:
        return False, None
    name = m.group(1)
    return True, (name if SELLER_RE.match(name) else None)


def search_params(seller, query, offset):
    """Browse item_summary/search params for one page of one seller's listings."""
    return {
        "q": query,
        "filter": f"sellers:{{{seller}}}",
        "limit": str(PAGE_SIZE),
        "offset": str(offset),
    }


def shipping_cost(item):
    """The first shipping option's cost as a number, "calculated" when eBay
    works it out per buyer, or "" when the summary carries none."""
    opts = item.get("shippingOptions") or []
    if not opts:
        return ""
    first = opts[0] or {}
    if str(first.get("shippingCostType", "")).upper() == "CALCULATED":
        return "calculated"
    try:
        return float((first.get("shippingCost") or {}).get("value"))
    except (TypeError, ValueError):
        return ""


def summary_row(item, pulled_at, seller):
    """One sheet row from a Browse item summary, or None without an item number."""
    number = price_log.ebay_item_number(item.get("itemId"))
    if not number:
        return None
    try:
        price = float((item.get("price") or {}).get("value"))
    except (TypeError, ValueError):
        price = ""
    bids = item.get("bidCount")
    return [
        pulled_at,
        seller,
        number,
        item.get("title") or "",
        price,
        shipping_cost(item),
        price_log.listing_format(item.get("buyingOptions")),
        bids if isinstance(bids, int) else "",
        item.get("condition") or "",
        item.get("itemWebUrl") or "",
    ]


def tab_name(seller):
    return TAB_PREFIX + seller.lower()


def write_tab(gspread_client, spreadsheet_id, seller, rows):
    """Replace the seller's tab with HEADER + rows; return the tab's title.

    RAW, so an item number stays text and a slogan that starts with "=" is not a
    formula.  Raises on failure: the caller reports it, since a pull that wrote
    nothing should say so.
    """
    ss = gspread_client.open_by_key(price_log._spreadsheet_key(spreadsheet_id))
    title = tab_name(seller)
    existing = {w.title: w for w in ss.worksheets()}
    ws = existing.get(title)
    if ws is None:
        ws = ss.add_worksheet(title=title, rows=max(100, len(rows) + 10), cols=len(HEADER))
    else:
        ws.clear()
    ws.update(values=[HEADER] + rows, range_name="A1", value_input_option="RAW")
    return title


def summary_text(seller, n, tab, error=None):
    """The Slack line for one pull."""
    if error:
        return f"⚠️ Seller pull for *{seller}* failed: {error}"
    if not n:
        return (f"Seller pull for *{seller}*: no Penn State listings found "
                f"(check the username).")
    return (f"📋 Pulled {n} Penn State listing{'s' if n != 1 else ''} from *{seller}* "
            f"into the `{tab}` tab of the Logger sheet (title, price, shipping).")
