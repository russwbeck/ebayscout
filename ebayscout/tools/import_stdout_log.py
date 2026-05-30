"""
ebayscout/tools/import_stdout_log.py

Recover scan_log.jsonl-shaped records from a Cloud Run STDOUT export. Use this
to fold a run that produced no structured scan_log.jsonl (e.g. the May 2026
dry-run backfill, which ran on old code) into your data so the audit / market
tools can read it.

What the stdout `>>> TITLE:` lines carry (and therefore all we can recover):
    [needed S YEAR SLOGAN] [seller] title   -> needed hit, best_score=S
    [low-conf S]           [seller] title   -> button-like, no needed match
    [rejected S]           [seller] title   -> below the rejection floor

NOT in stdout (left null/empty): asking price, item_id, listing_url, photo/crop
counts, full year composition, buying format. So imported records are good for
SUPPLY / SCARCITY / COVERAGE history, but NOT for cost/button/year (no price).
Each record is tagged `"source"` and gets a synthetic item_id (hash of
seller+title) so downstream dedup still works; real scan records are untouched.

Input is either a gcloud JSON export (a list of {textPayload, timestamp, ...})
or a plain-text file with one log line per row.

Usage:
    # Whole export -> JSONL on stdout
    python -m ebayscout.tools.import_stdout_log --stdout downloadedlogs.json

    # Just the backfill window, written to a file you then append to your store
    python -m ebayscout.tools.import_stdout_log --stdout downloadedlogs.json \
        --since 2026-05-29T16:00 --source backfill-2026-05-29 --out backfill.jsonl
"""

import argparse
import hashlib
import json
import re
import sys

from ebayscout.utils import extract_years, extract_lot_count

_NEEDED = re.compile(r">>> TITLE: \[needed ([\d.]+) (\d{4}) (.*?)\] \[([^\]]*)\] (.*)")
_OTHER  = re.compile(r">>> TITLE: \[(low-conf|rejected) ([\d.]+)\] \[([^\]]*)\] (.*)")

_RANK = {"needed": 2, "low-conf": 1, "rejected": 0}


def _synth_id(seller: str, title: str) -> str:
    h = hashlib.md5(f"{seller}\x00{title}".encode("utf-8")).hexdigest()[:16]
    return f"backfill|{h}"


def _record(kind, score, year, slogan, seller, title, ts, source):
    """Build one scan_log-shaped record from a parsed TITLE line."""
    needed = kind == "needed"
    return {
        "ts":            ts,
        "item_id":       _synth_id(seller, title),
        "title":         title,
        "listing_url":   "",
        "seller":        seller,
        "asking":        None,          # not in stdout — unknown
        "currency":      "USD",
        "buying_options": [],
        "condition":     "",
        "bid_count":     None,
        "photos_scored": None,
        "crops_scored":  None,
        "title_count":   extract_lot_count(title),
        "title_years":   sorted(extract_years(title)),
        "year_counts":   {year: 1} if needed else {},
        "best_score":    round(float(score), 4),
        "top_matches":   ([{"year": year, "slogan": slogan,
                            "overall": round(float(score), 4)}] if needed else []),
        "best_needed":   ({"year": year, "slogan": slogan,
                           "overall": round(float(score), 4)} if needed else None),
        "needed_hit":    needed,
        "alerted":       needed,        # dry-run "would alert"
        "source":        source,
        "_kind":         kind,          # internal, used for dedup; dropped on write
    }


def parse_stdout_records(entries, source: str, since: str | None = None) -> list[dict]:
    """
    entries: iterable of (timestamp, textPayload). Returns scan_log-shaped
    records for every parseable TITLE line, optionally filtered to ts >= since
    (ISO-prefix string compare).
    """
    out = []
    for ts, payload in entries:
        if since and ts and ts < since:
            continue
        m = _NEEDED.match(payload)
        if m:
            score, year, slogan, seller, title = m.groups()
            out.append(_record("needed", score, year, slogan, seller, title, ts, source))
            continue
        m = _OTHER.match(payload)
        if m:
            kind, score, seller, title = m.groups()
            out.append(_record(kind, score, None, None, seller, title, ts, source))
    return out


def dedup_records(records: list[dict]) -> list[dict]:
    """
    Collapse cross-pass duplicates (same seller+title processed under multiple
    queries) to the best record: needed > low-conf > rejected, then highest
    best_score. Mirrors the live scan's item_id dedup, but keyed on seller+title
    since stdout has no item_id.
    """
    best: dict = {}
    for r in records:
        key = (r["seller"].lower(), r["title"].lower())
        cur = best.get(key)
        if cur is None or (_RANK[r["_kind"]], r["best_score"]) > (_RANK[cur["_kind"]], cur["best_score"]):
            best[key] = r
    return list(best.values())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_entries(path: str):
    """Yield (timestamp, textPayload) from a gcloud JSON export or plain text."""
    with open(path) as fh:
        head = fh.read(64).lstrip()
        fh.seek(0)
        if head.startswith("["):                      # gcloud JSON export
            for e in json.load(fh):
                yield (e.get("timestamp", ""), e.get("textPayload", "") or "")
        else:                                          # plain text, one line each
            for line in fh:
                yield ("", line.rstrip("\n"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stdout", required=True, help="Cloud Run stdout export (JSON or text).")
    ap.add_argument("--since", help="Keep only entries with timestamp >= this ISO prefix.")
    ap.add_argument("--source", default="stdout-import",
                    help="Provenance tag stamped on each record.")
    ap.add_argument("--out", help="Write JSONL here (default: stdout).")
    ap.add_argument("--no-dedup", action="store_true", help="Keep cross-pass duplicates.")
    args = ap.parse_args(argv)

    records = parse_stdout_records(_load_entries(args.stdout), args.source, args.since)
    n_raw = len(records)
    if not args.no_dedup:
        records = dedup_records(records)

    kinds = {"needed": 0, "low-conf": 0, "rejected": 0}
    for r in records:
        kinds[r["_kind"]] += 1
    for r in records:
        r.pop("_kind", None)                           # internal field

    sink = open(args.out, "w") if args.out else sys.stdout
    try:
        for r in records:
            sink.write(json.dumps(r) + "\n")
    finally:
        if args.out:
            sink.close()

    where = args.out or "stdout"
    print(f">>> imported {n_raw} TITLE lines -> {len(records)} records "
          f"(needed={kinds['needed']}, low-conf={kinds['low-conf']}, "
          f"rejected={kinds['rejected']}) -> {where}", file=sys.stderr)
    return 0


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
