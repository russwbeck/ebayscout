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


def _order(order_id, created, seller, total, txs, status="Completed"):
    return (f"<Order><OrderID>{order_id}</OrderID><OrderStatus>{status}</OrderStatus>"
            f'<Total currencyID="USD">{total}</Total><CreatedTime>{created}</CreatedTime>'
            f"<SellerUserID>{seller}</SellerUserID>"
            f"<TransactionArray>{txs}</TransactionArray></Order>")


def _tx(item_id, title, price, qty="1", ship="", tid="1001", line_id=None):
    line = f"<OrderLineItemID>{line_id}</OrderLineItemID>" if line_id is not None \
        else f"<OrderLineItemID>{item_id}-{tid}</OrderLineItemID>"
    ship_el = f'<ActualShippingCost currencyID="USD">{ship}</ActualShippingCost>' if ship else ""
    return (f"<Transaction><Item><ItemID>{item_id}</ItemID><Title>{title}</Title></Item>"
            f"<QuantityPurchased>{qty}</QuantityPurchased>"
            f'<TransactionPrice currencyID="USD">{price}</TransactionPrice>{ship_el}'
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
        _order("12-111-222", "2026-09-20T18:00:00.000Z", "kling24toys", "26.50",
               _tx("158298813627", "2019 PENN STATE SET", "20.00", ship="6.50")
               + _tx("128083142875", "2025 SET OF 12", "17.00", qty="2", tid="1002"))
        + _order("12-333-444", "2026-09-28T01:02:03.000Z", "someone", "9.75",
                 _tx("267000000001", "1980 Pitt Isn't It", "8.00", ship="1.75")))
    lines, more = pu.parse_page(xml)
    assert more is False
    assert [l["line_item_id"] for l in lines] == ["158298813627-1001", "128083142875-1002",
                                                  "267000000001-1001"]
    first = lines[0]
    assert first["seller"] == "kling24toys" and first["order_id"] == "12-111-222"
    assert first["item_price"] == 20.0 and first["shipping"] == 6.5
    assert first["order_total"] == 26.5 and first["quantity"] == 1
    assert lines[1]["quantity"] == 2 and lines[1]["shipping"] == ""


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

def _line(line_id, when, price=8.0):
    item = line_id.split("-")[0]
    return {"purchased_at": when, "seller": "s", "ebay_id": item, "title": "t",
            "quantity": 1, "item_price": price, "shipping": "", "order_total": price,
            "order_status": "Completed", "order_id": "o", "line_item_id": line_id}


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


# --- the sheet ---------------------------------------------------------------------

def _gc(existing_values=None):
    ss = MagicMock()
    tabs = {}
    if existing_values is not None:
        ws = MagicMock(title=pu.TAB)
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
    assert route.index("_internal_request_ok(request)") < route.index("get_purchases")
    assert '_get_secret("EBAY_USER_TOKEN")' in route
    assert "purchases.write_tab(" in route and '_get_secret("LOGGER_ID")' in route


def test_nothing_but_the_command_pulls_purchases():
    """No schedule, no daily-scan hook: the operator asked for no extra checks."""
    callers = [n.name for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)
               and "get_purchases" in (ast.get_source_segment(_MAIN, n) or "")]
    assert callers == ["internal_purchases"]


def test_the_token_is_never_printed():
    """It can act on the operator's account: no print() may carry it."""
    client_src = open(os.path.join(PKG, "ebay_client.py")).read()
    client_tree = ast.parse(client_src)
    fns = [n for n in ast.walk(client_tree) if isinstance(n, ast.FunctionDef)
           and n.name == "get_purchases"]
    fns += [n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)
            and n.name == "internal_purchases"]
    assert len(fns) == 2
    for fn in fns:
        for call in (c for c in ast.walk(fn) if isinstance(c, ast.Call)
                     and getattr(c.func, "id", "") == "print"):
            names = {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
            assert not names & {"token", "user_token"}, fn.name
