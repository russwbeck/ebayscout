"""`/crawl purchases [days]` (2026-10-04): the operator's own eBay purchases,
pulled on demand into a merged `purchases` tab of the Logger workbook.

purchases.py and ebay_client.get_purchases are tested directly; main.py imports
the heavy stack, so its wiring is read via ast.

Run: python tests/run_purchases_tests.py
"""

import ast
import datetime
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import config, ebay_client, purchases as pu   # noqa: E402

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN = open(os.path.join(PKG, "main.py")).read()
_TREE = ast.parse(_MAIN)

NOW = datetime.datetime(2026, 10, 4, 15, 30, tzinfo=datetime.timezone.utc)
AUTHNAUTH = "AgAAAA**AQAAAA**aAAAAA**test+token/with=chars&more"
OAUTH = "v^1.1#i^1#r^1#p^3#f^0#t^Ul4xMF8xOk="


def _fn(name):
    f = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_MAIN, f) or ""


def _response(orders="", ack="Success", more="false", errors=""):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<GetOrdersResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<Ack>{ack}</Ack>{errors}"
            f"<OrderArray>{orders}</OrderArray>"
            f"<HasMoreOrders>{more}</HasMoreOrders>"
            "</GetOrdersResponse>")


def _order(order_id, created, seller, total, txs, status="Completed", subtotal="",
           shipping="", sales_tax=""):
    sub = f'<Subtotal currencyID="USD">{subtotal}</Subtotal>' if subtotal else ""
    ship = (f'<ShippingServiceSelected><ShippingServiceCost currencyID="USD">{shipping}'
            f"</ShippingServiceCost></ShippingServiceSelected>") if shipping else ""
    tax = (f'<ShippingDetails><SalesTax><SalesTaxAmount currencyID="USD">{sales_tax}'
           f"</SalesTaxAmount></SalesTax></ShippingDetails>") if sales_tax else ""
    return (f"<Order><OrderID>{order_id}</OrderID><OrderStatus>{status}</OrderStatus>"
            f"{tax}{ship}{sub}"
            f'<Total currencyID="USD">{total}</Total><CreatedTime>{created}</CreatedTime>'
            f"<SellerUserID>{seller}</SellerUserID>"
            f"<TransactionArray>{txs}</TransactionArray></Order>")


def _tx(item_id, title, price, qty="1", ship="", tid="1001", line_id=None, tax="",
        tax_as="eBayCollectAndRemitTaxes"):
    line = f"<OrderLineItemID>{line_id}</OrderLineItemID>" if line_id is not None \
        else f"<OrderLineItemID>{item_id}-{tid}</OrderLineItemID>"
    ship_el = f'<ActualShippingCost currencyID="USD">{ship}</ActualShippingCost>' if ship else ""
    tax_el = (f'<{tax_as}><TotalTaxAmount currencyID="USD">{tax}</TotalTaxAmount></{tax_as}>'
              if tax else "")
    return (f"<Transaction><Item><ItemID>{item_id}</ItemID><Title>{title}</Title></Item>"
            f"<QuantityPurchased>{qty}</QuantityPurchased>"
            f'<TransactionPrice currencyID="USD">{price}</TransactionPrice>{ship_el}{tax_el}'
            f"<TransactionID>{tid}</TransactionID>{line}</Transaction>")


# --- the command -------------------------------------------------------------------

def test_the_purchases_form_is_read_and_a_lot_count_keeps_its_meaning():
    assert pu.parse_command("purchases") == (True, 90, None)
    assert pu.parse_command("  Purchases 30 ") == (True, 30, None)
    assert pu.parse_command("purchase:7") == (True, 7, None)
    assert pu.parse_command("800") == (False, None, None)
    assert pu.parse_command("seller kling24toys") == (False, None, None)
    assert pu.parse_command("") == (False, None, None)


def test_a_day_count_outside_eBays_window_is_refused():
    for bad in ("purchases 0", "purchases 91", "purchases 365", "purchases all"):
        is_p, days, err = pu.parse_command(bad)
        assert is_p and days is None and "1–90" in err, bad


