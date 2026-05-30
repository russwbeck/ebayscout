"""
ebayscout/tools/market_report.py

Market report over the scan log (scan_log.jsonl). Pure-Python — runs anywhere,
no torch / GCP. Grows more accurate as the daily scans accumulate records and as
the richer capture fields (year_counts / crops_scored / title_count / buying
format) land from main._scan_log_record.

Headline metric: **cost per button, per YEAR.** Per-button-in-general is
useless (a 1974 CCB pin and a 2016 pin are different markets); per-button-per-
year is what you price your own listings against. We estimate it from
SINGLE-YEAR-LOT comps — listings whose buttons are all one year (title states a
single year, or every matched crop is one year) — where `asking / button_count`
is a clean per-button asking figure for that year. Mixed-year lots are counted
for supply but not used for the price estimate (their per-year price can't be
cleanly separated).

IMPORTANT CAVEAT: eBay's Browse API returns ACTIVE listings only, so every price
here is an ASKING price, not a sold/realized price. Read it as "what sellers are
asking per button for year Y," a listing-comp guide — not a guaranteed value.

Usage:
    python -m ebayscout.tools.market_report --scan-log scan_log.jsonl
    python -m ebayscout.tools.market_report --scan-log scan_log.jsonl --min-comps 3
"""

import argparse
import json
from collections import Counter, defaultdict
from statistics import median, quantiles

from ebayscout.utils import extract_years, extract_lot_count


# ---------------------------------------------------------------------------
# Pure cores (unit-testable)
# ---------------------------------------------------------------------------

def derive_listing(rec: dict) -> dict:
    """
    Normalize one scan-log record into the fields the report needs, working on
    both new (year_counts/crops_scored/title_count/title_years present) and old
    records (derive those from title + top_matches).

    Returns: asking, button_count, year_counts {year: n}, single_year (str|None).
    single_year is set when the lot is unambiguously one year (title names
    exactly one, or every matched crop is the same year).
    """
    asking = float(rec.get("asking") or 0.0)
    title  = rec.get("title", "") or ""

    year_counts = rec.get("year_counts")
    if not year_counts:
        year_counts = {}
        for m in rec.get("top_matches") or []:
            y = str(m.get("year"))
            if y and y != "None":
                year_counts[y] = year_counts.get(y, 0) + 1

    title_years = rec.get("title_years")
    if title_years is None:
        title_years = sorted(extract_years(title))
    title_years = [str(y) for y in title_years]

    title_count = rec.get("title_count")
    if title_count is None:
        title_count = extract_lot_count(title)

    crops = rec.get("crops_scored")
    if crops is None:
        crops = len(rec.get("top_matches") or [])

    # Button count: prefer the seller's stated lot size, then detected crops,
    # then matched-year total, else assume a single button.
    button_count = title_count or crops or sum(year_counts.values()) or 1

    single_year = None
    if len(title_years) == 1:
        single_year = title_years[0]
    elif len(year_counts) == 1:
        single_year = next(iter(year_counts))

    return {
        "asking": asking,
        "button_count": button_count,
        "year_counts": year_counts,
        "title_years": title_years,
        "single_year": single_year,
    }


def cost_per_button_by_year(records, min_comps: int = 1) -> dict:
    """
    Estimate asking cost/button per year from single-year-lot comps.

    Returns {
      "by_year": {year: {n, median, min, max, p25?, p75?, supply, median_lot}},
      "supply":  {year: # listings whose buttons include that year},
      "no_comp_years": [years with supply but no clean single-year comp],
    }
    """
    samples: dict = defaultdict(list)   # year -> [asking/button]
    lot_sizes: dict = defaultdict(list)
    supply: Counter = Counter()

    for rec in records:
        d = derive_listing(rec)
        for y in d["year_counts"]:
            supply[y] += 1
        if d["asking"] <= 0 or not d["single_year"] or not d["button_count"]:
            continue
        y = d["single_year"]
        samples[y].append(d["asking"] / d["button_count"])
        lot_sizes[y].append(d["button_count"])

    by_year: dict = {}
    for y, vals in samples.items():
        if len(vals) < min_comps:
            continue
        row = {
            "n": len(vals),
            "median": round(median(vals), 2),
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "supply": supply.get(y, 0),
            "median_lot": int(median(lot_sizes[y])),
        }
        if len(vals) >= 4:
            q = quantiles(vals, n=4)
            row["p25"], row["p75"] = round(q[0], 2), round(q[2], 2)
        by_year[y] = row

    no_comp = sorted(y for y in supply if y not in by_year)
    return {"by_year": by_year, "supply": dict(supply), "no_comp_years": no_comp}


def supply_summary(records) -> dict:
    """Quick supply-side context: counts, asking bands, sellers, format/condition."""
    records = list(records)
    asks = [float(r.get("asking") or 0) for r in records if (r.get("asking") or 0) > 0]
    bands: Counter = Counter()
    for a in asks:
        bands["<5" if a < 5 else "5-15" if a < 15 else "15-30" if a < 30
              else "30-75" if a < 75 else "75+"] += 1
    sellers = Counter(r.get("seller", "") for r in records if r.get("seller"))
    fmt: Counter = Counter()
    for r in records:
        opts = r.get("buying_options") or []
        fmt["auction" if "AUCTION" in opts else "fixed" if opts else "unknown"] += 1
    return {
        "n": len(records),
        "asking": {
            "n": len(asks),
            "median": round(median(asks), 2) if asks else 0,
            "min": round(min(asks), 2) if asks else 0,
            "max": round(max(asks), 2) if asks else 0,
        },
        "bands": dict(bands),
        "top_sellers": sellers.most_common(8),
        "distinct_sellers": len(sellers),
        "format": dict(fmt),
    }


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


def _print(records, min_comps: int) -> None:
    summ = supply_summary(records)
    print(f"\n=== Market report — {summ['n']} listings ===")
    a = summ["asking"]
    print(f"Asking (lot-level): median ${a['median']}  range ${a['min']}–${a['max']}  "
          f"bands {summ['bands']}")
    print(f"Sellers: {summ['distinct_sellers']} distinct; format mix {summ['format']}")
    print("Top sellers by supply:", ", ".join(f"{s}({c})" for s, c in summ["top_sellers"][:5]))

    rep = cost_per_button_by_year(records, min_comps=min_comps)
    print(f"\n=== Cost per BUTTON by YEAR (single-year-lot comps, min {min_comps}) ===")
    print("ASKING prices, not sold — a listing-comp guide.\n")
    print(f"  {'year':<6}{'comps':>6}{'$/btn med':>11}{'p25–p75':>14}"
          f"{'min–max':>14}{'lot':>6}{'supply':>8}")
    for y in sorted(rep["by_year"]):
        r = rep["by_year"][y]
        rng = f"{r.get('p25', '-')}–{r.get('p75', '-')}" if "p25" in r else "-"
        minmax = f"{r['min']}–{r['max']}"
        print(f"  {y:<6}{r['n']:>6}{r['median']:>11}{rng:>14}"
              f"{minmax:>14}{r['median_lot']:>6}{r['supply']:>8}")
    if rep["no_comp_years"]:
        print(f"\n  Years with supply but NO clean single-year comp (mixed lots only): "
              f"{', '.join(rep['no_comp_years'])}")
        print("  -> these need a single-year listing to surface before we can price them.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan-log", required=True, help="Path to scan_log.jsonl")
    ap.add_argument("--min-comps", type=int, default=1,
                    help="Min single-year comps before a year is reported (default 1).")
    args = ap.parse_args(argv)
    _print(_load(args.scan_log), args.min_comps)
    return 0


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
