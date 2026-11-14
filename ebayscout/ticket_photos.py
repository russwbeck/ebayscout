"""
ebayscout/ticket_photos.py

``/crawl tickets``: the first photo of every eBay listing a ticket search
finds, saved for training (2026-11-14).

The operator wants to know whether the button process can learn Penn State
ticket stubs.  A first look (buttonmatcher HANDOFF, 2026-11-14) found that OCR
plus the game list names the game, and that the detector, trained on round
buttons, never boxes a stub.  Teaching it needs photos of stubs with their
boxes drawn, and the daily search only asks for buttons.  This pulls the
photos; drawing the boxes is a separate, offline job.

``/crawl tickets`` asks the Browse API for ``QUERIES`` (a few pages of
summaries: no getItem), downloads each listing's first photo at full size and
writes::

    ebay_scout/tickets/photos/<item number>.jpg
    ebay_scout/tickets/index.json   {"<item number>": {title, price, url,
                                     image_url, photo, pulled_at}}

A photo already in the bucket is not downloaded again, and the index is merged
with the one already there, so a later pull only adds what is new and a listing
that has ended keeps its photo.  The index also goes to #ebay-checker as a
file, where a web session (no GCS access) can read it.

Prices (operator, 2026-11-14: "the same pricing and sales data as the
buttons").  Every listing the search finds also gets one row in ``price_log``
in the Logger workbook, like a ``/crawl seller`` row: kind ``listing``, source
``ticket``, the lot's price, shipping, format, bids, seller, condition, title
and link, no year or slogan (nothing is identified yet, so it never enters a
per-slogan average), run_id ``tickets-<pulled_at>``.  Each pull appends, so a
later pull is a later price for the same item.  What is not captured: which
game a ticket is from and a price per ticket (nothing identifies or counts
them yet), and sales (eBay's search gives none; button sales are entered by
the operator through buttonmatcher's /scout sold, which only knows buttons).

It is cheap on purpose: a few search pages, one photo per new listing and one
sheet write.  Nothing goes to CLIP or the detector, nothing is handed to
buttonmatcher and nothing is marked seen, so these listings never reach the
deal scan through here.  It only runs when someone types it.

Pure: the caller does the eBay calls, the downloads, the GCS reads and writes
and the sheet write.
"""

import json
import re

from . import price_log
from . import seller_listings

# The operator's own search first (419 listings on 2026-11-14).  Results are
# deduplicated by item number, so a second wording only adds what it finds.
QUERIES = ("vintage penn state ticket",)
PAGE_SIZE = 200            # the Browse page maximum
MAX_PAGES = 5              # per query
MAX_ITEMS = 1000           # a guard on one run's downloads

PHOTO_PREFIX = "ebay_scout/tickets/photos/"
INDEX_BLOB = "ebay_scout/tickets/index.json"
INDEX_FILENAME = "ticket_photos_index.json"

SOURCE_TICKET = "ticket"   # price_log source of these rows

_SIZE = re.compile(r"/s-l\d+\.(jpg|jpeg|png|webp)(?=$|\?)", re.IGNORECASE)


def parse_command(text):
    """True for ``/crawl tickets``; anything else keeps its meaning."""
    return bool(re.match(r"(?i)^\s*tickets\s*$", text or ""))


def search_params(query, offset):
    """Browse item_summary/search params for one page of one query."""
    return {"q": query, "limit": str(PAGE_SIZE), "offset": str(offset)}


def item_number(item_id):
    """The listing's number from a Browse id (``v1|<number>|0``) or a bare
    number; "" for anything else, so it is always safe in a blob name."""
    s = str(item_id or "").strip()
    m = re.match(r"^v1\|(\d+)\|", s)
    if m:
        return m.group(1)
    return s if s.isdigit() else ""


def full_size(url):
    """The same eBay photo at its largest size.  A summary's image is a
    thumbnail (``…/s-l225.jpg``); ``s-l1600`` is the size a listing page
    shows.  A URL of any other shape is returned as it came."""
    return _SIZE.sub(lambda m: f"/s-l1600.{m.group(1)}", str(url or ""))


def photo_blob(number):
    """Where one listing's photo is kept."""
    return f"{PHOTO_PREFIX}{number}.jpg"


def record(item, pulled_at):
    """``(number, index record)`` from a Browse item summary, or None without
    an item number or a photo."""
    number = item_number(item.get("itemId"))
    image = (item.get("image") or {}).get("imageUrl") or ""
    if not image:
        thumbs = item.get("thumbnailImages") or []
        image = (thumbs[0] or {}).get("imageUrl") or "" if thumbs else ""
    if not number or not image:
        return None
    try:
        price = float((item.get("price") or {}).get("value"))
    except (TypeError, ValueError):
        price = None
    return number, {
        "title": str(item.get("title") or "")[:300],
        "price": price,
        "url": item.get("itemWebUrl") or "",
        "image_url": full_size(image),
        "thumb_url": image,
        "photo": photo_blob(number),
        "pulled_at": pulled_at,
    }


