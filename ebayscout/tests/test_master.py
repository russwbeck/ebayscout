"""
Tests for tools/build_master_dataset.py — the one-master-dataset consolidator.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout.tools.build_master_dataset import (
    normalize_record,
    reconcile,
    to_csv_row,
)


class TestNormalize:
    def test_old_schema_derives_fields(self):
        rec = {"ts": "2026-05-29T14:00:00Z", "item_id": "v1|1|0",
               "title": "1984 PSU Mellon Bank Lot of 10", "asking": 20.0,
               "top_matches": [{"year": "1984", "slogan": "Coal Miners Slaughter", "overall": 0.9}]}
        n = normalize_record(rec)
        assert n["has_price"] is True
        assert n["title_count"] == 10
        assert "1984" in n["years"]
        assert n["best_year"] == "1984" and n["best_slogan"] == "Coal Miners Slaughter"
        assert n["buying_format"] == "unknown"   # old record, no buying_options

    def test_new_schema_uses_market_fields(self):
        rec = {"ts": "t", "item_id": "v1|2|0", "title": "x", "asking": 5.0,
               "buying_options": ["AUCTION"], "bid_count": 3,
               "year_counts": {"1990": 4}, "crops_scored": 4}
        n = normalize_record(rec)
        assert n["buying_format"] == "auction" and n["bid_count"] == 3
        assert n["years"] == ["1990"] and n["crops_scored"] == 4

    def test_no_price(self):
        assert normalize_record({"item_id": "backfill|abc", "asking": None})["has_price"] is False


class TestReconcile:
    def test_backfill_folds_into_matching_real_listing(self):
        real = {"ts": "2026-05-30T10:00:00Z", "item_id": "v1|9|0", "seller": "Sam",
                "title": "1979 PSU Lot", "asking": 18.0, "source": "hunt"}
        partial = {"ts": "2026-05-29T16:00:00Z", "item_id": "backfill|h", "seller": "sam",
                   "title": "1979 psu lot!", "asking": None, "source": "backfill"}
        master = reconcile([real, partial])
        assert len(master) == 1                       # folded, not double-counted
        row = master[0]
        assert row["item_id"] == "v1|9|0"             # real record represents it
        assert row["has_price"] is True
        assert row["times_seen"] == 2
        assert row["sources"] == "backfill;hunt"      # provenance preserved

    def test_unmatched_backfill_survives(self):
        real = {"ts": "t", "item_id": "v1|9|0", "seller": "Sam", "title": "A", "asking": 5.0}
        orphan = {"ts": "t", "item_id": "backfill|z", "seller": "Gone", "title": "B",
                  "asking": None, "source": "backfill"}
        master = reconcile([real, orphan])
        ids = {r["item_id"] for r in master}
        assert ids == {"v1|9|0", "backfill|z"}        # ended-listing history kept

    def test_cross_pass_real_duplicates_collapse(self):
        a = {"ts": "t1", "item_id": "v1|7|0", "seller": "s", "title": "t", "asking": 3.0}
        b = {"ts": "t2", "item_id": "v1|7|0", "seller": "s", "title": "t", "asking": 3.0}
        master = reconcile([a, b])
        assert len(master) == 1 and master[0]["times_seen"] == 2

    def test_priced_record_wins_representative(self):
        cheap_old = {"ts": "t1", "item_id": "v1|5|0", "seller": "s", "title": "t",
                     "asking": None, "source": "scan"}
        priced = {"ts": "t2", "item_id": "v1|5|0", "seller": "s", "title": "t",
                  "asking": 12.0, "source": "hunt"}
        master = reconcile([cheap_old, priced])
        assert master[0]["asking"] == 12.0 and master[0]["has_price"] is True


class TestCsvRow:
    def test_flattens_lists_and_bools(self):
        r = normalize_record({"item_id": "v1|1|0", "title": "x", "asking": 5.0,
                              "year_counts": {"1990": 1, "1991": 2}, "needed_hit": True})
        r["sources"] = "hunt"; r["times_seen"] = 1
        row = to_csv_row(r)
        assert row["years"] == "1990;1991"
        assert row["has_price"] == "yes" and row["needed_hit"] == "yes"
