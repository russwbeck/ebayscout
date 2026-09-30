"""
ebayscout/auction_watch.py

Follow the auctions the scan priced to their close, and record what they sold for.

Pure: no GCS, no eBay, no clock (callers pass ``now``).  main owns the calls;
price_log owns the rows.

The loop
--------
1. A pipeline lot that is an eBay AUCTION, with a price per button and at least
   one named button, goes on the watch list (``AUCTION_WATCH_BLOB``), keyed by
   its eBay item number and carrying the button breakdown the scan already
   made.  Nothing about the lot is looked at again.
2. ``/check-auctions`` — Cloud Scheduler, hourly — asks eBay about every entry
   that closes within SNAPSHOT_WINDOW and records its bid ("last seen"), and
   about every entry whose end time has passed:
     * eBay still shows it → its closing bid is final (price_basis "final");
     * eBay no longer shows it → the last bid seen stands (price_basis
       "last_seen"), because a bid in the last minutes is missed.
3. A close with at least one bid, and no reserve left unmet, becomes sold rows
   (price_log, source "auction") priced evenly over the lot's detected buttons.
   Anything else was unsold and is dropped.

The Browse API documents only live listings, so whether step 2 ever sees
"final" is an open question the first days of running answer
(ebay_client.get_auction_state prints eBay's status for every non-200).
"""

from __future__ import annotations

import datetime

from . import price_log as pl

# Checks run hourly, so looking at everything that closes within 75 minutes
# means the last look before a close is 0–60 minutes out.
SNAPSHOT_WINDOW = datetime.timedelta(minutes=75)
# eBay errors on an entry this long after its close: stop asking.
GIVE_UP_AFTER = datetime.timedelta(days=2)
# Bounds one request's eBay calls; the rest wait an hour.
MAX_CHECKS_PER_RUN = 60


def parse_time(value) -> datetime.datetime | None:
    """eBay's ISO-8601 UTC time ("2026-09-22T18:04:11.000Z"), or None."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        t = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def _iso(t: datetime.datetime) -> str:
    return t.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


def entry_for_lot(record: dict, ctx: dict) -> dict | None:
    """The watch entry for a priced pipeline lot, or None when it is not an
    auction worth watching.

    ``record`` is the lot's scan_log record (with scan_log.button_price_fields);
    ``ctx`` the pending context the feed saved (buying_options, end_date).
    """
    if pl.listing_format(ctx.get("buying_options")) != pl.FORMAT_AUCTION:
        return None
    if not record.get("buttons") or not record.get("buttons_detected"):
        return None
    ebay_id = (pl.ebay_item_number(record.get("item_id"))
               or pl.ebay_item_number(record.get("listing_url")))
    if not ebay_id:
        return None
    return {
        "ebay_id":          ebay_id,
        "item_id":          record.get("item_id") or ebay_id,
        "title":            record.get("title", ""),
        "listing_url":      record.get("listing_url", ""),
        "end_date":         ctx.get("end_date"),
        "buttons_detected": record.get("buttons_detected"),
        "buttons":          record.get("buttons"),
        "asking":           record.get("asking"),
        "bids_at_scan":     ctx.get("bid_count"),
        "run_id":           record.get("run_id", ""),
        "added":            record.get("ts"),
        "last_seen":        None,
    }


def due(entry: dict, now: datetime.datetime) -> str:
    """"settle" once the end has passed, "snapshot" inside the window before it
    (or when the end is unknown — the look learns it), else "wait"."""
    end = parse_time(entry.get("end_date"))
    if end is None:
        return "snapshot"
    if now >= end:
        return "settle"
    if end - now <= SNAPSHOT_WINDOW:
        return "snapshot"
    return "wait"


def apply_state(entry: dict, state: dict, now: datetime.datetime) -> str:
    """Fold one look at eBay into ``entry`` (in place) and say what happens.

    Returns "keep" (look again later), "sold" (``entry["result"]`` holds the
    sale), "unsold" (closed with no bid, or reserve not met), or "drop" (nothing
    more to learn: it vanished before its end, or eBay could not be asked for
    GIVE_UP_AFTER past it).
    """
    status = (state or {}).get("status")
    if status == "live":
        if state.get("end_date"):
            entry["end_date"] = state["end_date"]
        entry["last_seen"] = {"ts": _iso(now), "bid": state.get("current_bid"),
                              "bids": state.get("bid_count") or 0,
                              "reserve_met": state.get("reserve_met")}
        end = parse_time(entry.get("end_date"))
        if end is not None and now >= end:
            return _close(entry, pl.BASIS_FINAL)
        return "keep"
    end = parse_time(entry.get("end_date"))
    if status == "gone":
        if end is not None and now >= end and entry.get("last_seen"):
            return _close(entry, pl.BASIS_LAST_SEEN)
        return "drop"          # ended early, pulled, or closed before we ever saw a bid
    if end is not None and now - end > GIVE_UP_AFTER:
        return "drop"
    return "keep"


def expired(entry: dict, now: datetime.datetime) -> bool:
    """True GIVE_UP_AFTER past the close: a sale that still could not be written
    by then is dropped rather than retried forever."""
    end = parse_time(entry.get("end_date"))
    return end is not None and now - end > GIVE_UP_AFTER


def _close(entry: dict, basis: str) -> str:
    seen = entry.get("last_seen") or {}
    bid, bids = seen.get("bid"), int(seen.get("bids") or 0)
    if bids > 0 and isinstance(bid, (int, float)) and bid > 0 \
            and seen.get("reserve_met") is not False:
        entry["result"] = {"price": round(float(bid), 2), "bids": bids, "basis": basis}
        return "sold"
    return "unsold"


def sold_rows(entry: dict, ts: str | None = None) -> list[list]:
    """The sold rows for a settled entry: the close spread evenly over every
    button the scan detected, one row per named button."""
    result = entry.get("result") or {}
    counts = pl.counts_from_buttons(entry.get("buttons"))
    if not result or not counts:
        return []
    try:
        prices = pl.split_sale(result["price"], entry.get("buttons_detected"), counts)
    except ValueError:
        return []
    end = parse_time(entry.get("end_date"))
    return pl.lot_rows(
        kind=pl.KIND_SOLD, source=pl.SOURCE_AUCTION, ebay_id=entry.get("ebay_id"),
        counts=counts, prices=prices, buttons_in_lot=entry.get("buttons_detected"),
        lot_price=result["price"], allocation="even", sale_format=pl.FORMAT_AUCTION,
        bids=result.get("bids"), sale_date=end.date().isoformat() if end else "",
        price_basis=result.get("basis", ""), title=entry.get("title"),
        listing_url=entry.get("listing_url"), run_id=entry.get("run_id"), ts=ts,
    )


def order(watch: dict) -> list[str]:
    """Entries soonest-closing first (unknown ends first, to learn them), so a
    run capped at MAX_CHECKS_PER_RUN spends its calls where a close is near."""
    far = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)

    def key(eid):
        end = parse_time((watch.get(eid) or {}).get("end_date"))
        return (end is not None, end or far, eid)

    return sorted(watch, key=key)
