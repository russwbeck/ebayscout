"""
ebayscout/tools/merge_scan_log.py

One-time seed: fold recovered/historical records INTO the bot's live
scan_log.jsonl so its single growing log is complete from day one — then the
bot keeps appending to that same file on every run, no further steps.

Safe + idempotent:
  - the base log is preserved exactly (the bot's own observations are never
    dropped, including multiple observations of the same item over time);
  - only records whose item_id isn't already in the base are appended, so
    re-running adds nothing the second time.

Operate on a COPY pulled from GCS, then push the result back:

    BUCKET=gs://60d488c5-9c8e-4acc-aac-button-data/ebay_scout
    gsutil cp $BUCKET/scan_log.jsonl ./scan_log.jsonl                 # current live log
    python -m ebayscout.tools.merge_scan_log \
        --base scan_log.jsonl --add backfill_scan_log.jsonl --out scan_log.merged.jsonl
    gsutil cp ./scan_log.merged.jsonl $BUCKET/scan_log.jsonl          # complete log back
"""

import argparse
import json


def merge(base: list[dict], adds: list[list[dict]]) -> tuple[list[dict], int]:
    """Return (merged_records, n_added). Base kept intact; only item_ids not
    already present are appended (idempotent)."""
    seen = {r.get("item_id") for r in base if r.get("item_id")}
    out = list(base)
    added = 0
    for recs in adds:
        for r in recs:
            iid = r.get("item_id")
            if iid and iid in seen:
                continue
            if iid:
                seen.add(iid)
            out.append(r)
            added += 1
    return out, added


def _load(path: str) -> list[dict]:
    return [json.loads(line) for line in open(path) if line.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="The live scan_log.jsonl (kept intact).")
    ap.add_argument("--add", nargs="+", required=True,
                    help="Historical JSONL file(s) to fold in (e.g. backfill_scan_log.jsonl).")
    ap.add_argument("--out", required=True, help="Where to write the merged log.")
    args = ap.parse_args(argv)

    base = _load(args.base)
    adds = [_load(p) for p in args.add]
    out, added = merge(base, adds)
    with open(args.out, "w") as fh:
        for r in out:
            fh.write(json.dumps(r) + "\n")
    print(f">>> base {len(base)} + {added} new historical records = {len(out)} total "
          f"-> {args.out}")
    return 0


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
