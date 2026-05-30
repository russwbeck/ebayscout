"""
ebayscout/tools/audit_reference_coverage.py

Audit the CLIP reference data behind the needed-button scan. Two independent
checks, driven by the May 2026 backfill analysis:

  1. --coverage   Cross the sheet's NEEDED (year, slogan) rows against the
                  reference embeddings (text_features.pt phrases/years +
                  vectors.pt image labels). Flags needed buttons that are
                  MISSING from the reference set entirely (they can score ~0.00
                  and never alert — the HANDOFF-flagged gap, worst in the rarest
                  Central Counties 1972-1983 era). Needs torch + GCS + Sheets,
                  so it runs in Cloud / CI, not in a web session.

  2. --scan-log   Pure-Python analysis of a scan_log.jsonl (or a gcloud stdout
                  export is not supported here — feed the structured JSONL).
                  Surfaces "attractor" reference vectors that unrelated crops
                  collapse onto (e.g. "Penn State Pins To Win" appeared in 40 of
                  72 listings' top matches), degenerate listings whose every crop
                  maps to one slogan, and zero-score listings (coverage misses).
                  Runs anywhere — no torch / GCP needed.

The pure functions (coverage_report, analyze_scan_log, parse_image_labels) take
already-extracted data so they are unit-testable without the ML stack; the CLI
wrappers do the heavy, environment-dependent loading.

Usage:
    python -m ebayscout.tools.audit_reference_coverage --scan-log scan_log.jsonl
    python -m ebayscout.tools.audit_reference_coverage --coverage           # Cloud/CI
    python -m ebayscout.tools.audit_reference_coverage --coverage \
        --vectors vectors.pt --text text_features.pt --needed-file needed.json
"""

import argparse
import json
import re
import sys
from collections import Counter


