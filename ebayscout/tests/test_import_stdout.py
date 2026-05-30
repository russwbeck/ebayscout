"""
Tests for tools/import_stdout_log.py — recovering scan_log records from stdout.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout.tools.import_stdout_log import (
    parse_stdout_records,
    dedup_records,
)

_NEEDED = '>>> TITLE: [needed 0.86 1979 Bag The Aggies] [sellerA] 1979 PSU Lot of 9'
_LOW    = '>>> TITLE: [low-conf 0.55] [sellerB] Penn State pin'
_REJ    = '>>> TITLE: [rejected 0.30] [sellerC] PSU sweater'
_NOISE  = '>>> IMAGE: Returning 3 Hough crops.'


class TestParse:
    def test_needed_line(self):
        recs = parse_stdout_records([("2026-05-29T16:40:00Z", _NEEDED)], "src")
        assert len(recs) == 1
        r = recs[0]
        assert r["needed_hit"] and r["alerted"]
        assert r["best_needed"] == {"year": "1979", "slogan": "Bag The Aggies", "overall": 0.86}
        assert r["year_counts"] == {"1979": 1}
        assert r["title_count"] == 9 and r["title_years"] == [1979]
        assert r["asking"] is None and r["source"] == "src"
        assert r["item_id"].startswith("backfill|")

    def test_low_and_rejected(self):
        recs = parse_stdout_records([("", _LOW), ("", _REJ)], "src")
        kinds = {r["_kind"] for r in recs}
        assert kinds == {"low-conf", "rejected"}
        assert all(not r["needed_hit"] and r["best_needed"] is None for r in recs)

    def test_ignores_non_title_lines(self):
        assert parse_stdout_records([("", _NOISE)], "src") == []

    def test_since_filter(self):
        entries = [("2026-05-29T14:00:00Z", _NEEDED), ("2026-05-29T17:00:00Z", _NEEDED)]
        recs = parse_stdout_records(entries, "src", since="2026-05-29T16:00")
        assert len(recs) == 1 and recs[0]["ts"] == "2026-05-29T17:00:00Z"


class TestDedup:
    def test_keeps_best_kind_then_score(self):
        entries = [
            ("", '>>> TITLE: [low-conf 0.55] [s] same title'),
            ("", '>>> TITLE: [needed 0.61 1990 Foo] [s] same title'),
            ("", '>>> TITLE: [needed 0.80 1990 Foo] [s] same title'),
        ]
        recs = dedup_records(parse_stdout_records(entries, "src"))
        assert len(recs) == 1
        assert recs[0]["_kind"] == "needed" and recs[0]["best_score"] == 0.80

    def test_distinct_listings_kept(self):
        entries = [("", '>>> TITLE: [low-conf 0.5] [s] a'),
                   ("", '>>> TITLE: [low-conf 0.5] [s] b')]
        assert len(dedup_records(parse_stdout_records(entries, "src"))) == 2
