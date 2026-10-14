"""`/crawl seller <username>` (2026-10-03): one seller's Penn State listings,
pulled on demand into price_log (its own seller_<username> tab until
2026-10-14) for price comparison.

seller_listings.py and ebay_client.find_seller_listings are tested directly;
main.py imports the heavy stack, so its wiring is read via ast.

Run: python tests/run_seller_listings_tests.py
"""

import ast
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import config, ebay_client, price_log as pl, seller_listings as sl   # noqa: E402

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN = open(os.path.join(PKG, "main.py")).read()
_TREE = ast.parse(_MAIN)


def _fn(name):
    f = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_MAIN, f) or ""


def _item(number="267770621737", title="2008 Penn State Citizens Bank Buttons Season Set",
          price="12.00", buying=("FIXED_PRICE",), shipping=None, bids=None):
    item = {
        "itemId": f"v1|{number}|0",
        "title": title,
        "price": {"value": price, "currency": "USD"},
        "buyingOptions": list(buying),
        "condition": "Used",
        "itemWebUrl": f"https://www.ebay.com/itm/{number}",
        "seller": {"username": "kling24toys"},
    }
    if shipping is not None:
        item["shippingOptions"] = [shipping]
    if bids is not None:
        item["bidCount"] = bids
    return item


# --- the command -----------------------------------------------------------------

def test_the_seller_form_is_read_and_a_lot_count_keeps_its_meaning():
    assert sl.parse_command("seller kling24toys") == (True, "kling24toys")
    assert sl.parse_command("seller:kling24toys") == (True, "kling24toys")
    assert sl.parse_command("  SELLER   kling24toys ") == (True, "kling24toys")
    assert sl.parse_command("800") == (False, None)
    assert sl.parse_command("") == (False, None)
    assert sl.parse_command("sellerx") == (False, None)


def test_a_seller_form_without_a_valid_username_is_refused():
    """The name goes into the Browse filter string, so braces and spaces are typos."""
    for bad in ("seller", "seller ", "seller two words", "seller a}b", "seller a|b"):
        assert sl.parse_command(bad) == (True, None), bad


def test_each_page_is_filtered_to_the_seller():
    p = sl.search_params("kling24toys", "penn state", 400)
    assert p == {"q": "penn state", "filter": "sellers:{kling24toys}",
                 "limit": "200", "offset": "400"}


# --- rows --------------------------------------------------------------------------

def _at(row, name):
    return row[pl.PRICE_HEADER.index(name)]


def test_a_row_is_a_price_log_listing_row_from_the_seller():
    row = sl.summary_row(_item(shipping={"shippingCostType": "FIXED",
                                         "shippingCost": {"value": "4.50"}}),
                         "2026-10-03T12:00:00+00:00", "Kling24Toys")
    assert len(row) == len(pl.PRICE_HEADER)
    assert row == ["2026-10-03T12:00:00+00:00", "listing", "seller", "267770621737",
                   "", "", "", "", 12.0, 4.5, "", "", "buy_it_now", "", "", "asking", "no",
                   "2008 Penn State Citizens Bank Buttons Season Set",
                   "https://www.ebay.com/itm/267770621737",
                   "seller-kling24toys-2026-10-03T12:00:00+00:00"]


def test_a_seller_row_never_enters_the_per_slogan_average():
    """No price per button: the average's QUERY reads only rows that have one."""
    row = sl.summary_row(_item(), "t", "s")
    assert _at(row, "price_per_button") == "" and _at(row, "year") == "" and _at(row, "slogan") == ""
    assert "is not null" in pl.avg_formula()


def test_an_auction_row_keeps_its_bid_count():
    row = sl.summary_row(_item(buying=("AUCTION",), bids=3), "t", "s")
    assert _at(row, "sale_format") == "auction"
    assert _at(row, "bids") == 3


def test_calculated_shipping_goes_in_blank_as_on_a_scan_row():
    row = sl.summary_row(_item(shipping={"shippingCostType": "CALCULATED"}), "t", "s")
    assert _at(row, "shipping") == ""


def test_shipping_is_a_number_calculated_or_blank():
    assert sl.shipping_cost(_item(shipping={"shippingCostType": "CALCULATED"})) == "calculated"
    assert sl.shipping_cost(_item()) == ""
    assert sl.shipping_cost(_item(shipping={"shippingCost": {"value": "0.00"}})) == 0.0


def test_a_summary_without_an_item_number_is_skipped():
    item = _item()
    item["itemId"] = ""
    assert sl.summary_row(item, "t", "s") is None


# --- the sheet ---------------------------------------------------------------------

class _PriceTab:
    title = "price_log"

    def __init__(self, header):
        self.header, self.appended = header, []

    def row_values(self, i):
        return list(self.header) if i == 1 else []

    def append_rows(self, rows, value_input_option=None):
        self.appended.append((rows, value_input_option))


