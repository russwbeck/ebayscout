"""
Tests for the pure cores of tools/audit_reference_coverage.py.

Only the torch/GCS/Sheets-free functions are covered here (the CLI loaders are
environment-dependent and exercised in Cloud/CI).
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout.tools.audit_reference_coverage import (
    parse_image_labels,
    coverage_report,
    analyze_scan_log,
)


class TestParseImageLabels:
    def test_year_slogan_split(self):
        pairs = parse_image_labels(["1979 Bag The Aggies", "2003 Is that Owl you've Got?"])
        assert ("1979", "Bag The Aggies") in pairs
        assert ("2003", "Is that Owl you've Got?") in pairs

    def test_bare_year_and_garbage_skipped(self):
        assert parse_image_labels(["1990", "", "xx"]) == set()


class TestCoverageReport:
    def test_flags_fully_missing_needed(self):
        needed = {("1983", "We Won The War"), ("2003", "Is that Owl you've Got?")}
        text   = {("2003", "Is that Owl you've Got?")}
        image  = {("2003", "Is that Owl you've Got?")}
        rep = coverage_report(needed, text, image)
        assert rep["total_needed"] == 2
        missing = {(r["year"], r["slogan"]) for r in rep["fully_missing"]}
        assert missing == {("1983", "We Won The War")}

    def test_normalization_matches_punctuation_and_case(self):
        needed = {("1986", "Begorra & Begone")}
        text   = {("1986", "begorra and begone")}  # different surface form...
        # punctuation/case stripped but "and" vs "&" still differ -> treat as missing
        rep = coverage_report(needed, text, set())
        assert len(rep["fully_missing"]) == 1
        # exact-after-normalization match is covered
        text2 = {("1986", "BEGORRA & BEGONE!!")}
        rep2 = coverage_report(needed, text2, set())
        assert rep2["fully_missing"] == []

    def test_image_only_is_not_fully_missing(self):
        needed = {("1974", "Deck Navy")}
        rep = coverage_report(needed, set(), {("1974", "Deck Navy")})
        assert rep["fully_missing"] == []
        assert len(rep["no_text"]) == 1   # still un-sloganable


class TestAnalyzeScanLog:
    def test_attractors_and_degenerate_and_zero(self):
        records = [
            {"item_id": "1", "best_score": 0.7, "top_matches": [
                {"year": "1990", "slogan": "Pins To Win"},
                {"year": "1990", "slogan": "Pins To Win"},
            ]},  # degenerate (all one slogan)
            {"item_id": "2", "best_score": 0.0, "top_matches": [
                {"year": "1990", "slogan": "Pins To Win"},
                {"year": "1979", "slogan": "Can The Juice"},
            ]},  # zero score
            {"item_id": "3", "best_score": 0.5, "top_matches": []},
        ]
        rep = analyze_scan_log(records)
        assert rep["n"] == 3
        assert rep["degenerate"] == ["1"]
        assert rep["zero_score"] == ["2"]
        # "Pins To Win" tops crops in listings 1 and 2
        listing_counts = dict(rep["top_attractor_listings"])
        assert listing_counts["Pins To Win"] == 2

    def test_empty(self):
        rep = analyze_scan_log([])
        assert rep["n"] == 0
        assert rep["degenerate"] == [] and rep["zero_score"] == []