# --- the request -------------------------------------------------------------------

def test_the_window_stays_inside_90_days():
    frm, to = pu.window(90, NOW)
    assert to == "2026-10-04T15:30:00.000Z"
    assert frm == "2026-07-06T15:40:00.000Z"


def test_an_authnauth_token_goes_in_the_body_escaped_and_asks_as_the_buyer():
    body = pu.request_xml(AUTHNAUTH, 90, 2, NOW)
    assert "<eBayAuthToken>AgAAAA**AQAAAA**aAAAAA**test+token/with=chars&amp;more</eBayAuthToken>" in body
    assert "<OrderRole>Buyer</OrderRole>" in body
    assert "<PageNumber>2</PageNumber>" in body
    assert "<EntriesPerPage>100</EntriesPerPage>" in body
    assert "X-EBAY-API-IAF-TOKEN" not in pu.request_headers(AUTHNAUTH)
    assert pu.request_headers(AUTHNAUTH)["X-EBAY-API-CALL-NAME"] == "GetOrders"


def test_an_oauth_token_goes_in_the_header_and_not_the_body():
    assert "RequesterCredentials" not in pu.request_xml(OAUTH, 30, 1, NOW)
    assert pu.request_headers(OAUTH)["X-EBAY-API-IAF-TOKEN"] == OAUTH


# --- the response -------------------------------------------------------------------

def test_each_line_item_of_a_combined_order_is_its_own_row():
    xml = _response(
        _order("12-111-222", "2026-09-20T18:00:00.000Z", "kling24toys", "62.66",
               _tx("158298813627", "2019 PENN STATE SET", "20.00", ship="6.50", tax="1.80")
               + _tx("128083142875", "2025 SET OF 12", "17.00", qty="2", tid="1002", tax="2.36"),
               subtotal="54.00", shipping="4.50")
        + _order("12-333-444", "2026-09-28T01:02:03.000Z", "someone", "9.75",
                 _tx("267000000001", "1980 Pitt Isn't It", "8.00", ship="1.75")))
    lines, more = pu.parse_page(xml)
    assert more is False
    assert [l["line_item_id"] for l in lines] == ["158298813627-1001", "128083142875-1002",
                                                  "267000000001-1001"]
    first = lines[0]
    assert first["seller"] == "kling24toys" and first["order_id"] == "12-111-222"
    assert first["item_price"] == 20.0 and first["quantity"] == 1
    assert lines[1]["item_price"] == 17.0 and lines[1]["quantity"] == 2
    for line in lines[:2]:   # every line carries the order's money; merge keeps one
        assert (line["order_subtotal"], line["order_shipping"], line["order_tax"],
                line["order_total"]) == (54.0, 4.5, 4.16, 62.66)
    assert "shipping" not in first


def test_the_shipping_is_what_the_order_charged_not_each_listings_own():
    # Two lines whose listings each say $8.75 shipping, combined into one $8.75
    # charge: the order-level figure is the one that adds up to the total.
    lines, _ = pu.parse_page(_response(_order(
        "16-1", "2026-09-24T20:15:33.000Z", "s", "33.97",
        _tx("318900000001", "a", "12.00", ship="8.75", tax="1.45")
        + _tx("318900000002", "b", "10.00", ship="8.75", tid="1002", tax="1.77"),
        subtotal="22.00", shipping="8.75")))
    d = lines[0]
    assert d["order_shipping"] == 8.75
    assert round(d["order_subtotal"] + d["order_shipping"] + d["order_tax"], 2) == d["order_total"]


def _money_of(subtotal, shipping, tax, total):
    lines, _ = pu.parse_page(_response(_order(
        "9", "2026-09-24T20:15:33.000Z", "s", total,
        _tx("318900000009", "t", subtotal, ship=shipping, tax=tax),
        subtotal=subtotal, shipping=shipping)))
    d = lines[0]
    return d["order_shipping"], d["order_tax"], d["order_total"]


