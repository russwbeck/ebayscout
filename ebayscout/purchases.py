"""
ebayscout/purchases.py

The operator's own eBay purchases, pulled on demand (2026-10-04).

``/crawl purchases`` asks eBay's Trading API (``GetOrders`` with
``OrderRole=Buyer``) for the operator's orders and keeps them in a ``purchases``
tab of the Logger workbook, one row per line item: what was bought, from whom,
and for how much.

Purchases are private, so the app-only token the Browse searches use cannot read
them.  The call needs a user token for the operator's own account, kept as the
``EBAY_USER_TOKEN`` secret (DEPLOY.md, "EBAY_USER_TOKEN"):
- An Auth'n'Auth token (it lasts 18 months) goes in ``RequesterCredentials``.
- An OAuth user access token (``v^1.1#…``, two hours) goes in the
  ``X-EBAY-API-IAF-TOKEN`` header instead.
The token can act on the account, so it lives only in Secret Manager and is
never logged.

What an order cost is on its first line only (2026-10-04), so a column sums to
what was spent:
- ``order_subtotal`` (the items), ``order_shipping`` and ``order_tax`` are eBay's
  order-level figures, and ``order_total`` is what was paid, tax included.
- A line item's own ``ActualShippingCost`` is not used: it is the listing's
  shipping before a combined-shipping discount, so it overstated those orders
  (one showed $8.75 shipping on a $12.84 total: a $12 item plus tax).
- ``item_price`` stays on every line: it is that item's own price, per unit.

``GetOrders`` reaches back 90 days at most.  So the tab is merged, not replaced:
- A pull adds new line items and refreshes the ones it sees again.
- It keeps rows older than eBay still returns.
Pull at least once every 90 days and the tab is a complete history from the
first pull.

Cheap, like ``/crawl seller``: a few API pages and one sheet write, no photos.
It runs two ways:
- **On demand:** ``/crawl purchases``.
- **Mondays:** by itself at the end of the 9 AM daily scan, at the operator's
  request (2026-10-04: "I don't need it daily").
It adds no schedule of its own.

Pure apart from ``write_tab``, which takes the gspread client from the caller.
The HTTP call lives in ``ebay_client.get_purchases``.
"""

import datetime
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from . import price_log

MAX_DAYS = 90            # GetOrders' limit: nothing created earlier comes back
ENTRIES_PER_PAGE = 100   # GetOrders' page maximum
MAX_PAGES = 20           # up to 2,000 orders a pull
COMPATIBILITY_LEVEL = "1451"
PULL_WEEKDAY = 0         # Monday: the daily scan pulls purchases on this day

TAB = "purchases"
HEADER = ["purchased_at", "seller", "ebay_id", "title", "quantity", "item_price",
          "order_subtotal", "order_shipping", "order_tax", "order_total",
          "order_status", "order_id", "line_item_id", "listing_url", "pulled_at"]
KEY = HEADER.index("line_item_id")
WHEN = HEADER.index("purchased_at")
ORDER = HEADER.index("order_id")
ORDER_MONEY = [HEADER.index(c) for c in
               ("order_subtotal", "order_shipping", "order_tax", "order_total")]

# Sales tax eBay collects is reported per line item; the first path found counts.
_TAX_PATHS = ("e:eBayCollectAndRemitTaxes/e:TotalTaxAmount", "e:Taxes/e:TotalTaxAmount")

_NS = {"e": "urn:ebay:apis:eBLBaseComponents"}


class PurchasesError(RuntimeError):
    """eBay answered, but with Ack=Failure (a bad token, a bad window)."""


def parse_command(text):
    """Read the text after ``/crawl``.

    Returns ``(is_purchases, days, error)``:
    - ``(False, None, None)``: not this form, so ``/crawl 800`` keeps its
      meaning.
    - ``(True, 90, None)`` for ``purchases``; ``(True, 30, None)`` for
      ``purchases 30``.
    - ``(True, None, message)`` for a day count that isn't 1–90.
    """
    m = re.match(r"(?i)^\s*purchases?\b[\s:]*(.*?)\s*$", text or "")
    if not m:
        return False, None, None
    arg = m.group(1)
    if not arg:
        return True, MAX_DAYS, None
    if arg.isdigit() and 1 <= int(arg) <= MAX_DAYS:
        return True, int(arg), None
    return True, None, (f"Usage: `/crawl purchases [days]` — days 1–{MAX_DAYS} "
                        f"(eBay returns at most the last {MAX_DAYS} days).")