def collect(items, pulled_at, limit=MAX_ITEMS):
    """``{number: record}`` for the summaries, each listing once, in the order
    eBay gave them, at most ``limit``."""
    out = {}
    for item in items or []:
        got = record(item, pulled_at)
        if got and got[0] not in out:
            out[got[0]] = got[1]
            if len(out) >= limit:
                break
    return out


def price_row(item, pulled_at):
    """One price_log row (PRICE_HEADER order) from a Browse item summary, or
    None without an item number.  Shaped like ``seller_listings.summary_row``:
    calculated shipping goes in blank, year and slogan stay empty."""
    number = item_number(item.get("itemId"))
    if not number:
        return None
    try:
        price = float((item.get("price") or {}).get("value"))
    except (TypeError, ValueError):
        price = ""
    ship = seller_listings.shipping_cost(item)
    bids = item.get("bidCount")
    return [
        pulled_at, price_log.KIND_LISTING, SOURCE_TICKET, number,
        "", "", "", "",                     # year, slogan, n_in_lot, buttons_detected
        price, "" if ship == "calculated" else ship,
        "", "",                             # price_per_button, allocation
        price_log.listing_format(item.get("buyingOptions")),
        bids if isinstance(bids, int) else "",
        "", price_log.BASIS_ASKING, "no",   # sale_date, price_basis, superseded
        item.get("title") or "", item.get("itemWebUrl") or "",
        f"tickets-{pulled_at}",
        str((item.get("seller") or {}).get("username") or "").lower(),
        item.get("condition") or "",
    ]


def price_rows(items, pulled_at, limit=MAX_ITEMS):
    """A price_log row for each listing, each once, at most ``limit``."""
    rows, seen = [], set()
    for item in items or []:
        row = price_row(item, pulled_at)
        if row and row[3] not in seen:
            seen.add(row[3])
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows


def save_photos(records, exists, fetch, store):
    """Download and keep each record's photo.

    ``exists(blob)`` says a photo is already kept, ``fetch(url)`` returns its
    bytes and ``store(blob, data)`` keeps them.  The full-size photo is tried
    first and the thumbnail after it, since a rewritten URL is a guess.
    Returns ``(saved, kept, failed)``: numbers downloaded now, numbers already
    in the bucket, and numbers whose photo could not be had (left out of the
    index by the caller)."""
    saved, kept, failed = [], [], []
    for number, rec in records.items():
        blob = rec["photo"]
        try:
            if exists(blob):
                kept.append(number)
                continue
        except Exception:
            pass
        data = None
        for url in dict.fromkeys((rec["image_url"], rec["thumb_url"])):
            try:
                data = fetch(url)
            except Exception:
                data = None
            if data:
                rec["image_url"] = url
                break
        if not data:
            failed.append(number)
            continue
        try:
            store(blob, data)
            saved.append(number)
        except Exception:
            failed.append(number)
    return saved, kept, failed


def merge_index(old_text, records, failed):
    """The index as JSON text: what was there, plus this pull's records whose
    photo is in the bucket.  A listing already indexed keeps its first
    ``pulled_at``.  An unreadable old index is replaced."""
    try:
        index = json.loads(old_text) if old_text else {}
        if not isinstance(index, dict):
            index = {}
    except ValueError:
        index = {}
    gone = set(failed)
    for number, rec in records.items():
        if number in gone:
            continue
        rec = {k: v for k, v in rec.items() if k != "thumb_url"}
        if number in index and index[number].get("pulled_at"):
            rec["pulled_at"] = index[number]["pulled_at"]
        index[number] = rec
    return json.dumps(index, indent=1, sort_keys=True)


def summary_text(n_found, n_saved, n_kept, n_failed, n_index, n_prices=0,
                 error=None, price_error=None):
    """The line posted to #ebay-checker."""
    if error:
        return f"⚠️ `/crawl tickets` failed: {error}"
    if not n_found:
        return "🎟️ `/crawl tickets`: the search found no listings."
    text = (f"🎟️ `/crawl tickets`: {n_found} listings found; {n_saved} photos "
            f"saved, {n_kept} already there")
    if n_failed:
        text += f", {n_failed} could not be downloaded"
    text += f". {n_index} in `{INDEX_BLOB}`, photos under `{PHOTO_PREFIX}`. "
    if price_error:
        text += f"⚠️ The prices were not written to the Logger: {price_error}. "
    else:
        text += (f"{n_prices} price rows added to `{price_log.PRICE_TAB}` "
                 f"(source `{SOURCE_TICKET}`). ")
    return text + "Nothing was matched or marked seen."