def test_a_combined_shipping_discount_eBay_left_out_is_taken_off_the_shipping():
    # The two live orders (2026-10-06) whose parts came to more than the total:
    # shipping and tax are re-solved at the order's own tax rate.
    assert _money_of("12.00", "8.75", "1.45", "12.84") == (0.0, 0.84, 12.84)
    assert _money_of("14.88", "15.10", "2.10", "21.38") == (5.1, 1.4, 21.38)


def test_parts_that_add_up_or_a_mismatch_that_isnt_shipping_are_left_alone():
    assert _money_of("68.00", "10.00", "5.46", "83.46") == (10.0, 5.46, 83.46)
    assert _money_of("20.00", "8.75", "1.45", "20.21") == (8.75, 1.45, 20.21)   # over shipping
    assert _money_of("20.00", "8.75", "1.45", "35.00") == (8.75, 1.45, 35.0)    # total higher
    assert _money_of("29.00", "0.00", "", "25.00") == (0.0, "", 25.0)           # no shipping


def test_the_tax_is_counted_once_per_line_and_falls_back_to_the_orders_own():
    both = ('<eBayCollectAndRemitTaxes><TotalTaxAmount currencyID="USD">0.84</TotalTaxAmount>'
            '</eBayCollectAndRemitTaxes><Taxes><TotalTaxAmount currencyID="USD">0.84'
            '</TotalTaxAmount></Taxes>')
    tx = _tx("267000000003", "t", "12.00").replace("<TransactionID>", both + "<TransactionID>")
    lines, _ = pu.parse_page(_response(_order("1", "2026-09-01T00:00:00.000Z", "s", "12.84", tx)))
    assert lines[0]["order_tax"] == 0.84                       # not 1.68
    lines, _ = pu.parse_page(_response(_order(
        "2", "2026-09-01T00:00:00.000Z", "s", "10.60",
        _tx("267000000004", "t", "10.00", tax="0.60", tax_as="Taxes"))))
    assert lines[0]["order_tax"] == 0.6
    lines, _ = pu.parse_page(_response(_order(
        "3", "2026-09-01T00:00:00.000Z", "s", "10.70",
        _tx("267000000005", "t", "10.00"), sales_tax="0.70")))
    assert lines[0]["order_tax"] == 0.7
    lines, _ = pu.parse_page(_response(_order(
        "4", "2026-09-01T00:00:00.000Z", "s", "10.00", _tx("267000000006", "t", "10.00"))))
    assert lines[0]["order_tax"] == "" and lines[0]["order_shipping"] == ""


def test_a_line_without_an_OrderLineItemID_is_keyed_by_item_and_transaction():
    lines, _ = pu.parse_page(_response(_order("1", "2026-09-01T00:00:00.000Z", "s", "5",
                                              _tx("267000000002", "t", "5", tid="77",
                                                  line_id=""))))
    assert lines[0]["line_item_id"] == "267000000002-77"


def test_a_failure_carries_eBays_message():
    xml = _response(ack="Failure", errors=(
        "<Errors><ShortMessage>Auth token is invalid.</ShortMessage>"
        "<LongMessage>Validation of the authentication token in API request failed.</LongMessage>"
        "<ErrorCode>931</ErrorCode><SeverityCode>Error</SeverityCode></Errors>"))
    try:
        pu.parse_page(xml)
    except pu.PurchasesError as exc:
        assert "931" in str(exc) and "authentication token" in str(exc)
        return
    raise AssertionError("expected PurchasesError")


def test_a_warning_is_not_a_failure():
    lines, more = pu.parse_page(_response(ack="Warning", more="true"))
    assert lines == [] and more is True


# --- rows and the merge --------------------------------------------------------------

def _line(line_id, when, price=8.0, order=None):
    item = line_id.split("-")[0]
    return {"purchased_at": when, "seller": "s", "ebay_id": item, "title": "t",
            "quantity": 1, "item_price": price, "order_subtotal": price,
            "order_shipping": 1.0, "order_tax": 0.5, "order_total": price + 1.5,
            "order_status": "Completed", "order_id": order or f"o-{line_id}",
            "line_item_id": line_id}