# ---------------------------------------------------------------------------
# Pure cores (no torch / GCP — unit-testable)
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Strip punctuation, lowercase, collapse — matches sheets_client._normalize_key."""
    return re.sub(r"[^\w\s]", "", str(s).lower()).strip()


def parse_image_labels(labels) -> set[tuple[str, str]]:
    """
    Turn vectors.pt image labels ("YEAR SLOGAN") into a {(year_str, slogan)} set.
    Labels that are a bare year or unparseable are skipped.
    """
    pairs: set[tuple[str, str]] = set()
    for lab in labels or []:
        parts = str(lab).split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            pairs.add((parts[0], parts[1]))
    return pairs


def coverage_report(
    needed_pairs,
    text_pairs,
    image_pairs,
) -> dict:
    """
    needed_pairs / text_pairs / image_pairs: iterables of (year, slogan).

    A needed button is "matchable by slogan" only if its (year, normalized
    slogan) is in the text reference set; the image set is a secondary signal.
    Returns a structured report; `fully_missing` is the actionable list.
    """
    tn = {(str(y), _norm(s)) for y, s in text_pairs}
    iz = {(str(y), _norm(s)) for y, s in image_pairs}

    rows = []
    for y, s in sorted({(str(y), s) for y, s in needed_pairs}):
        key = (y, _norm(s))
        rows.append({
            "year": y, "slogan": s,
            "in_text":  key in tn,
            "in_image": key in iz,
        })
    fully_missing = [r for r in rows if not r["in_text"] and not r["in_image"]]
    no_text       = [r for r in rows if not r["in_text"]]
    return {
        "total_needed": len(rows),
        "fully_missing": fully_missing,
        "no_text": no_text,
        "rows": rows,
    }


def analyze_scan_log(records) -> dict:
    """
    records: iterable of scan_log.jsonl dicts (see main._scan_log_record).

    Returns attractor frequency over top_matches, degenerate listings (>=2 crops
    all mapping to one slogan), and zero-score listings (best_score == 0.0).
    """
    records = list(records)
    attractor: Counter = Counter()
    slog_listing: Counter = Counter()   # distinct listings a slogan tops
    degenerate = []
    zero_score = []
    for r in records:
        tm = r.get("top_matches") or []
        ident = r.get("item_id") or r.get("title") or "?"
        for m in tm:
            attractor[(m.get("year"), m.get("slogan"))] += 1
        names = {m.get("slogan") for m in tm}
        for nm in names:
            slog_listing[nm] += 1
        if len(tm) >= 2 and len(names) == 1:
            degenerate.append(ident)
        if r.get("best_score", 1.0) == 0.0:
            zero_score.append(ident)
    return {
        "n": len(records),
        "top_attractors": attractor.most_common(25),
        "top_attractor_listings": slog_listing.most_common(25),
        "degenerate": degenerate,
        "zero_score": zero_score,
    }


# ---------------------------------------------------------------------------
# CLI glue (heavy / environment-dependent loading)
# ---------------------------------------------------------------------------

def _load_scan_log(path: str) -> list[dict]:
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _load_reference(vectors_path, text_path):
    """Load (text_pairs, image_pairs) from .pt files (local) or GCS. Needs torch."""
    import os
    import tempfile
    import torch  # lazy — only --coverage needs it

    def _pt(path_or_blob, default_blob):
        if path_or_blob and os.path.exists(path_or_blob):
            return torch.load(path_or_blob, weights_only=False, map_location="cpu")
        # Fall back to GCS using the project bucket.
        from google.cloud import storage
        from ebayscout import config
        client = storage.Client()
        bucket = client.bucket(config.BUCKET_NAME)
        with tempfile.TemporaryDirectory() as td:
            local = os.path.join(td, default_blob)
            bucket.blob(default_blob).download_to_filename(local)
            return torch.load(local, weights_only=False, map_location="cpu")

    text = _pt(text_path, "text_features.pt")
    vecs = _pt(vectors_path, "vectors.pt")
    text_pairs = {(str(y), p) for y, p in zip(text["years"], text["phrases"])}
    image_pairs = parse_image_labels(vecs.get("labels", []))
    return text_pairs, image_pairs


def _load_needed(needed_file):
    """Needed (year, slogan) pairs from a JSON file [[year, slogan], ...] or Sheets."""
    if needed_file:
        with open(needed_file) as fh:
            return {(str(y), s) for y, s in json.load(fh)}
    # Sheets path — needs GOOGLE_SHEETS_JSON + SPREADSHEET_ID in the env.
    import os
    from ebayscout import sheets_client
    sheets_json    = os.environ["GOOGLE_SHEETS_JSON"]
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    buy_rules = sheets_client.load_buy_rules(sheets_json, spreadsheet_id)
    needed = set()
    for (year, slogan), rule in buy_rules.items():
        try:
            if int((rule or {}).get("amount_needed", 0) or 0) > 0:
                needed.add((str(year), slogan))
        except (ValueError, TypeError):
            continue
    return needed


def _print_coverage(rep: dict) -> None:
    print(f"\n=== Reference coverage: {rep['total_needed']} needed (year, slogan) rows ===")
    print(f"fully missing from BOTH text + image reference: {len(rep['fully_missing'])}")
    print(f"missing from TEXT reference (un-sloganable):     {len(rep['no_text'])}")
    if rep["fully_missing"]:
        print("\n-- ADD THESE to the reference set (needed but absent) --")
        for r in rep["fully_missing"]:
            print(f"   {r['year']}  {r['slogan']}")


def _print_scanlog(rep: dict) -> None:
    print(f"\n=== scan_log analysis: {rep['n']} listings ===")
    print("Top attractor (year, slogan) by # listings whose crops map to it:")
    for nm, c in rep["top_attractor_listings"][:15]:
        print(f"   x{c:4d}  {nm}")
    print(f"\nDegenerate listings (every crop -> one slogan): {len(rep['degenerate'])}")
    print(f"Zero-score listings (best_score == 0.0):        {len(rep['zero_score'])}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan-log", help="Path to a scan_log.jsonl to analyze (pure-Python).")
    ap.add_argument("--coverage", action="store_true",
                    help="Cross needed rows vs reference embeddings (needs torch/GCS/Sheets).")
    ap.add_argument("--vectors", help="Local vectors.pt (else GCS).")
    ap.add_argument("--text", help="Local text_features.pt (else GCS).")
    ap.add_argument("--needed-file", help="JSON [[year, slogan], ...] (else Google Sheets via env).")
    args = ap.parse_args(argv)

    if not args.scan_log and not args.coverage:
        ap.error("pass --scan-log PATH and/or --coverage")

    if args.scan_log:
        _print_scanlog(analyze_scan_log(_load_scan_log(args.scan_log)))

    if args.coverage:
        try:
            text_pairs, image_pairs = _load_reference(args.vectors, args.text)
            needed = _load_needed(args.needed_file)
        except Exception as exc:   # pragma: no cover - environment-dependent
            print(f"!!! coverage audit could not load inputs: {exc}", file=sys.stderr)
            print("    (needs torch + the .pt reference files and the needed list; "
                  "run in Cloud/CI or pass --vectors/--text/--needed-file)", file=sys.stderr)
            return 2
        _print_coverage(coverage_report(needed, text_pairs, image_pairs))

    return 0


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