def _gc(header=None, others=("price_analysis",)):
    log = _PriceTab(pl.PRICE_HEADER if header is None else header)
    ss = MagicMock(title="Logger")
    ss.worksheets.return_value = [log] + [MagicMock(title=t) for t in others]
    gc = MagicMock()
    gc.open_by_key.return_value = ss
    return gc, ss, log


def test_a_pull_appends_to_price_log_raw_and_makes_no_tab_of_its_own():
    gc, ss, log = _gc()
    rows = [sl.summary_row(_item(), "t", "kling24toys")]
    title = sl.write_tab(gc, "https://docs.google.com/spreadsheets/d/KEY123/edit", "Kling24Toys", rows)
    assert title == "price_log" == sl.tab_name("Kling24Toys")
    gc.open_by_key.assert_called_once_with("KEY123")
    assert log.appended == [(rows, "RAW")]
    ss.add_worksheet.assert_not_called()


def test_an_empty_pull_writes_nothing():
    gc, ss, log = _gc()
    sl.write_tab(gc, "KEY", "kling24toys", [])
    assert log.appended == []


def test_a_price_log_with_another_header_fails_the_pull_loudly():
    gc, ss, log = _gc(header=["ts", "item_id"])
    try:
        sl.write_tab(gc, "KEY", "kling24toys", [["x"]])
    except RuntimeError as e:
        assert "price_log" in str(e)
    else:
        raise AssertionError("expected a failure")
    assert log.appended == []


def test_the_slack_line_says_what_happened():
    assert "Pulled 2 Penn State listings from *kling24toys*" in sl.summary_text("kling24toys", 2, "price_log")
    assert "no Penn State listings" in sl.summary_text("kling24toys", 0, "price_log")
    assert "failed: boom" in sl.summary_text("kling24toys", 0, "t", error="boom")


# --- the eBay search ----------------------------------------------------------------

def _resp(items):
    r = MagicMock()
    r.json.return_value = {"itemSummaries": items}
    return r


def test_the_search_pages_dedupes_and_keeps_an_excluded_seller():
    """EXCLUDED_SELLERS is not applied: an excluded seller's prices are still wanted."""
    full_page = [_item(number=str(267000000000 + i)) for i in range(sl.PAGE_SIZE)]
    pages = {("penn state", "0"): full_page,
             ("penn state", "200"): [_item(number="267999999999")],
             ("psu", "0"): [_item(number="267000000000")],            # duplicate
             ("nittany", "0"): [_item(number="267888888888", title="Penn State hoodie")]}
    calls = []

    def fake_get(url, params, headers):
        calls.append(params)
        return _resp(pages.get((params["q"], params["offset"]), []))

    with patch.object(ebay_client, "_get_app_token", return_value="tok"), \
         patch.object(ebay_client, "_get_with_retry", side_effect=fake_get):
        items = ebay_client.find_seller_listings("id", "secret", "kling24toys")

    numbers = {i["itemId"].split("|")[1] for i in items}
    assert len(items) == sl.PAGE_SIZE + 1
    assert "267999999999" in numbers and "267888888888" not in numbers   # apparel dropped
    assert all(p["filter"] == "sellers:{kling24toys}" for p in calls)
    assert [(p["q"], p["offset"]) for p in calls] == [
        ("penn state", "0"), ("penn state", "200"), ("psu", "0"), ("nittany", "0")]


def test_a_failed_page_fails_the_pull_instead_of_writing_part_of_it():
    with patch.object(ebay_client, "_get_app_token", return_value="tok"), \
         patch.object(ebay_client, "_get_with_retry", side_effect=RuntimeError("503")):
        try:
            ebay_client.find_seller_listings("id", "secret", "kling24toys")
        except RuntimeError:
            return
    raise AssertionError("expected the pull to raise")


# --- main.py wiring ------------------------------------------------------------------

def test_crawl_reads_the_seller_form_before_a_lot_count():
    body = _fn("handle_crawl_command")
    assert body.index("seller_listings.parse_command(raw)") < body.index("n = int(raw)")
    assert "_start_seller_pull(ack, seller)" in body


def test_the_pull_runs_inside_its_own_authenticated_request():
    kick = _fn("_start_seller_pull")
    assert '/internal/seller' in kick and "X-Internal-Secret" in kick
    route = _fn("internal_seller")
    assert route.index("_internal_request_ok(request)") < route.index("find_seller_listings")
    assert "seller_listings.write_tab(" in route and '_get_secret("LOGGER_ID")' in route


def test_nothing_but_the_command_pulls_a_seller():
    """No schedule, no daily-scan hook: the operator asked for no extra checks."""
    callers = [n.name for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)
               and "find_seller_listings" in (ast.get_source_segment(_MAIN, n) or "")]
    assert callers == ["internal_seller"]


def test_no_seller_is_excluded_from_the_scan():
    """Emptied 2026-10-03: every seller's lots belong in scan_log and price_log."""
    assert config.EXCLUDED_SELLERS == []
