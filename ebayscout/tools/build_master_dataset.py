"""
ebayscout/tools/build_master_dataset.py

Consolidate every scan/market data source into ONE master historical dataset —
so nothing we paid to collect gets split off and forgotten. Pulls together:

  - the live scan_log.jsonl (daily + hunt records: real IDs, prices, rich schema)
  - older daily logs on the previous schema (no year_counts/crops/format) — fields
    are derived from title + top_matches so they slot in cleanly
  - the recovered backfill records (synthetic IDs, NO price) from a run that wrote
    no scan_log (import_stdout_log.py)

Reconciliation (no double-counting, nothing dropped silently):
  - real listings dedupe by item_id;
  - a backfill record whose seller+title matches a real listing is FOLDED INTO
    that listing (the priced/real record supersedes it) instead of becoming a
    second row;
  - backfill records with no real match survive as their own rows — these are the
    items only ever seen in the big crawl (many since ended), which is exactly the
    history you'd otherwise lose.
  - every master row carries `sources` (where it came from) and `times_seen`, and
    `has_price` so price analysis can filter to the rows that support it.

Idempotent: re-run it over the (growing) sources anytime to regenerate the
master — it's derived, not appended, so it never drifts.

Usage:
    python -m ebayscout.tools.build_master_dataset \
        --sources scan_log.jsonl backfill_scan_log.jsonl \
        --out-jsonl master.jsonl --out-csv master.csv
"""

import argparse
import csv
import json
import re
from collections import defaultdict

from ebayscout.utils import extract_years, extract_lot_count

# Stable column order for the master CSV / sheet.
CSV_COLUMNS = [
    "observed_at", "sources", "times_seen", "item_id", "seller", "title",
    "asking", "has_price", "currency", "buying_format", "condition", "bid_count",
    "title_count", "crops_scored", "years", "best_score", "best_year",
    "best_slogan", "needed_hit", "listing_url",
]


def _norm_txt(s: str) -> str:
    return re.sub(r"[^\w\s]", "", (s or "").lower()).strip()


def _is_synthetic(item_id) -> bool:
    """Backfill/imported records use synthetic 'backfill|…' ids (no real eBay id)."""
    return (not item_id) or str(item_id).startswith("backfill|")


def _buying_format(opts) -> str:
    opts = opts or []
    return "auction" if "AUCTION" in opts else "fixed" if opts else "unknown"


def normalize_record(rec: dict) -> dict:
    """Map any-schema scan/market record to the canonical master shape, deriving
    year_counts / title_count / years from title + top_matches on old records."""
    title = rec.get("title", "") or ""
    yc = dict(rec.get("year_counts") or {})
    if not yc:
        for m in rec.get("top_matches") or []:
            y = str(m.get("year"))
            if y and y != "None":
                yc[y] = yc.get(y, 0) + 1
    title_years = rec.get("title_years")
    if title_years is None:
        title_years = sorted(extract_years(title))
    title_count = rec.get("title_count")
    if title_count is None:
        title_count = extract_lot_count(title)
    crops = rec.get("crops_scored")
    if crops is None:
        crops = len(rec.get("top_matches") or [])

    top = sorted((rec.get("top_matches") or []),
                 key=lambda m: m.get("overall", 0), reverse=True)
    best = rec.get("best_needed") or (top[0] if top else None) or {}
    asking = rec.get("asking")

    return {
        "observed_at":  rec.get("ts", "") or "",
        "item_id":      rec.get("item_id", "") or "",
        "seller":       rec.get("seller", "") or "",
        "title":        title,
        "asking":       asking,
        "has_price":    bool(asking and float(asking) > 0),
        "currency":     rec.get("currency", "USD") or "USD",
        "buying_format": _buying_format(rec.get("buying_options")),
        "condition":    rec.get("condition", "") or "",
        "bid_count":    rec.get("bid_count"),
        "title_count":  title_count,
        "crops_scored": crops,
        "years":        sorted(set(list(yc.keys()) + [str(y) for y in title_years])),
        "best_score":   rec.get("best_score"),
        "best_year":    best.get("year"),
        "best_slogan":  best.get("slogan"),
        "needed_hit":   bool(rec.get("needed_hit", False)),
        "listing_url":  rec.get("listing_url", "") or "",
        "source":       rec.get("source") or "scan",
    }


def _richness(r: dict) -> tuple:
    """Sort key for picking a group's representative: priced + real + newest wins."""
    return (
        r["has_price"],
        not _is_synthetic(r["item_id"]),
        r["buying_format"] != "unknown",
        r["observed_at"],
    )


def reconcile(records: list[dict]) -> list[dict]:
    """Collapse to one row per unique listing; fold synthetic backfill rows into a
    matching real listing; preserve unmatched backfill rows. Adds sources/
    times_seen and keeps the richest observation as the representative."""
    norm = [normalize_record(r) for r in records]

    # Map seller+title -> a real item_id, so synthetic rows can fold into reals.
    st_to_real: dict[str, str] = {}
    for r in norm:
        if not _is_synthetic(r["item_id"]):
            st_to_real.setdefault(_norm_txt(r["seller"]) + "|" + _norm_txt(r["title"]),
                                  r["item_id"])

    def key(r):
        if _is_synthetic(r["item_id"]):
            st = _norm_txt(r["seller"]) + "|" + _norm_txt(r["title"])
            rid = st_to_real.get(st)
            return ("id", rid) if rid else ("st", st)
        return ("id", r["item_id"])

    groups: dict = defaultdict(list)
    for r in norm:
        groups[key(r)].append(r)

    master = []
    for grp in groups.values():
        rep = dict(max(grp, key=_richness))
        rep["sources"] = ";".join(sorted({g["source"] for g in grp}))
        rep["times_seen"] = len(grp)
        master.append(rep)
    master.sort(key=lambda r: (r["observed_at"], r["item_id"]), reverse=True)
    return master


def to_csv_row(r: dict) -> dict:
    out = {c: r.get(c, "") for c in CSV_COLUMNS}
    out["years"] = ";".join(r.get("years") or [])
    out["has_price"] = "yes" if r.get("has_price") else "no"
    out["needed_hit"] = "yes" if r.get("needed_hit") else "no"
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load(path: str) -> list:
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", nargs="+", required=True,
                    help="One or more JSONL source files to consolidate.")
    ap.add_argument("--out-jsonl", help="Write the canonical master JSONL here.")
    ap.add_argument("--out-csv", help="Write the flat master CSV (sheet) here.")
    args = ap.parse_args(argv)

    records = []
    for path in args.sources:
        rows = _load(path)
        print(f">>> loaded {len(rows)} records from {path}")
        records.extend(rows)

    master = reconcile(records)
    priced = sum(1 for r in master if r["has_price"])
    print(f">>> master: {len(master)} unique listings from {len(records)} source rows "
          f"({priced} with price, {len(master) - priced} without).")

    if args.out_jsonl:
        with open(args.out_jsonl, "w") as fh:
            for r in master:
                fh.write(json.dumps(r) + "\n")
        print(f">>> wrote {args.out_jsonl}")
    if args.out_csv:
        with open(args.out_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            w.writeheader()
            for r in master:
                w.writerow(to_csv_row(r))
        print(f">>> wrote {args.out_csv}")
    return 0


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
