"""
ebayscout/shipping_backfill.py

``/crawl shipping`` (2026-10-13): a one-time pull of the shipping each already
logged listing charged, into the blank ``shipping`` cells of its price_log rows.

From 2026-10-13 the scan writes a listing's shipping on its row as it reads the
lot (``ebay_client.shipping_cost``), and a ``/scout sold`` entered without
shipping takes it from there (price_log.log_sale), because a sold item's page
stops showing what shipping cost.  The rows written before that have none.
This fills them.

The Browse API returns active listings only, and most of those rows are sold or
ended by now, so it asks the Trading API's ``GetItem`` instead, which still
answers for an item ended up to about 90 days ago — and price_log began on
2026-09-30.  It uses the operator's user token, as ``/crawl purchases`` does.

- One ``GetItem`` per item number with a blank listing row (about 900 the first
  time; the Trading API allows 5,000 calls a day), then one batch of cell
  writes.  No photos, no Gemini.
- Only listing rows, and only blank cells: a sold row's shipping is what the
  operator entered, and a cell already filled is left alone.  So a second run
  costs one sheet read and the calls for whatever is still blank.
- An item eBay no longer answers for, or whose domestic shipping is calculated
  (eBay prices it only for a buyer's location), stays blank and is counted.

Pure apart from the injected worksheet; the HTTP call lives in
``ebay_client.get_item_shipping``.
"""

import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from . import price_log
from .purchases import COMPATIBILITY_LEVEL, is_oauth

COMMAND = "shipping"
WRITE_CHUNK = 500        # cells per batch_update

_NS = {"e": "urn:ebay:apis:eBLBaseComponents"}


class ItemUnavailable(RuntimeError):
    """eBay answered GetItem with Ack=Failure for this item (ended too long
    ago, removed, or a bad id)."""


def parse_command(text):
    """True for ``/crawl shipping``; anything else keeps its meaning."""
    return bool(re.match(r"(?i)^\s*shipping\s*$", text or ""))


def request_headers(token):
    headers = {
        "X-EBAY-API-CALL-NAME": "GetItem",
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
        "Content-Type": "text/xml",
    }
    if is_oauth(token):
        headers["X-EBAY-API-IAF-TOKEN"] = token
    return headers


def request_xml(token, item_id):
    creds = ("" if is_oauth(token) else
             f"<RequesterCredentials><eBayAuthToken>{escape(token)}</eBayAuthToken>"
             f"</RequesterCredentials>")
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"{creds}"
            f"<ItemID>{escape(str(item_id))}</ItemID>"
            "<IncludeItemSpecifics>false</IncludeItemSpecifics>"
            "</GetItemRequest>")


def _text(node, path):
    found = node.find(path, _NS) if node is not None else None
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_item(xml_text):
    """The cheapest domestic shipping a GetItem response quotes, or None.

    0.0 for free shipping.  None when the domestic shipping is calculated (it
    has no price without a buyer location) or no option states a cost.
    Raises ItemUnavailable on Ack=Failure.
    """
    root = ET.fromstring(xml_text)
    if _text(root, "e:Ack") == "Failure":
        errors = [f"{_text(e, 'e:ErrorCode')} {_text(e, 'e:ShortMessage')}".strip()
                  for e in root.findall("e:Errors", _NS)
                  if _text(e, "e:SeverityCode") != "Warning"]
        raise ItemUnavailable("; ".join(errors) or "GetItem failed")
    details = root.find("e:Item/e:ShippingDetails", _NS)
    if details is None:
        return None
    if _text(details, "e:ShippingType").startswith("Calculated"):
        return None
    costs = []
    for opt in details.findall("e:ShippingServiceOptions", _NS):
        if _text(opt, "e:FreeShipping").lower() == "true":
            costs.append(0.0)
            continue
        try:
            costs.append(float(_text(opt, "e:ShippingServiceCost")))
        except ValueError:
            continue
    return min(costs) if costs else None


def blank_listing_rows(bd_rows, ship_rows):
    """{item number: [sheet row, ...]} for every listing row with no shipping.

    Columns as the Sheets API returns them (price_log.live_sales): kind..ebay_id
    and shipping, row 1 the header, trailing blanks trimmed.
    """
    out = {}
    for i, r in enumerate(bd_rows or []):
        if i == 0:
            continue
        kind = r[0] if len(r) > 0 else ""
        eid = str(r[2]).strip() if len(r) > 2 else ""
        ship = ship_rows[i][0] if i < len(ship_rows or []) and ship_rows[i] else ""
        if kind == price_log.KIND_LISTING and eid and str(ship).strip() == "":
            out.setdefault(eid, []).append(i + 1)
    return out


def cells(blanks, costs):
    """The batch_update cells writing each item's shipping into its blank rows.

    ``costs`` is {item number: shipping}; an item missing from it, or None,
    writes nothing.
    """
    letter = price_log.col("shipping")
    out = []
    for eid, rows in blanks.items():
        cost = costs.get(eid)
        if cost is None:
            continue
        out.extend({"range": f"{letter}{r}", "values": [[cost]]} for r in rows)
    return out


def summary_text(items, filled_items, filled_rows, no_quote, unavailable, error=None):
    """The Slack line for one run."""
    if error:
        return f"⚠️ Shipping backfill failed: {error}"
    if not items:
        return "🚚 Shipping backfill: every listing row in `price_log` already has shipping."
    return (f"🚚 Shipping backfill: asked eBay about {items} item(s) whose `price_log` "
            f"listing rows had no shipping. Filled {filled_rows} row(s) across "
            f"{filled_items} item(s). Left blank: {no_quote} with calculated or no "
            f"stated shipping, {unavailable} eBay no longer returns. A `/scout sold` "
            f"of any filled item now takes its shipping.")