def test_a_row_is_in_header_order_with_the_listing_link():
    r = pu.row(_line("267000000001-1001", "2026-09-28T01:02:03.000Z"), "P")
    assert len(r) == len(pu.HEADER)
    d = dict(zip(pu.HEADER, r))
    assert d["ebay_id"] == "267000000001"
    assert d["listing_url"] == "https://www.ebay.com/itm/267000000001"
    assert d["pulled_at"] == "P" and d["line_item_id"] == "267000000001-1001"


def test_the_merge_keeps_rows_eBay_no_longer_returns_and_refreshes_repeats():
    old = [pu.row(_line("267000000001-1", "2026-05-01T00:00:00.000Z"), "old"),
           pu.row(_line("267000000002-1", "2026-09-01T00:00:00.000Z", 5.0), "old")]
    new = [pu.row(_line("267000000002-1", "2026-09-01T00:00:00.000Z", 6.0), "new"),
           pu.row(_line("267000000003-1", "2026-10-01T00:00:00.000Z"), "new")]
    merged = pu.merge(old, new)
    keys = [r[pu.KEY] for r in merged]
    assert keys == ["267000000003-1", "267000000002-1", "267000000001-1"]   # newest first
    assert dict(zip(pu.HEADER, merged[1]))["item_price"] == 6.0             # refreshed
    assert dict(zip(pu.HEADER, merged[2]))["pulled_at"] == "old"            # kept


def test_an_orders_money_is_on_its_first_line_only():
    when = "2026-09-25T00:40:27.000Z"
    combined = [pu.row(_line(f"40615948312{n}-1", when, order="06-1"), "P") for n in range(3)]
    single = pu.row(_line("267000000009-1", "2026-09-26T00:00:00.000Z"), "P")
    merged = pu.merge([], combined + [single])
    money = [[r[i] for i in pu.ORDER_MONEY] for r in merged]
    assert merged[0][pu.KEY] == "267000000009-1" and money[0] == [8.0, 1.0, 0.5, 9.5]
    assert [r[pu.ORDER] for r in merged[1:]] == ["06-1"] * 3          # kept together
    assert money[1] == [8.0, 1.0, 0.5, 9.5] and money[2] == money[3] == [""] * 4
    assert [r[pu.HEADER.index("item_price")] for r in merged] == [8.0] * 4
    assert combined[1][pu.ORDER_MONEY[-1]] == 9.5                     # caller's rows untouched


# --- the sheet ---------------------------------------------------------------------

def _gc(existing_values=None):
    ss = MagicMock()
    tabs = {}
    if existing_values is not None:
        ws = MagicMock(title=pu.TAB, row_count=100, col_count=len(pu.HEADER))
        ws.get_all_values.return_value = existing_values
        tabs[pu.TAB] = ws
    ss.worksheets.return_value = list(tabs.values())
    ss.add_worksheet.side_effect = lambda title, rows, cols: tabs.setdefault(title, MagicMock(title=title))
    gc = MagicMock()
    gc.open_by_key.return_value = ss
    return gc, ss, tabs


def test_a_first_pull_creates_the_tab():
    gc, ss, tabs = _gc()
    rows = [pu.row(_line("267000000001-1", "2026-09-01T00:00:00.000Z"), "P")]
    assert pu.write_tab(gc, "https://docs.google.com/spreadsheets/d/KEY9/edit", rows) == ("purchases", 1, 1)
    gc.open_by_key.assert_called_once_with("KEY9")
    tabs[pu.TAB].update.assert_called_once_with(values=[pu.HEADER] + rows, range_name="A1",
                                                value_input_option="RAW")


