"""`/crawl seller <username>` (2026-10-03): one seller's Penn State listings,
pulled on demand into a seller_<username> tab for price comparison.

seller_listings.py and ebay_client.find_seller_listings are tested directly;
main.py imports the heavy stack, so its wiring is read via ast.

Run: python tests/run_seller_listings_tests.py
"""

import ast
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import config, ebay_client, seller_listings as sl   # noqa: E402

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

def test_a_row_carries_the_item_number_price_shipping_and_format():
    row = sl.summary_row(_item(shipping={"shippingCostType": "FIXED",
                                         "shippingCost": {"value": "4.50"}}),
                         "2026-10-03T12:00:00+00:00", "kling24toys")
    assert row == ["2026-10-03T12:00:00+00:00", "kling24toys", "267770621737",
                   "2008 Penn State Citizens Bank Buttons Season Set", 12.0, 4.5,
                   "buy_it_now", "", "Used", "https://www.ebay.com/itm/267770621737"]
    assert len(row) == len(sl.HEADER)


def test_an_auction_row_keeps_its_bid_count():
    row = sl.summary_row(_item(buying=("AUCTION",), bids=3), "t", "s")
    assert row[sl.HEADER.index("format")] == "auction"
    assert row[sl.HEADER.index("bids")] == 3


def test_shipping_is_a_number_calculated_or_blank():
    assert sl.shipping_cost(_item(shipping={"shippingCostType": "CALCULATED"})) == "calculated"
    assert sl.shipping_cost(_item()) == ""
    assert sl.shipping_cost(_item(shipping={"shippingCost": {"value": "0.00"}})) == 0.0


def test_a_summary_without_an_item_number_is_skipped():
    item = _item()
    item["itemId"] = ""
    assert sl.summary_row(item, "t", "s") is None


# --- the sheet ---------------------------------------------------------------------

def _gc(existing_titles=()):
    ss = MagicMock()
    tabs = {t: MagicMock(title=t) for t in existing_titles}
    ss.worksheets.return_value = list(tabs.values())
    ss.add_worksheet.side_effect = lambda title, rows, cols: tabs.setdefault(title, MagicMock(title=title))
    gc = MagicMock()
    gc.open_by_key.return_value = ss
    return gc, ss, tabs


def test_a_first_pull_creates_the_tab_and_writes_raw():
    gc, ss, tabs = _gc()
    rows = [sl.summary_row(_item(), "t", "kling24toys")]
    title = sl.write_tab(gc, "https://docs.google.com/spreadsheets/d/KEY123/edit", "Kling24Toys", rows)
    assert title == "seller_kling24toys"
    gc.open_by_key.assert_called_once_with("KEY123")
    ws = tabs["seller_kling24toys"]
    ws.update.assert_called_once_with(values=[sl.HEADER] + rows, range_name="A1",
                                      value_input_option="RAW")


def test_a_later_pull_replaces_the_tab_rather_than_appending():
    """Prices change and listings end: the tab is the seller's stock as of now."""
    gc, ss, tabs = _gc(existing_titles=("seller_kling24toys",))
    sl.write_tab(gc, "KEY", "kling24toys", [])
    ws = tabs["seller_kling24toys"]
    ws.clear.assert_called_once()
    ss.add_worksheet.assert_not_called()
    ws.update.assert_called_once()


def test_the_slack_line_says_what_happened():
    assert "Pulled 2 Penn State listings from *kling24toys*" in sl.summary_text("kling24toys", 2, "seller_kling24toys")
    assert "no Penn State listings" in sl.summary_text("kling24toys", 0, "seller_kling24toys")
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


def test_kling24toys_is_not_excluded_from_the_scan():
    """Removed 2026-10-03: its lots belong in scan_log and price_log."""
    assert "kling24toys" not in {s.lower() for s in config.EXCLUDED_SELLERS}
