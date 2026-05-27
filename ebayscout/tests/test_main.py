"""
Tests for ebayscout utility functions (parse_price_source, format_manual_result).

These live in utils.py so they can be tested without triggering the
module-level GCP secret fetching in main.py.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout.utils import parse_price_source, format_manual_result, title_has_excluded_keyword


class TestParsePriceSource:
    def test_dollar_sign_with_source(self):
        price, source = parse_price_source("$25.00 | Facebook Marketplace")
        assert price == 25.0
        assert source == "Facebook Marketplace"

    def test_no_dollar_sign(self):
        price, source = parse_price_source("12 | Mercari")
        assert price == 12.0
        assert source == "Mercari"

    def test_cents(self):
        price, source = parse_price_source("$8.50 | Etsy")
        assert price == 8.5
        assert source == "Etsy"

    def test_no_pipe_returns_none(self):
        price, source = parse_price_source("$25.00")
        assert price is None
        assert source == ""

    def test_bad_price_returns_none(self):
        price, source = parse_price_source("free | Craigslist")
        assert price is None

    def test_source_with_spaces(self):
        price, source = parse_price_source("$45 | Facebook Marketplace Group")
        assert price == 45.0
        assert source == "Facebook Marketplace Group"

    def test_comma_in_price(self):
        price, source = parse_price_source("$1,200.00 | eBay")
        assert price == 1200.0

    def test_extra_whitespace(self):
        price, source = parse_price_source("  $30   |   Mercari  ")
        assert price == 30.0
        assert source == "Mercari"


class TestFormatManualResult:
    def _matches(self):
        return [
            {"year": "1977", "slogan": "We Are Number One",
             "max_price_single": "$4.00", "amount_needed": 2},
            {"year": "1980", "slogan": "4th & Goal",
             "max_price_single": "$3.50", "amount_needed": 0},
        ]

    def test_contains_source(self):
        text = format_manual_result(
            source="Facebook Marketplace", asking_price=10.0,
            matches=self._matches(), lot_value=7.50, margin=-2.50,
            needed=[self._matches()[0]], unmatched_count=0,
        )
        assert "Facebook Marketplace" in text

    def test_shows_asking_price(self):
        text = format_manual_result(
            source="Mercari", asking_price=5.0,
            matches=self._matches(), lot_value=7.50, margin=2.50,
            needed=[], unmatched_count=0,
        )
        assert "$5.00" in text

    def test_good_deal_verdict(self):
        text = format_manual_result(
            source="Mercari", asking_price=5.0,
            matches=self._matches(), lot_value=7.50, margin=2.50,
            needed=[], unmatched_count=0,
        )
        assert "Good deal" in text
        assert "+$2.50" in text

    def test_overpay_verdict(self):
        text = format_manual_result(
            source="Mercari", asking_price=50.0,
            matches=self._matches(), lot_value=7.50, margin=-42.50,
            needed=[], unmatched_count=0,
        )
        assert "overpay" in text
        assert "$42.50" in text

    def test_needed_buttons_flagged(self):
        text = format_manual_result(
            source="FB", asking_price=10.0,
            matches=self._matches(), lot_value=7.50, margin=-2.50,
            needed=[self._matches()[0]], unmatched_count=0,
        )
        assert "⭐" in text
        assert "We Are Number One" in text

    def test_unmatched_count_shown(self):
        text = format_manual_result(
            source="FB", asking_price=10.0,
            matches=[], lot_value=0.0, margin=-10.0,
            needed=[], unmatched_count=3,
        )
        assert "3 button" in text

    def test_star_on_needed_match_line(self):
        text = format_manual_result(
            source="FB", asking_price=5.0,
            matches=self._matches(), lot_value=7.50, margin=2.50,
            needed=[self._matches()[0]], unmatched_count=0,
        )
        assert "need 2" in text


class TestTitleHasExcludedKeyword:
    _KEYWORDS = ["embroidered", "hoodie", "sweatshirt", "polo", "quarterzip",
                 "quarter zip", "quarter-zip", "drifit", "stitched", "denim",
                 "antigua", "jacket", "pullover"]

    def test_exact_match(self):
        assert title_has_excluded_keyword("Penn State Hoodie 2001", self._KEYWORDS)

    def test_case_insensitive(self):
        assert title_has_excluded_keyword("PSU EMBROIDERED Button Lot", self._KEYWORDS)

    def test_keyword_mid_title(self):
        assert title_has_excluded_keyword("Vintage Penn State denim jacket pin", self._KEYWORDS)

    def test_normal_button_title_passes(self):
        assert not title_has_excluded_keyword("Penn State Football Button 1987 Fiesta Bowl", self._KEYWORDS)

    def test_empty_keywords_list_never_filters(self):
        assert not title_has_excluded_keyword("Penn State Hoodie", [])

    def test_empty_title(self):
        assert not title_has_excluded_keyword("", self._KEYWORDS)

    def test_quarter_zip_with_space(self):
        assert title_has_excluded_keyword("PSU Quarter Zip Pullover pin", self._KEYWORDS)

    def test_quarter_zip_hyphenated(self):
        assert title_has_excluded_keyword("Penn State Quarter-Zip", self._KEYWORDS)

    def test_antigua_brand(self):
        assert title_has_excluded_keyword("Penn State Antigua polo shirt", self._KEYWORDS)
