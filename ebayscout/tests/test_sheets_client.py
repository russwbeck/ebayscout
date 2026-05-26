"""
Tests for ebayscout/sheets_client.py
"""

import pytest
from unittest.mock import patch, MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import sheets_client


FAKE_ROWS = [
    ["Year", "Slogan", "Count", "Max Price Single", "Max Price Year", "Notes", "Amount Needed"],
    ["1977", "We Are Number One", "1", "$4.00", "$8.00", "", "2"],
    ["1978", "Touchdown!",        "1", "$2.50", "$5.00", "rare", "0"],
    ["1980", "4th & Goal",        "2", "$3.50", "$7.00", "", "1"],
    ["1982", "Smear the Bear",    "1", "$6.00", "$12.00", "", "3"],
]


def _make_mock_sheet(rows=FAKE_ROWS):
    mock_sheet = MagicMock()
    mock_sheet.get_all_values.return_value = rows
    mock_spreadsheet = MagicMock()
    mock_spreadsheet.sheet1 = mock_sheet
    mock_gc = MagicMock()
    mock_gc.open_by_key.return_value = mock_spreadsheet
    return mock_gc


class TestLoadBuyRules:
    def _load(self, rows=FAKE_ROWS):
        mock_gc = _make_mock_sheet(rows)
        with patch("ebayscout.sheets_client.gspread.authorize", return_value=mock_gc), \
             patch("ebayscout.sheets_client.service_account.Credentials.from_service_account_info"):
            import json
            fake_json = json.dumps({"type": "service_account"})
            return sheets_client.load_buy_rules(fake_json, "fake-spreadsheet-id")

    def test_loads_correct_number_of_rules(self):
        rules = self._load()
        assert len(rules) == 4   # 4 data rows

    def test_correct_key_structure(self):
        rules = self._load()
        assert ("1977", "We Are Number One") in rules
        assert ("1980", "4th & Goal") in rules

    def test_correct_field_values(self):
        rules = self._load()
        rule = rules[("1977", "We Are Number One")]
        assert rule["max_price_single"] == "$4.00"
        assert rule["max_price_year"]   == "$8.00"
        assert rule["amount_needed"]    == "2"

    def test_raises_on_empty_sheet(self):
        with pytest.raises(RuntimeError, match="empty"):
            self._load(rows=[])

    def test_raises_on_missing_column(self):
        bad_rows = [["Year", "Slogan"], ["1977", "We Are Number One"]]
        with pytest.raises(RuntimeError, match="Missing expected column"):
            self._load(rows=bad_rows)


class TestGetBuyDecision:
    def _rules(self):
        return {
            ("1977", "We Are Number One"): {
                "max_price_single": "$4.00",
                "max_price_year":   "$8.00",
                "notes":            "",
                "amount_needed":    "2",
            },
            ("1978", "Touchdown!"): {
                "max_price_single": "$2.50",
                "max_price_year":   "$5.00",
                "notes":            "rare",
                "amount_needed":    "0",
            },
        }

    def test_exact_match(self):
        price, _, _, needed = sheets_client.get_buy_decision("1977", "We Are Number One", self._rules())
        assert price == "$4.00"
        assert needed == 2

    def test_fuzzy_match_strips_punctuation(self):
        """'We Are Number One!' should fuzzy-match 'We Are Number One'."""
        price, _, _, needed = sheets_client.get_buy_decision(
            "1977", "We Are Number One!", self._rules()
        )
        assert price == "$4.00"

    def test_returns_zeros_for_unknown(self):
        price, year, notes, needed = sheets_client.get_buy_decision(
            "1999", "Unknown Slogan", self._rules()
        )
        assert price == ""
        assert needed == 0

    def test_amount_needed_as_int(self):
        _, _, _, needed = sheets_client.get_buy_decision("1977", "We Are Number One", self._rules())
        assert isinstance(needed, int)
        assert needed == 2

    def test_amount_needed_zero_as_int(self):
        _, _, _, needed = sheets_client.get_buy_decision("1978", "Touchdown!", self._rules())
        assert needed == 0


class TestParsePrice:
    def test_dollar_sign(self):
        assert sheets_client.parse_price("$4.00") == pytest.approx(4.0)

    def test_no_dollar_sign(self):
        assert sheets_client.parse_price("2.50") == pytest.approx(2.5)

    def test_empty_string(self):
        assert sheets_client.parse_price("") == 0.0

    def test_invalid_string(self):
        assert sheets_client.parse_price("N/A") == 0.0
