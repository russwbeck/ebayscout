"""
Tests for ebayscout utility functions (parse_price_source, format_manual_result).

These live in utils.py so they can be tested without triggering the
module-level GCP secret fetching in main.py.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout.utils import (
    parse_price_source,
    format_manual_result,
    title_has_excluded_keyword,
    extract_years,
    needed_years,
    build_year_queries,
    build_era_queries,
    era_year_set,
    parse_era,
    parse_confirmation,
    other_era,
)


class TestBuildEraQueries:
    def test_prefix_button_product_tagged_with_era(self):
        out = build_era_queries(["Penn State", "PSU"], ["button", "pin"], "Mellon", "Mellon")
        assert ("Penn State Mellon button", "Mellon") in out
        assert ("PSU Mellon pin", "Mellon") in out
        assert len(out) == 4
        assert all(era == "Mellon" for _q, era in out)

    def test_central_counties_word(self):
        out = build_era_queries(["Nittany Lions"], ["badge"], "Central Counties", "Central Counties")
        assert out == [("Nittany Lions Central Counties badge", "Central Counties")]

    def test_empty_inputs(self):
        assert build_era_queries([], ["button"], "Mellon", "Mellon") == []
        assert build_era_queries(["PSU"], [], "Mellon", "Mellon") == []

_ERAS = {
    "Central Counties": (1972, 1983),
    "Mellon":           (1984, 2001),
    "Citizens":         (2001, 2026),
}


class TestEraHelpers:
    def test_era_year_set_range(self):
        assert era_year_set("Central Counties", _ERAS) == set(range(1972, 1984))
        assert era_year_set("Mellon", _ERAS) == set(range(1984, 2002))

    def test_era_year_set_all_and_unknown_are_empty(self):
        assert era_year_set("all", _ERAS) == set()
        assert era_year_set("", _ERAS) == set()
        assert era_year_set("Nonsense", _ERAS) == set()

    def test_parse_era_aliases(self):
        assert parse_era("ccb") == "Central Counties"
        assert parse_era("central counties lot") == "Central Counties"
        assert parse_era("looks like mellon") == "Mellon"
        assert parse_era("citizens era") == "Citizens"

    def test_parse_era_all_opt_out(self):
        assert parse_era("all") == "all"
        assert parse_era("any era") == "all"

    def test_parse_era_none(self):
        assert parse_era("go") is None
        assert parse_era("42") is None

    def test_parse_confirmation_go(self):
        assert parse_confirmation("go") == (None, None)

    def test_parse_confirmation_count_only(self):
        assert parse_confirmation("42") == (42, None)

    def test_parse_confirmation_era_only(self):
        assert parse_confirmation("mellon") == (None, "Mellon")

    def test_parse_confirmation_both(self):
        assert parse_confirmation("mellon 42") == (42, "Mellon")
        assert parse_confirmation("all 54") == (54, "all")

    def test_other_era_prefers_runner_up_vote(self):
        # Runner-up by vote that isn't the era we used.
        assert other_era("Central Counties",
                          ["Central Counties", "Mellon", "Citizens"], _ERAS) == "Mellon"

    def test_other_era_falls_back_to_next_defined(self):
        # No useful ranking → first defined era that isn't the one used.
        assert other_era("Central Counties", [], _ERAS) == "Mellon"
        assert other_era("Mellon", [], _ERAS) == "Central Counties"

    def test_other_era_none_when_only_one(self):
        assert other_era("Only", ["Only"], {"Only": (1, 2)}) is None


class TestNeededYears:
    def _rules(self):
        return {
            ("1982", "We Are Number One"): {"amount_needed": "2"},
            ("1982", "Other Slogan"):      {"amount_needed": "0"},
            ("1995", "Beat Michigan"):     {"amount_needed": "1"},
            ("2003", "Gopher Broke"):      {"amount_needed": ""},   # blank → 0
            ("abcd", "Bad Year"):          {"amount_needed": "5"},  # unparseable year
        }

    def test_selects_only_positive_amounts(self):
        assert needed_years(self._rules()) == {1982, 1995}

    def test_empty_rules(self):
        assert needed_years({}) == set()

    def test_skips_unparseable_year_but_keeps_others(self):
        # "abcd" is dropped silently; 1982/1995 still returned.
        years = needed_years(self._rules())
        assert 1982 in years and 1995 in years
        assert all(isinstance(y, int) for y in years)


class TestBuildYearQueries:
    def test_term_year_product(self):
        out = build_year_queries(["Penn State button", "PSU pin"], {1982})
        assert ("Penn State button 1982", 1982) in out
        assert ("PSU pin 1982", 1982) in out
        assert len(out) == 2

    def test_sorted_by_year(self):
        out = build_year_queries(["X"], [1995, 1982, 1990])
        years = [y for _q, y in out]
        assert years == [1982, 1990, 1995]

    def test_empty_inputs(self):
        assert build_year_queries([], {1982}) == []
        assert build_year_queries(["X"], []) == []

    def test_query_string_format(self):
        out = build_year_queries(["Nittany Lions badge"], {2001})
        assert out == [("Nittany Lions badge 2001", 2001)]


class TestExtractYears:
    def test_single_year(self):
        assert extract_years("Penn State 1982 Fiesta Bowl button") == {1982}

    def test_multiple_years(self):
        assert extract_years("PSU buttons lot 1977 1980 1982") == {1977, 1980, 1982}

    def test_no_year(self):
        assert extract_years("Penn State Nittany Lions pinback button") == set()

    def test_empty_title(self):
        assert extract_years("") == set()

    def test_ignores_too_long_digit_run(self):
        # A year embedded in a longer digit run (e.g. a SKU) is not a year.
        assert extract_years("item 219820 lot") == set()

    def test_ignores_out_of_range(self):
        # 1899 / 2100 are outside the 1900-2099 button-era window.
        assert extract_years("vintage 1899 reproduction") == set()
        assert extract_years("future 2100 design") == set()

    def test_price_like_number_with_comma_not_matched(self):
        # "$1,982" has a comma, so no bare 4-digit run — not a year.
        assert extract_years("rare lot value $1,982 obo") == set()

    def test_year_adjacent_to_punctuation(self):
        assert extract_years("Penn State (1986) Orange Bowl") == {1986}


class TestParsePriceSource:
    def test_dollar_sign_with_source(self):
        price, source, count = parse_price_source("$25.00 | Facebook Marketplace")
        assert price == 25.0
        assert source == "Facebook Marketplace"
        assert count is None

    def test_no_dollar_sign(self):
        price, source, count = parse_price_source("12 | Mercari")
        assert price == 12.0
        assert source == "Mercari"
        assert count is None

    def test_cents(self):
        price, source, count = parse_price_source("$8.50 | Etsy")
        assert price == 8.5
        assert source == "Etsy"

    def test_no_pipe_returns_none(self):
        price, source, count = parse_price_source("$25.00")
        assert price is None
        assert source == ""
        assert count is None

    def test_bad_price_returns_none(self):
        price, source, count = parse_price_source("free | Craigslist")
        assert price is None

    def test_source_with_spaces(self):
        price, source, count = parse_price_source("$45 | Facebook Marketplace Group")
        assert price == 45.0
        assert source == "Facebook Marketplace Group"

    def test_comma_in_price(self):
        price, source, count = parse_price_source("$1,200.00 | eBay")
        assert price == 1200.0

    def test_extra_whitespace(self):
        price, source, count = parse_price_source("  $30   |   Mercari  ")
        assert price == 30.0
        assert source == "Mercari"

    def test_button_count_parsed(self):
        price, source, count = parse_price_source("$25.00 | Facebook Marketplace | 35")
        assert price == 25.0
        assert source == "Facebook Marketplace"
        assert count == 35

    def test_invalid_count_is_none(self):
        # A non-integer count is silently ignored (utils.parse_price_source).
        price, source, count = parse_price_source("$25.00 | Facebook | many")
        assert price == 25.0
        assert source == "Facebook"
        assert count is None


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
