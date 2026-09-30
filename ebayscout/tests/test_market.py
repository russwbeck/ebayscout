"""
Tests for the pure cores of tools/market_report.py (cost/button/year).
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout.tools.market_report import (
    derive_listing,
    cost_per_button_by_year,
    price_per_button_by_button,
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

    def test_detected_count_beats_the_confirmed_count(self):
        # a pipeline row's crops_scored is the confirmed crops only
        rec = {"asking": 20.0, "title": "Penn State buttons", "top_matches": [],
               "crops_scored": 4, "buttons_detected": 10}
        assert derive_listing(rec)["button_count"] == 10

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


def _priced(item_id, per, *buttons):
    return {"item_id": item_id, "price_per_button": per,
            "buttons": [{"year": y, "slogan": sl, "n": n} for y, sl, n in buttons]}


class TestPricePerButtonByButton:
    def test_averages_a_button_over_the_lots_it_turned_up_in(self):
        rep = price_per_button_by_button([
            _priced("a", 2.0, (1985, "Beat Pitt", 1), (1986, "Beat Miami", 1)),
            _priced("b", 4.0, (1985, "Beat Pitt", 1)),
        ])
        pitt = next(r for r in rep["buttons"] if r["slogan"] == "Beat Pitt")
        assert pitt == {"year": "1985", "slogan": "Beat Pitt", "lots": 2,
                        "avg": 3.0, "median": 3.0, "min": 2.0, "max": 4.0}
        miami = next(r for r in rep["buttons"] if r["slogan"] == "Beat Miami")
        assert miami["lots"] == 1 and miami["avg"] == 2.0
        assert rep["lots"] == 2
        assert rep["overall"] == {"avg": 3.0, "median": 3.0}

    def test_duplicates_in_one_lot_are_one_observation(self):
        rep = price_per_button_by_button([
            _priced("a", 1.0, (1985, "Beat Pitt", 5)),
            _priced("b", 3.0, (1985, "Beat Pitt", 1)),
        ])
        assert rep["buttons"][0]["lots"] == 2 and rep["buttons"][0]["avg"] == 2.0

    def test_records_without_a_per_button_price_are_left_out(self):
        rep = price_per_button_by_button([
            {"item_id": "old", "asking": 50.0, "crops_scored": 3,
             "top_matches": [{"year": 1985, "slogan": "Beat Pitt", "overall": 0.9}]},
            _priced("none", None, (1985, "Beat Pitt", 1)),
            _priced("zero", 0.0, (1985, "Beat Pitt", 1)),
        ])
        assert rep == {"lots": 0, "overall": None, "buttons": []}

    def test_a_recrawled_listing_counts_once_at_its_latest_price(self):
        rep = price_per_button_by_button([
            _priced("a", 5.0, (1985, "Beat Pitt", 1)),
            _priced("a", 3.0, (1985, "Beat Pitt", 1)),
        ])
        assert rep["lots"] == 1 and rep["buttons"][0]["avg"] == 3.0

    def test_auctions_are_not_listing_prices(self):
        auction = _priced("a", 0.5, (1985, "Beat Pitt", 1))
        auction["buying_options"] = ["AUCTION"]
        fixed = _priced("b", 3.0, (1985, "Beat Pitt", 1))
        fixed["buying_options"] = ["FIXED_PRICE"]
        rep = price_per_button_by_button([auction, fixed])
        assert rep["lots"] == 1 and rep["buttons"][0]["avg"] == 3.0

    def test_min_comps_filters_thin_buttons(self):
        rep = price_per_button_by_button([
            _priced("a", 2.0, (1985, "Beat Pitt", 1), (1990, "Beat Texas", 1)),
            _priced("b", 4.0, (1985, "Beat Pitt", 1)),
        ], min_comps=2)
        assert [r["slogan"] for r in rep["buttons"]] == ["Beat Pitt"]

    def test_sorted_by_year_then_slogan(self):
        rep = price_per_button_by_button([
            _priced("a", 2.0, (1990, "B", 1), (1985, "Z", 1), (1985, "A", 1)),
        ])
        assert [(r["year"], r["slogan"]) for r in rep["buttons"]] == [
            ("1985", "A"), ("1985", "Z"), ("1990", "B")]
