"""
ebayscout/scan_log.py

Where a scan-log record belongs, and how to read a partitioned log back.

Pure: no GCS, no config, no I/O beyond opening the paths a tool hands it.
seen_items.py owns the blob calls, the tools own their CLIs; this owns the
decisions both have to agree on — which file a record goes in, and what a lot's
per-button price is (``button_price_fields``, written by main, averaged by
tools/market_report.py).

Why partitioned
---------------
GCS has no append.  The log used to be ONE blob, so every processed lot
downloaded the whole thing and re-uploaded it with a line added: cost linear in
the log's size per lot, growing forever, and serialized behind a lock so two
Gem results could not clobber each other.  A year of daily feeds makes every
single lot pay for every lot before it.

Writing ``scan_log/YYYY-MM.jsonl`` bounds that at one month.  Nothing else
changes: the records are the same JSON lines in the same order, the month is
read from the record's own ``ts`` (not from the clock at write time), so a
backfill of old records lands in the months it belongs to rather than in
whichever month it was replayed.
"""

from __future__ import annotations

import json
import os

# A record whose ts is missing or unparseable still has to go somewhere, and it
# must be somewhere a reader will look.  Dropping it would lose an observation
# to a formatting problem.
UNDATED = "undated"


def month_of(record: dict) -> str:
    """The ``YYYY-MM`` partition one record belongs to, from its own ``ts``.

    ``ts`` is written as an ISO-8601 UTC string by main._scan_log_record, so the
    first seven characters are the month.  Anything that does not look like one
    is UNDATED rather than an error: the record is data we already paid an eBay
    call and a CLIP pass for.
    """
    ts = record.get("ts") if isinstance(record, dict) else None
    if not isinstance(ts, str) or len(ts) < 7:
        return UNDATED
    head = ts[:7]
    if len(head) == 7 and head[4] == "-" and head[:4].isdigit() and head[5:].isdigit():
        return head
    return UNDATED


def partition(records) -> dict[str, list[dict]]:
    """Group records by month, each group keeping the order it arrived in.

    An ordinary write is one lot's single record and therefore one group; a
    checkpointed backfill can span months and then touches one blob per month.
    """
    out: dict[str, list[dict]] = {}
    for r in records:
        out.setdefault(month_of(r), []).append(r)
    return out


def blob_name(month: str, prefix: str) -> str:
    """The object name for one month's partition: ``<prefix><month>.jsonl``."""
    return f"{prefix}{month}.jsonl"


def appended_text(existing: str, records) -> str:
    """``existing`` with ``records`` added as JSON lines.

    A log whose last line has no newline (a truncated upload, or a file edited
    by hand) must not have the next record welded onto it.
    """
    if existing and not existing.endswith("\n"):
        existing += "\n"
    return existing + "".join(json.dumps(r) + "\n" for r in records)


# --- per-button price ------------------------------------------------------------

def button_price_fields(asking, buttons_detected: int, confirmed) -> dict:
    """The per-button price fields of one lot's record.

    ``price_per_button`` is the asking price over EVERY button detected in the
    photo, named or not: a lot of 20 where we could name 12 still sold 20
    buttons for that price.  Dividing by the confirmed count instead would
    overstate it.  None when there is no price or nothing was detected, so a
    report never averages in a zero it did not observe.

    ``buttons`` is every confirmed button with its count in this lot.  The
    record's ``top_matches`` keeps only the best five, so without this a
    button's price across lots could only be read off five buttons per lot.
    ``year_counts`` has every crop but no slogan.
    """
    try:
        ask = float(asking or 0.0)
    except (TypeError, ValueError):
        ask = 0.0
    n = int(buttons_detected or 0)
    per = round(ask / n, 2) if ask > 0 and n > 0 else None

    counts: dict[tuple, int] = {}
    for m in confirmed or []:
        if m.get("year") is None or not m.get("slogan"):
            continue
        k = (m["year"], m["slogan"])
        counts[k] = counts.get(k, 0) + 1
    buttons = [{"year": y, "slogan": s, "n": c}
               for (y, s), c in sorted(counts.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))]
    return {"buttons_detected": n, "price_per_button": per, "buttons": buttons}


# --- reading a partitioned log back ---------------------------------------------

def expand_paths(paths) -> list[str]:
    """Turn what an operator typed into the list of files to read.

    A directory becomes its ``*.jsonl`` files, sorted — which for ``YYYY-MM``
    names is chronological order, so a reader sees the log as one stream in the
    order it was written.  A file is itself.  The single legacy
    ``scan_log.jsonl`` is just a file, so ``--scan-log old.jsonl months/``
    reads the whole history.
    """
    out: list[str] = []
    for p in ([paths] if isinstance(paths, str) else list(paths)):
        if os.path.isdir(p):
            out.extend(sorted(os.path.join(p, n) for n in os.listdir(p)
                              if n.endswith(".jsonl")))
        else:
            out.append(p)
    return out


def load(paths) -> list[dict]:
    """Every record in ``paths`` (files and/or directories), in file order.

    A malformed line is skipped rather than ending the read: these logs are
    assembled from months of appends and one bad line must not hide the rest.
    """
    records: list[dict] = []
    for path in expand_paths(paths):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    return records
