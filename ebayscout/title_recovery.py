"""
ebayscout/title_recovery.py

``/crawl titles``: the titles of lots already handed to buttonmatcher, looked
up again on eBay (2026-11-11).

The operator answers every handed-over lot (a count, "Button, skip" or "Not a
button"), and buttonmatcher keeps each answer with the lot's photo in a label
sidecar.  A filter for the lots that are not buttons is to be trained on the
photo and the title (buttonmatcher's tools/ebay_triage.py), and until
2026-11-11 the sidecar had no title: it was only shown in Slack.

What ties an old answer to its listing is one line buttonmatcher logs for
every lot it takes::

    >>> EBAY_LOT: v1|237173788601|0 -> Scout job ed9f0edd-0e4c-...

The operator saves those lines from Cloud Logging into
``gs://<bucket>/ebay_scout/title_jobs.txt``.  This reads them, asks eBay for
each item that still exists (Browse getItem: one call an item, no search, no
photo, no CLIP, nothing handed to buttonmatcher, nothing marked seen), and
writes ``ebay_scout/titles.json``::

    {"<job_id>": {"item_id": "...", "title": "...", "condition": "..."}}

which tools/ebay_triage.py reads with ``--titles``.  A listing that has ended
gives no title; fixed-price listings stay up for months, so the clothing and
the enamel pins mostly come back.  The seller is not written: the filter must
never use it (operator, 2026-11-11).

Pure: the caller does the GCS reads and writes and the eBay calls.
"""

import re

JOBS_BLOB = "ebay_scout/title_jobs.txt"
TITLES_BLOB = "ebay_scout/titles.json"
MAX_ITEMS = 5000           # a guard against a wrong file, far above a month of lots

_LINE = re.compile(r"EBAY_LOT:\s*(\S+)\s*->\s*Scout job\s+([0-9A-Za-z-]+)")


def parse_command(text):
    """True for ``/crawl titles``; anything else keeps its meaning."""
    return bool(re.match(r"(?i)^\s*titles\s*$", text or ""))


def browse_id(item_id):
    """An item id as Browse getItem takes it: ``v1|<number>|0``.  A bare
    number is wrapped; anything else is passed as it came."""
    s = str(item_id or "").strip()
    return f"v1|{s}|0" if s.isdigit() else s


def parse_jobs(text, limit=MAX_ITEMS):
    """[(job_id, item_id)] from the saved log lines, each job once, in order.
    Lines that are not a hand-off line are ignored."""
    out, seen = [], set()
    for m in _LINE.finditer(text or ""):
        item, job = m.group(1), m.group(2)
        if job in seen or item in ("None", ""):
            continue
        seen.add(job)
        out.append((job, item))
        if len(out) >= limit:
            break
    return out


def collect(jobs, fetch):
    """``{job_id: {"item_id", "title", "condition"}}`` for every job whose
    item ``fetch(browse item id)`` still finds with a title.  One fetch per
    distinct item: a relisted photo can sit under two jobs."""
    cache, out = {}, {}
    for job, item in jobs:
        key = browse_id(item)
        if key not in cache:
            try:
                cache[key] = fetch(key) or {}
            except Exception:
                cache[key] = {}
        got = cache[key]
        title = str(got.get("title") or "").strip()
        if title:
            rec = {"item_id": item, "title": title[:300]}
            if got.get("condition"):
                rec["condition"] = str(got["condition"])
            out[job] = rec
    return out


def summary_text(n_jobs, n_titles, error=None):
    """The one line posted to #ebay-checker."""
    if error:
        return f"⚠️ `/crawl titles` failed: {error}"
    if not n_jobs:
        return (f"📋 `/crawl titles`: no hand-off lines in `{JOBS_BLOB}`. "
                f"Save buttonmatcher's EBAY_LOT log lines there first.")
    return (f"📋 `/crawl titles`: {n_titles} of {n_jobs} lots still have a "
            f"listing on eBay; their titles are in `{TITLES_BLOB}`.")
