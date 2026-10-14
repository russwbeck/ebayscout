"""
ebayscout/seller_listings.py

One seller's Penn State listings, pulled on demand for price comparison
(2026-10-03).

The operator lists their own buttons and wants to price against another seller
(kling24toys first).  ``/crawl seller <username>`` asks the Browse API for that
seller's listings and appends them to ``price_log`` in the Logger workbook, one
row per lot: kind ``listing``, source ``seller``, the lot's price and shipping,
no year or slogan (nothing is identified), and run_id
``seller-<username>-<pulled_at>`` so one pull can be filtered out.  Each pull
appends, so a later pull is a later price for the same item; price_analysis's
per-item table shows the highest.  The comparison itself is done from the sheet.

Until 2026-10-14 each pull replaced a ``seller_<username>`` tab of its own; the
operator wanted fewer tabs.  The rows carry no price per button, so they never
enter the per-slogan averages.  A ``/scout sold`` of one of these items takes
its shipping from the row, as from a scan row.

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
    """One price_log row (PRICE_HEADER order) from a Browse item summary, or
    None without an item number.

    Calculated shipping goes in blank, as on a scan row: it has no price
    without a buyer's location.
    """
    number = price_log.ebay_item_number(item.get("itemId"))
    if not number:
        return None
    try:
        price = float((item.get("price") or {}).get("value"))
    except (TypeError, ValueError):
        price = ""
    ship = shipping_cost(item)
    bids = item.get("bidCount")
    seller = seller.lower()
    return [
        pulled_at, price_log.KIND_LISTING, price_log.SOURCE_SELLER, number,
        "", "", "", "",                     # year, slogan, n_in_lot, buttons_detected
        price, "" if ship == "calculated" else ship,
        "", "",                             # price_per_button, allocation
        price_log.listing_format(item.get("buyingOptions")),
        bids if isinstance(bids, int) else "",
        "", price_log.BASIS_ASKING, "no",   # sale_date, price_basis, superseded
        item.get("title") or "", item.get("itemWebUrl") or "",
        f"seller-{seller}-{pulled_at}",
    ]


def tab_name(seller):
    """Where a pull goes: price_log, whoever the seller."""
    return price_log.PRICE_TAB


def write_tab(gspread_client, spreadsheet_id, seller, rows):
    """Append ``rows`` to price_log; return the tab's title.

    RAW, so an item number stays text and a title that starts with "=" is not
    a formula.  Raises on failure, including a price_log with another header:
    the caller reports it, since a pull that wrote nothing should say so.
    """
    ws = price_log.open_price_sheet(gspread_client, spreadsheet_id)
    if ws is None:
        raise RuntimeError(f"the {price_log.PRICE_TAB} tab could not be opened "
                           f"(see the PRICE_LOG line in the service log)")
    if rows:
        ws.append_rows(rows, value_input_option="RAW")
    return price_log.PRICE_TAB


def summary_text(seller, n, tab, error=None):
    """The Slack line for one pull."""
    if error:
        return f"⚠️ Seller pull for *{seller}* failed: {error}"
    if not n:
        return (f"Seller pull for *{seller}*: no Penn State listings found "
                f"(check the username).")
    return (f"📋 Pulled {n} Penn State listing{'s' if n != 1 else ''} from *{seller}* "
            f"into the `{tab}` tab of the Logger sheet (price and shipping, source "
            f"`seller`).")
