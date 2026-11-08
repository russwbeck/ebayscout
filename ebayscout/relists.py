"""
ebayscout/relists.py

A relisted lot is recognised, not run again (operator, 2026-11-08).

Sellers relist the same lot every week or so, and eBay gives each relisting a
new item number, so the seen-list never matches it and buttonmatcher asked the
operator to count and confirm the same buttons again (rmn-vintage's 1999 Tire
Out Akron: 10-17, 10-30, 11-06; westmidwest1's Cincinnati, Ho Ho: 10-23, 11-01,
11-07).  Before a lot is handed over, it is matched against the lots already
confirmed in price_log:

- **the same photo**: eBay keeps a picture's id (``.../images/g/<id>/...``,
  and ``:g:<id>`` in a listing URL) when the seller relists with it, and no two
  listings share one otherwise; or
- **the same seller and the same title**, once its trailing stock code is set
  aside (westmidwest1 numbers its listings "... Pin Back Button P117" and
  takes a new photo each time).

A match is not handed over.  It is marked seen and gets one price_log row
(source ``relist``, its seller, price and title, run_id ``relist-of-<earlier
item number>``), and the day's Slack line lists it with a link, so a wrong
match is in plain sight.  Its buttons are the earlier lot's rows, joined on
that item number; they are not written again, so the averages count a lot once
however often it is relisted.

Only lots someone finished count as confirmed (button rows with a year and a
slogan, not superseded).  A lot that was handed over and never finished is
handed over again: it has nothing to reuse.

Pure: the caller reads price_log's columns and does the writes.
"""

from __future__ import annotations

import re

from . import price_log

SOURCE_RELIST = "relist"

_PHOTO_RES = (re.compile(r"/images/g/([^/?#]+)/"), re.compile(r":g:([^&|#\s]+)"))
# A seller's own stock code at the end of a title: P117, A12, #45.
_STOCK_CODE = re.compile(r"(?:\s+(?:#\s*\d{1,5}|[A-Za-z]{1,2}-?\d{1,5}))+\s*$")

# price_log columns read, in order (one batch_get)
COLUMNS = ("kind", "source", "ebay_id", "year", "slogan", "superseded",
           "title", "listing_url", "seller")


def photo_id(*urls) -> str:
    """eBay's picture id from a picture URL or a listing URL, or ""."""
    for u in urls:
        for p in _PHOTO_RES:
            m = p.search(str(u or ""))
            if m:
                return m.group(1)
    return ""


def title_key(title) -> str:
    """A title without its trailing stock code, case, or punctuation."""
    t = _STOCK_CODE.sub("", str(title or ""))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t.lower()).split())


def ranges() -> list[str]:
    """The A1 ranges ``index`` reads, in COLUMNS order."""
    return [f"{price_log.col(c)}:{price_log.col(c)}" for c in COLUMNS]


def _cell(cols, name, i):
    col = cols[COLUMNS.index(name)] or []
    row = col[i] if i < len(col) else []
    return str(row[0]).strip() if row else ""


def index(cols) -> dict:
    """{key: earlier item number} over the confirmed lots in price_log.

    ``cols`` is the batch_get of ``ranges()``: one column each, header first,
    trailing empty cells and rows trimmed as the API returns them.  Keys are
    ``photo:<id>`` and ``title:<seller>|<title_key>``; a later lot wins, so a
    relist points at the latest confirmation."""
    n = max((len(c or []) for c in cols), default=0)
    out = {}
    for i in range(1, n):
        g = lambda name: _cell(cols, name, i)
        if (g("kind") != price_log.KIND_LISTING or g("superseded") == "yes"
                or not g("year") or not g("slogan") or not g("ebay_id")):
            continue
        item = g("ebay_id")
        pid = photo_id(g("listing_url"))
        if pid:
            out[f"photo:{pid}"] = item
        seller, tk = g("seller").lower(), title_key(g("title"))
        if seller and tk:
            out[f"title:{seller}|{tk}"] = item
    return out


def match(listing: dict, idx: dict, picture_urls=()) -> str:
    """The item number of the confirmed lot ``listing`` relists, or ""."""
    own = price_log.ebay_item_number(listing.get("item_id"))
    pid = photo_id(*(list(picture_urls or []) + [listing.get("gallery_url"),
                                                  listing.get("listing_url"),
                                                  listing.get("url")]))
    hits = [idx.get(f"photo:{pid}") if pid else None]
    seller, tk = str(listing.get("seller") or "").lower(), title_key(listing.get("title"))
    if seller and tk:
        hits.append(idx.get(f"title:{seller}|{tk}"))
    for h in hits:
        if h and h != own:
            return h
    return ""


def relist_record(listing: dict, earlier: str) -> dict:
    """The record of a relist's one price_log row (``price_log.handoff_row``
    with source ``relist``)."""
    return {
        "item_id":        listing.get("item_id"),
        "title":          listing.get("title"),
        "listing_url":    listing.get("listing_url") or listing.get("url"),
        "asking":         listing.get("current_price"),
        "shipping":       listing.get("shipping_cost"),
        "buying_options": listing.get("buying_options") or [],
        "bid_count":      listing.get("bid_count"),
        "seller":         listing.get("seller"),
        "condition":      listing.get("condition"),
        "run_id":         f"relist-of-{earlier}",
        "source":         SOURCE_RELIST,
    }


def slack_lines(relists, limit: int = 15) -> str:
    """The day's relists, for the feed's Slack line: title, seller, price and
    the earlier item, each linked."""
    if not relists:
        return ""
    out = []
    for rec in relists[:limit]:
        title = str(rec.get("title") or "(no title)").replace("<", "").replace(">", "")[:80]
        url = rec.get("listing_url") or ""
        price = rec.get("asking")
        price = f"${float(price):,.2f}" if isinstance(price, (int, float)) else "?"
        was = rec["run_id"].replace("relist-of-", "")
        out.append(f"• <{url}|{title}> · {rec.get('seller') or '?'} · {price} — "
                   f"same lot as <https://www.ebay.com/itm/{was}|{was}>")
    more = len(relists) - limit
    if more > 0:
        out.append(f"…and {more} more")
    return "\n".join(out)
