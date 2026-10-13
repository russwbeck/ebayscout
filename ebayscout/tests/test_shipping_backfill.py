"""`/crawl shipping` (2026-10-13): a one-time fill of price_log listing rows'
blank shipping from the Trading API's GetItem.

shipping_backfill.py is tested directly; main.py imports the heavy stack, so its
wiring is read via ast.

Run: python tests/run_shipping_backfill_tests.py
"""

import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import price_log as pl, shipping_backfill as sb   # noqa: E402

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN = open(os.path.join(PKG, "main.py")).read()
_TREE = ast.parse(_MAIN)


def _fn(name):
    f = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_MAIN, f) or ""


def _item(details="", ack="Success", errors=""):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<Ack>{ack}</Ack>{errors}"
            f"<Item><ItemID>1</ItemID>{details}</Item></GetItemResponse>")


def _opt(cost=None, free=False):
    return ("<ShippingServiceOptions>"
            + ("<FreeShipping>true</FreeShipping>" if free else "")
            + (f'<ShippingServiceCost currencyID="USD">{cost}</ShippingServiceCost>'
               if cost is not None else "")
            + "</ShippingServiceOptions>")


def _ship(type_, *opts):
    return f"<ShippingDetails><ShippingType>{type_}</ShippingType>{''.join(opts)}</ShippingDetails>"


def test_the_command_is_its_own_word():
    assert sb.parse_command("shipping") and sb.parse_command(" Shipping ")
    assert not sb.parse_command("800") and not sb.parse_command("shipping 5")


def test_the_cheapest_flat_option_is_the_shipping():
    assert sb.parse_item(_item(_ship("Flat", _opt("5.75"), _opt("4.50")))) == 4.5


def test_free_shipping_is_zero():
    assert sb.parse_item(_item(_ship("Free", _opt("0.0", free=True)))) == 0.0


def test_calculated_domestic_shipping_has_no_price():
    assert sb.parse_item(_item(_ship("Calculated", _opt("0.0")))) is None
    assert sb.parse_item(_item(_ship("CalculatedDomesticFlatInternational", _opt("0.0")))) is None
    assert sb.parse_item(_item(_ship("FlatDomesticCalculatedInternational", _opt("3.95")))) == 3.95


def test_no_stated_shipping_is_none():
    assert sb.parse_item(_item()) is None
    assert sb.parse_item(_item(_ship("Flat", _opt()))) is None


def test_an_item_eBay_wont_return_raises_with_its_message():
    xml = _item(ack="Failure", errors=(
        "<Errors><ShortMessage>Item cannot be accessed.</ShortMessage>"
        "<ErrorCode>17</ErrorCode><SeverityCode>Error</SeverityCode></Errors>"))
    try:
        sb.parse_item(xml)
    except sb.ItemUnavailable as exc:
        assert "17" in str(exc)
        return
    raise AssertionError("expected ItemUnavailable")


def test_the_request_asks_for_one_item_with_the_token_where_it_belongs():
    body = sb.request_xml("AgAA&token", "256123456789")
    assert "<ItemID>256123456789</ItemID>" in body and "AgAA&amp;token" in body
    assert sb.request_headers("AgAA")["X-EBAY-API-CALL-NAME"] == "GetItem"
    oauth = "v^1.1#abc"
    assert oauth not in sb.request_xml(oauth, "1")
    assert sb.request_headers(oauth)["X-EBAY-API-IAF-TOKEN"] == oauth


def test_only_blank_listing_rows_are_asked_about():
    bd = [["kind", "source", "ebay_id"], ["listing", "scan", "111"], ["listing", "scan", "111"],
          ["sold", "scout_sold", "222"], ["listing", "own_store", "333"],
          ["listing", "scan", "444"], ["listing", "scan", ""]]
    ship = [["shipping"], [""], [], [""], ["4.5"]]                   # rows 6-7 trimmed
    assert sb.blank_listing_rows(bd, ship) == {"111": [2, 3], "444": [6]}


def test_cells_write_each_items_shipping_into_its_rows_only():
    letter = pl.col("shipping")
    got = sb.cells({"111": [2, 3], "444": [6], "555": [7]}, {"111": 4.0, "444": 0.0, "555": None})
    assert got == [{"range": f"{letter}2", "values": [[4.0]]},
                   {"range": f"{letter}3", "values": [[4.0]]},
                   {"range": f"{letter}6", "values": [[0.0]]}]


def test_the_slack_line_says_what_happened():
    line = sb.summary_text(900, 700, 2400, 120, 80)
    assert "900 item(s)" in line and "2400 row(s) across 700" in line
    assert "120 with calculated" in line and "80 eBay no longer returns" in line
    assert "already has shipping" in sb.summary_text(0, 0, 0, 0, 0)
    assert "failed: boom" in sb.summary_text(0, 0, 0, 0, 0, error="boom")


def test_crawl_reads_the_shipping_form_and_runs_it_in_its_own_request():
    assert "if shipping_backfill.parse_command(raw):" in _MAIN
    kick = _fn("_start_shipping_backfill")
    assert "/internal/shipping" in kick and "X-Internal-Secret" in kick
    run = _fn("internal_shipping")
    assert "_internal_request_ok(request)" in run and "_backfill_shipping()" in run


def test_the_backfill_writes_raw_and_stops_on_a_bad_token():
    body = _fn("_backfill_shipping")
    assert '_get_secret("EBAY_USER_TOKEN")' in body
    assert 'value_input_option="RAW"' in body
    assert '"token" in str(exc).lower()' in body and "raise" in body
    assert "print(token" not in body and "{token}" not in body