def test_a_later_pull_merges_into_the_tab():
    kept = [str(v) for v in pu.row(_line("267000000001-1", "2026-05-01T00:00:00.000Z"), "old")]
    gc, ss, tabs = _gc(existing_values=[pu.HEADER, kept, [""] * len(pu.HEADER)])
    new = [pu.row(_line("267000000009-1", "2026-10-01T00:00:00.000Z"), "new")]
    assert pu.write_tab(gc, "KEY", new) == ("purchases", 1, 2)
    ss.add_worksheet.assert_not_called()
    written = tabs[pu.TAB].update.call_args.kwargs["values"]
    assert [r[pu.KEY] for r in written[1:]] == ["267000000009-1", "267000000001-1"]
    tabs[pu.TAB].get_all_values.assert_called_once_with(value_render_option="UNFORMATTED_VALUE")
    tabs[pu.TAB].resize.assert_not_called()


def test_a_tab_from_before_the_order_columns_is_relaid():
    # The first pull (2026-10-04) wrote one `shipping` per line and the order's
    # total on every line.  Read back unformatted, its numbers stay numbers.
    old_header = ["purchased_at", "seller", "ebay_id", "title", "quantity", "item_price",
                  "shipping", "order_total", "order_status", "order_id", "line_item_id",
                  "listing_url", "pulled_at"]
    old = [["2026-07-07T00:09:27.000Z", "g", "117261226188", "a", 1, 25, 1.4, 120.87,
            "Completed", "26-1", "117261226188-1", "u", "P0"],
           ["2026-07-07T00:09:27.000Z", "g", "117261234267", "b", 1, 15, 0.84, 120.87,
            "Completed", "26-1", "117261234267-1", "u", "P0"]]
    gc, ss, tabs = _gc(existing_values=[old_header] + old)
    tabs[pu.TAB].col_count = len(old_header)
    new = [pu.row(_line("267000000009-1", "2026-10-01T00:00:00.000Z"), "new")]
    assert pu.write_tab(gc, "KEY", new) == ("purchases", 1, 3)
    tabs[pu.TAB].resize.assert_called_once_with(rows=100, cols=len(pu.HEADER))
    written = tabs[pu.TAB].update.call_args.kwargs["values"]
    assert written[0] == pu.HEADER
    kept = [dict(zip(pu.HEADER, r)) for r in written[2:]]
    assert [k["item_price"] for k in kept] == [25, 15]
    assert [k["order_total"] for k in kept] == [120.87, ""]          # once per order
    assert kept[0]["order_shipping"] == "" and kept[0]["pulled_at"] == "P0"


def test_the_slack_line_says_what_happened():
    assert "Pulled 3 purchase line items from the last 90 days" in pu.summary_text(90, 3, 2, 10)
    assert "2 new, 10 in the tab" in pu.summary_text(90, 3, 2, 10)
    assert "failed: boom" in pu.summary_text(90, 0, 0, 0, error="boom")


# --- the eBay call -------------------------------------------------------------------

def test_the_call_pages_until_eBay_says_no_more():
    pages = {1: _response(_order("1", "2026-09-01T00:00:00.000Z", "s", "5",
                                 _tx("267000000001", "a", "5")), more="true"),
             2: _response(_order("2", "2026-09-02T00:00:00.000Z", "s", "6",
                                 _tx("267000000002", "b", "6")), more="false")}
    calls = []

    def fake_post(url, data, headers, timeout):
        body = data.decode()
        page = int(body.split("<PageNumber>")[1].split("<")[0])
        calls.append((url, page, headers.get("X-EBAY-API-CALL-NAME")))
        r = MagicMock(text=pages[page])
        r.raise_for_status.return_value = None
        return r

    with patch.object(ebay_client.requests, "post", side_effect=fake_post):
        lines = ebay_client.get_purchases(AUTHNAUTH, 90, now=NOW)
    assert [l["ebay_id"] for l in lines] == ["267000000001", "267000000002"]
    assert calls == [(config.EBAY_TRADING_URL, 1, "GetOrders"),
                     (config.EBAY_TRADING_URL, 2, "GetOrders")]