def is_pull_day(now):
    """True on the day the 9 AM scan also pulls purchases (Monday).

    Read in UTC at the start of the scan.  Cloud Scheduler fires at 9 AM
    America/New_York, which is 13:00 or 14:00 UTC on the same calendar day, so no
    time-zone data is needed.
    """
    return now.astimezone(datetime.timezone.utc).weekday() == PULL_WEEKDAY


def is_oauth(token):
    """OAuth user access tokens start ``v^1.``; Auth'n'Auth tokens don't."""
    return (token or "").startswith("v^1.")


def window(days, now):
    """``(CreateTimeFrom, CreateTimeTo)`` as eBay's UTC timestamps.

    Ten minutes short of the full span, so a 90-day pull never asks for 90
    days and a second, which eBay refuses.
    """
    to = now.astimezone(datetime.timezone.utc).replace(microsecond=0)
    frm = to - datetime.timedelta(days=days) + datetime.timedelta(minutes=10)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return frm.strftime(fmt), to.strftime(fmt)


def request_headers(token):
    headers = {
        "X-EBAY-API-CALL-NAME": "GetOrders",
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
        "Content-Type": "text/xml",
    }
    if is_oauth(token):
        headers["X-EBAY-API-IAF-TOKEN"] = token
    return headers


def request_xml(token, days, page, now):
    """One GetOrders page of the operator's purchases, as the XML body."""
    frm, to = window(days, now)
    creds = ("" if is_oauth(token) else
             f"<RequesterCredentials><eBayAuthToken>{escape(token)}</eBayAuthToken>"
             f"</RequesterCredentials>")
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<GetOrdersRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"{creds}"
            "<DetailLevel>ReturnAll</DetailLevel>"
            "<OrderRole>Buyer</OrderRole>"
            "<OrderStatus>All</OrderStatus>"
            f"<CreateTimeFrom>{frm}</CreateTimeFrom>"
            f"<CreateTimeTo>{to}</CreateTimeTo>"
            f"<Pagination><EntriesPerPage>{ENTRIES_PER_PAGE}</EntriesPerPage>"
            f"<PageNumber>{page}</PageNumber></Pagination>"
            "</GetOrdersRequest>")


def _text(node, path):
    found = node.find(path, _NS) if node is not None else None
    return (found.text or "").strip() if found is not None and found.text else ""


def _money(node, path):
    try:
        return float(_text(node, path))
    except ValueError:
        return ""


def _order_tax(order, txs):
    """The order's sales tax: the tax eBay collects, summed over its line items;
    else the seller-collected SalesTax on the order; else ""."""
    amounts = []
    for tx in txs:
        for path in _TAX_PATHS:
            amount = _money(tx, path)
            if amount != "":
                amounts.append(amount)
                break
    if amounts:
        return round(sum(amounts), 2)
    return _money(order, "e:ShippingDetails/e:SalesTax/e:SalesTaxAmount")


def _order_money(order, txs):
    """What the order cost, from eBay's order-level figures (module docstring)."""
    return {"order_subtotal": _money(order, "e:Subtotal"),
            "order_shipping": _money(order, "e:ShippingServiceSelected/e:ShippingServiceCost"),
            "order_tax": _order_tax(order, txs),
            "order_total": _money(order, "e:Total")}


