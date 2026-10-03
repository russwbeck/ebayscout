"""
ebayscout/catchup.py

`/crawl catchup [N]`: a one-time catch-up for the price log (2026-10-03).

price_log listing rows began on config.PRICE_LOG_START (2026-09-30).  A lot the
pipeline read before then was marked seen, and every search since skips it, so a
listing that is still for sale has no listing row and never will.

Which lots qualify
------------------
A listing the searches return now (the daily and /crawl phrases, several pages
deep) that is in seen_items, whose LAST seen mark is before PRICE_LOG_START, and
whose item number has no listing row in price_log.

The last-seen rule is what makes this one-time: a re-fed lot is marked seen again
today, so it never qualifies a second time, even when it confirms no button and
so writes no row.  Catch-up marks its lots seen when it FEEDS them (the daily
scan and /crawl mark on confirmation), so running the command twice before the
Gem answers cannot feed a lot twice and log its prices twice.  The price of that:
a lot the Gem never answers is not retried, which is acceptable for a one-time
catch-up.

What a catch-up lot does
------------------------
It runs the normal pipeline and writes its scan_log record (flagged
``"catchup": true``) and price_log rows, and it is marked seen.  It posts NO deal
alert (if it was a deal it was reported the first time), stages NO crop into
reference/_staging (those would be duplicates of crops already staged), and
writes no training label, crop vectors or match/confirm rows (a second copy of
the first pass would skew the statistics built on them).

Cost
----
One Gemini read plus CPU per lot fed.  `/crawl catchup` with no number feeds
nothing: it runs the searches (free Browse calls), reads the price_log ids once,
and says how many lots qualify, so the cost is known before anything is spent.

Pure: no GCS, no Sheets, no Slack.  main.py does the I/O.
"""

import re

from . import price_log

COMMAND = "catchup"


def parse_command(text):
    """Read the text after ``/crawl``.

    Returns ``(is_catchup, n, error)``:
      ``(False, None, None)`` for anything else (``/crawl 800`` keeps its meaning);
      ``(True, 0, None)`` for a bare ``catchup`` — count only, feed nothing;
      ``(True, n, None)`` for ``catchup 50``;
      ``(True, None, message)`` when the number is not one.
    """
    m = re.match(r"(?i)^\s*catch-?up\b\s*(.*?)\s*$", text or "")
    if not m:
        return False, None, None
    arg = m.group(1)
    if not arg:
        return True, 0, None
    if not re.fullmatch(r"\d+", arg):
        return True, None, (f"`/crawl catchup {arg}` — the number of lots to feed "
                            f"must be a whole number, e.g. `/crawl catchup 50`.")
    return True, int(arg), None


def last_seen_date(item_id, seen):
    """The ISO date of an item's most recent seen mark, or None.

    seen_items stores one date as a string and later marks as a list."""
    val = seen.get(item_id)
    if not val:
        return None
    if isinstance(val, str):
        return val
    dates = [d for d in val if isinstance(d, str) and d]
    return max(dates) if dates else None


def logged_listing_numbers(bd_rows):
    """Item numbers that already have a listing row, from price_log's
    kind..ebay_id columns (B:D) as the Sheets API returns them: row 1 is the
    header and trailing empty cells are trimmed."""
    out = set()
    for i, row in enumerate(bd_rows or []):
        if i == 0 or len(row) < 3:
            continue
        if row[0] == price_log.KIND_LISTING and str(row[2]).strip():
            out.add(str(row[2]).strip())
    return out


def item_number(listing):
    return (price_log.ebay_item_number(listing.get("item_id"))
            or price_log.ebay_item_number(listing.get("listing_url")))


def candidates(listings, seen, logged_numbers, cutoff):
    """The listings that qualify, in the order given (newest first per query).

    Unseen listings are left out: those are the daily scan's and /crawl's job.
    """
    out = []
    for listing in listings:
        iid = listing.get("item_id")
        if not iid or iid not in seen:
            continue
        last = last_seen_date(iid, seen)
        if last is None or last >= cutoff:
            continue
        number = item_number(listing)
        if number and number in logged_numbers:
            continue
        out.append(listing)
    return out


def preview_text(n_qualify, n_found, cap):
    """The Slack line for a bare `/crawl catchup` (nothing fed)."""
    if not n_qualify:
        return (f"🧾 `/crawl catchup`: none of the {n_found} listing(s) the searches "
                f"found need a catch-up. Nothing to feed.")
    return (f"🧾 `/crawl catchup`: {n_qualify} still-listed lot(s) were read before "
            f"price logging began and have no price row (of {n_found} found). "
            f"Each costs one Gemini read. Run `/crawl catchup <N>` to feed up to N "
            f"(max {cap}); they post no deal alerts and stage no crops.")


def summary_text(fed, n_picked, n_qualify):
    """The Slack line after a catch-up feed."""
    left = max(0, n_qualify - n_picked)
    tail = (f" {left} more qualify; run `/crawl catchup` again to see them."
            if left else " That was all of them.")
    return (f"🧾 `/crawl catchup`: fed {fed}/{n_picked} lot(s) into the Gemini "
            f"pipeline for their price rows (no alerts, no crop staging).{tail}")
