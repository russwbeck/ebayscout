"""
Tests for the pure cores of tools/market_report.py (cost/button/year).
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout.tools.market_report import (
    derive_listing,
    cost_per_button_by_year,
    supply_summary,
)


class TestDeriveListing:
    def test_uses_new_fields_when_present(self):
        rec = {"asking": 18.0, "title_count": 9, "title_years": [1979],
               "year_counts": {"1979": 9}, "crops_scored": 9}
        d = derive_listing(rec)
        assert d["asking"] == 18.0
        assert d["button_count"] == 9
        assert d["single_year"] == "1979"

    def test_derives_from_title_and_top_matches_on_old_records(self):
        rec = {"asking": 20.0,
               "title": "1984 Penn State Mellon Bank Buttons Full Set Lot of 10",
               "top_matches": [{"year": "1984", "slogan": "x"},
                               {"year": "1984", "slogan": "y"}]}
        d = derive_listing(rec)
        assert d["single_year"] == "1984"      # one title year
        assert d["button_count"] == 10         # from "Lot of 10"

    def test_mixed_year_lot_has_no_single_year(self):
        rec = {"asking": 50.0, "year_counts": {"1979": 3, "1986": 2},
               "title_years": [], "crops_scored": 5}
        d = derive_listing(rec)
        assert d["single_year"] is None
        assert d["button_count"] == 5

    def test_button_count_falls_back_to_one(self):
        rec = {"asking": 5.0, "title": "Penn State pin", "top_matches": []}
        assert derive_listing(rec)["button_count"] == 1


class TestCostPerButtonByYear:
    def test_single_year_lots_drive_estimate(self):
        records = [
            # 1979: $18 / 9 = $2.00 ; $9 / 3 = $3.00  -> median 2.50
            {"asking": 18.0, "title_count": 9, "title_years": [1979], "year_counts": {"1979": 9}},
            {"asking": 9.0,  "title_count": 3, "title_years": [1979], "year_counts": {"1979": 3}},
            # mixed-year lot: counts for supply, excluded from price
            {"asking": 50.0, "year_counts": {"1979": 2, "1986": 2}, "title_years": []},
        ]
        rep = cost_per_button_by_year(records)
        assert rep["by_year"]["1979"]["n"] == 2
        assert rep["by_year"]["1979"]["median"] == 2.5
        # supply for 1979 = all three listings touch it
        assert rep["supply"]["1979"] == 3
        # 1986 has supply (the mixed lot) but no clean comp
        assert "1986" in rep["no_comp_years"]
        assert "1986" not in rep["by_year"]

    def test_min_comps_filters_thin_years(self):
        records = [{"asking": 10.0, "title_count": 2, "title_years": [2003],
                    "year_counts": {"2003": 2}}]
        assert cost_per_button_by_year(records, min_comps=2)["by_year"] == {}

    def test_zero_or_missing_price_ignored(self):
        records = [{"asking": 0.0, "title_years": [1980], "year_counts": {"1980": 1}}]
        assert cost_per_button_by_year(records)["by_year"] == {}


class TestSupplySummary:
    def test_bands_and_format(self):
        records = [
            {"asking": 3.0, "seller": "a", "buying_options": ["AUCTION"]},
            {"asking": 20.0, "seller": "b", "buying_options": ["FIXED_PRICE"]},
            {"asking": 100.0, "seller": "a", "buying_options": []},
        ]
        s = supply_summary(records)
        assert s["n"] == 3
        assert s["bands"]["<5"] == 1 and s["bands"]["75+"] == 1
        assert s["format"]["auction"] == 1 and s["format"]["fixed"] == 1
        assert s["distinct_sellers"] == 2