def parse_page(xml_text):
    """``(line_items, has_more)`` from one GetOrders response.

    One dict per transaction (line item).  A combined order has several, each
    carrying the order's money; ``merge`` keeps it on the first line only.
    Raises PurchasesError on Ack=Failure with eBay's own message; a Warning is
    not a failure.
    """
    root = ET.fromstring(xml_text)
    if _text(root, "e:Ack") == "Failure":
        errors = [f"{_text(e, 'e:ErrorCode')} {_text(e, 'e:LongMessage') or _text(e, 'e:ShortMessage')}".strip()
                  for e in root.findall("e:Errors", _NS)
                  if _text(e, "e:SeverityCode") != "Warning"]
        raise PurchasesError("; ".join(errors) or "GetOrders failed")

    lines = []
    for order in root.findall("e:OrderArray/e:Order", _NS):
        txs = order.findall("e:TransactionArray/e:Transaction", _NS)
        money = _order_money(order, txs)
        for tx in txs:
            item_id = _text(tx, "e:Item/e:ItemID")
            line_id = _text(tx, "e:OrderLineItemID") or (
                f"{item_id}-{_text(tx, 'e:TransactionID')}" if item_id else "")
            if not line_id:
                continue
            try:
                qty = int(_text(tx, "e:QuantityPurchased"))
            except ValueError:
                qty = ""
            lines.append({
                "purchased_at": _text(order, "e:CreatedTime"),
                "seller": _text(order, "e:SellerUserID"),
                "ebay_id": item_id,
                "title": _text(tx, "e:Item/e:Title"),
                "quantity": qty,
                "item_price": _money(tx, "e:TransactionPrice"),
                **money,
                "order_status": _text(order, "e:OrderStatus"),
                "order_id": _text(order, "e:OrderID"),
                "line_item_id": line_id,
            })
    return lines, _text(root, "e:HasMoreOrders").lower() == "true"


def row(line, pulled_at):
    """One sheet row, in HEADER order."""
    number = price_log.ebay_item_number(line.get("ebay_id")) or line.get("ebay_id") or ""
    values = dict(line, ebay_id=number, pulled_at=pulled_at,
                  listing_url=f"https://www.ebay.com/itm/{number}" if number else "")
    return [values.get(col, "") for col in HEADER]


def merge(existing, new):
    """Every row keyed by line item: ``new`` replaces what it repeats and the
    rest of ``existing`` stays.

    Newest purchase first, an order's lines together, and the order's money on
    its first line only, so summing a column counts each order once.
    """
    by_key = {}
    for r in existing:
        if len(r) > KEY and r[KEY]:
            by_key[r[KEY]] = list(r) + [""] * (len(HEADER) - len(r))
    for r in new:
        by_key[r[KEY]] = list(r)
    rows = sorted(by_key.values(), key=lambda r: (str(r[WHEN]), str(r[ORDER])), reverse=True)
    seen = set()
    for r in rows:
        if r[ORDER] in seen:
            for i in ORDER_MONEY:
                r[i] = ""
        elif r[ORDER]:
            seen.add(r[ORDER])
    return rows


def _relaid(r, header):
    """A row read under ``header`` (the tab's own first row), in HEADER's
    column order: a column HEADER no longer has is dropped, a new one is blank."""
    by_col = dict(zip(header, r))
    return [by_col.get(c, "") for c in HEADER]


def write_tab(gspread_client, spreadsheet_id, new_rows):
    """Merge ``new_rows`` into the ``purchases`` tab.

    Returns ``(title, added, total)``: the tab's title, how many line items
    were new to it, and how many it now holds.  RAW, so an item or order number
    stays text.  The kept rows are read unformatted, so a price goes back as a
    number and still sums.  Raises on failure; the caller reports it.
    """
    ss = gspread_client.open_by_key(price_log._spreadsheet_key(spreadsheet_id))
    ws = {w.title: w for w in ss.worksheets()}.get(TAB)
    existing, made = [], ws is None
    if made:
        ws = ss.add_worksheet(title=TAB, rows=max(100, len(new_rows) + 10), cols=len(HEADER))
    else:
        values = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
        header = values[0] if values else HEADER
        existing = [_relaid(r, header) for r in values[1:] if any(r)]
    known = {r[KEY] for r in existing}
    merged = merge(existing, new_rows)
    if not made and (ws.row_count < len(merged) + 1 or ws.col_count < len(HEADER)):
        ws.resize(rows=max(ws.row_count, len(merged) + 1), cols=max(ws.col_count, len(HEADER)))
    ws.clear()
    ws.update(values=[HEADER] + merged, range_name="A1", value_input_option="RAW")
    return TAB, sum(1 for r in new_rows if r[KEY] not in known), len(merged)


def summary_text(days, pulled, added, total, error=None):
    """The Slack line for one pull."""
    if error:
        return f"⚠️ Purchases pull failed: {error}"
    return (f"🧾 Pulled {pulled} purchase line item{'s' if pulled != 1 else ''} from "
            f"the last {days} days into the `{TAB}` tab of the Logger sheet: "
            f"{added} new, {total} in the tab. It also runs by itself every "
            f"Monday with the 9 AM scan.")