def test_a_failed_page_fails_the_pull_instead_of_writing_part_of_it():
    r = MagicMock(text=_response(ack="Failure", errors=(
        "<Errors><ShortMessage>bad</ShortMessage><ErrorCode>931</ErrorCode>"
        "<SeverityCode>Error</SeverityCode></Errors>")))
    r.raise_for_status.return_value = None
    with patch.object(ebay_client.requests, "post", return_value=r):
        try:
            ebay_client.get_purchases(AUTHNAUTH, 90, now=NOW)
        except pu.PurchasesError:
            return
    raise AssertionError("expected the pull to raise")


# --- main.py wiring ------------------------------------------------------------------

def test_crawl_reads_the_purchases_form_before_a_lot_count():
    body = _fn("handle_crawl_command")
    assert body.index("purchases.parse_command(raw)") < body.index("n = int(raw)")
    assert "_start_purchases_pull(ack, days, purchases_err)" in body


def test_the_pull_runs_inside_its_own_authenticated_request_with_the_user_token():
    kick = _fn("_start_purchases_pull")
    assert "/internal/purchases" in kick and "X-Internal-Secret" in kick
    route = _fn("internal_purchases")
    assert route.index("_internal_request_ok(request)") < route.index("_pull_purchases(days)")
    pull = _fn("_pull_purchases")
    assert '_get_secret("EBAY_USER_TOKEN")' in pull and "get_purchases(" in pull
    assert "purchases.write_tab(" in pull and '_get_secret("LOGGER_ID")' in pull


def test_only_the_command_and_the_monday_scan_pull_purchases():
    """The operator asked for Mondays with the 9 AM scan, and no other schedule."""
    def callers(needle, skip=()):
        return sorted(n.name for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)
                      and n.name not in skip
                      and needle in (ast.get_source_segment(_MAIN, n) or ""))
    assert callers("get_purchases(") == ["_pull_purchases"]
    assert callers("_pull_purchases(", skip=("_pull_purchases",)) == ["internal_purchases",
                                                                     "run_scan"]


def test_the_scan_pulls_only_on_a_plain_live_monday_run():
    scan = _fn("run_scan")
    day = scan.index("purchases.is_pull_day(")
    assert day < scan.index("_scan_lock.acquire(")          # read before the scan runs
    call = scan.index("_pull_purchases(purchases.MAX_DAYS, scheduled=True)")
    guard = scan.rindex("if purchases_day and plain_run and not dry:", 0, call)
    assert scan.index("_run_crawl(") < guard                 # after the scan itself
    assert "plain_run = not (year_crawl or era_crawl or hunt_ids or ignore_seen or limit)" in scan
    assert call < scan.index("_scan_lock.release()")          # still inside the request


def test_monday_is_the_pull_day():
    monday_9am_et = datetime.datetime(2026, 10, 5, 13, 0, tzinfo=datetime.timezone.utc)
    assert pu.is_pull_day(monday_9am_et)
    assert not pu.is_pull_day(monday_9am_et - datetime.timedelta(days=1))   # Sunday
    assert not pu.is_pull_day(monday_9am_et + datetime.timedelta(days=1))   # Tuesday
    winter = datetime.datetime(2026, 12, 7, 14, 0, tzinfo=datetime.timezone.utc)  # EST
    assert pu.is_pull_day(winter)


def test_the_monday_run_is_quiet_until_the_token_exists():
    pull = _fn("_pull_purchases")
    quiet = pull.index("if scheduled:")
    assert quiet < pull.index("return {\"status\": \"skipped\"") < pull.index("notifier.send_text(")
    assert "raise RuntimeError" in pull                     # the command still says why


def test_the_token_is_never_printed():
    """It can act on the operator's account: no print() may carry it."""
    client_src = open(os.path.join(PKG, "ebay_client.py")).read()
    client_tree = ast.parse(client_src)
    fns = [n for n in ast.walk(client_tree) if isinstance(n, ast.FunctionDef)
           and n.name == "get_purchases"]
    fns += [n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)
            and n.name == "_pull_purchases"]
    assert len(fns) == 2
    for fn in fns:
        for call in (c for c in ast.walk(fn) if isinstance(c, ast.Call)
                     and getattr(c.func, "id", "") == "print"):
            names = {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
            assert not names & {"token", "user_token"}, fn.name
