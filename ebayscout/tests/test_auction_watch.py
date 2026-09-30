"""The auction tracker (2026-09-30): auctions the scan priced, followed to their
close, and their sale written to price_log.

auction_watch.py is pure and tested directly; ebay_client.get_auction_state
with requests patched out; main.py imports the heavy stack, so its wiring is
read as source.

Run: python tests/run_auction_watch_tests.py
"""

import ast
import datetime
import os
import sys
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from ebayscout import auction_watch as aw
from ebayscout import ebay_client
from ebayscout import price_log as pl

UTC = datetime.timezone.utc
END = "2026-09-22T18:00:00.000Z"
T_END = datetime.datetime(2026, 9, 22, 18, 0, tzinfo=UTC)


def _record(**kw):
    rec = {"ts": "2026-09-20T12:00:00+00:00", "item_id": "v1|256123456789|0",
           "title": "Vintage 1975 Penn State", "listing_url": "https://www.ebay.com/itm/256123456789",
           "asking": 9.99, "buttons_detected": 12, "price_per_button": 0.83,
           "buttons": [{"year": 1975, "slogan": "Temple Hoo?", "n": 1},
                       {"year": 1975, "slogan": "Skin the Cat", "n": 2}],
           "run_id": "r1"}
    rec.update(kw)
    return rec


def _ctx(**kw):
    ctx = {"buying_options": ["AUCTION"], "bid_count": 0, "end_date": END}
    ctx.update(kw)
    return ctx


def _entry(**kw):
    e = aw.entry_for_lot(_record(), _ctx())
    e.update(kw)
    return e


# --- what gets watched ------------------------------------------------------------------

def test_a_priced_auction_is_watched_with_the_scans_button_breakdown():
    e = aw.entry_for_lot(_record(), _ctx())
    assert e["ebay_id"] == "256123456789" and e["item_id"] == "v1|256123456789|0"
    assert e["end_date"] == END and e["buttons_detected"] == 12
    assert e["buttons"] == _record()["buttons"] and e["last_seen"] is None


def test_only_priced_auctions_with_a_named_button_are_watched():
    assert aw.entry_for_lot(_record(), _ctx(buying_options=["FIXED_PRICE"])) is None
    assert aw.entry_for_lot(_record(), _ctx(buying_options=[])) is None
    assert aw.entry_for_lot(_record(buttons=[]), _ctx()) is None
    assert aw.entry_for_lot(_record(buttons_detected=0), _ctx()) is None
    assert aw.entry_for_lot(_record(item_id="?", listing_url=""), _ctx()) is None


# --- when to look -------------------------------------------------------------------------

def test_an_auction_is_looked_at_in_its_last_hour_and_after_it_closes():
    e = _entry()
    assert aw.due(e, T_END - datetime.timedelta(hours=3)) == "wait"
    assert aw.due(e, T_END - datetime.timedelta(minutes=50)) == "snapshot"
    assert aw.due(e, T_END) == "settle"
    assert aw.due(e, T_END + datetime.timedelta(hours=5)) == "settle"
    assert aw.due(_entry(end_date=None), T_END) == "snapshot"   # learn the end


def test_soonest_close_first_and_unknown_ends_before_all():
    watch = {"a": {"end_date": "2026-09-25T00:00:00Z"}, "b": {"end_date": END},
             "c": {"end_date": None}}
    assert aw.order(watch) == ["c", "b", "a"]


# --- what a look means ----------------------------------------------------------------------

def _live(bid, bids, end=END, reserve=None):
    return {"status": "live", "current_bid": bid, "bid_count": bids,
            "end_date": end, "reserve_met": reserve}


def test_a_look_before_the_close_records_the_bid_and_keeps_watching():
    e = _entry()
    now = T_END - datetime.timedelta(minutes=30)
    assert aw.apply_state(e, _live(31.0, 4), now) == "keep"
    assert e["last_seen"]["bid"] == 31.0 and e["last_seen"]["bids"] == 4


def test_ebay_still_showing_the_closed_auction_gives_the_final_price():
    e = _entry()
    assert aw.apply_state(e, _live(39.0, 1), T_END + datetime.timedelta(minutes=20)) == "sold"
    assert e["result"] == {"price": 39.0, "bids": 1, "basis": "final"}


def test_gone_after_the_close_settles_on_the_last_bid_seen():
    e = _entry(last_seen={"ts": "x", "bid": 31.0, "bids": 4, "reserve_met": None})
    assert aw.apply_state(e, {"status": "gone"}, T_END + datetime.timedelta(minutes=5)) == "sold"
    assert e["result"] == {"price": 31.0, "bids": 4, "basis": "last_seen"}


def test_no_bid_or_an_unmet_reserve_is_unsold():
    assert aw.apply_state(_entry(), _live(9.99, 0), T_END) == "unsold"
    assert aw.apply_state(_entry(), _live(45.0, 3, reserve=False), T_END) == "unsold"
    assert aw.apply_state(_entry(), _live(45.0, 3, reserve=True), T_END) == "sold"


def test_nothing_to_learn_is_dropped():
    # gone before its end: pulled or ended early
    assert aw.apply_state(_entry(), {"status": "gone"},
                          T_END - datetime.timedelta(hours=2)) == "drop"
    # gone after the close, but never seen live near it
    assert aw.apply_state(_entry(), {"status": "gone"}, T_END) == "drop"


def test_ebay_errors_are_retried_until_two_days_past_the_close():
    assert aw.apply_state(_entry(), {"status": "error"}, T_END + datetime.timedelta(hours=3)) == "keep"
    assert aw.apply_state(_entry(), {"status": "error"}, T_END + datetime.timedelta(days=3)) == "drop"
    assert aw.expired(_entry(), T_END + datetime.timedelta(days=3))
    assert not aw.expired(_entry(), T_END + datetime.timedelta(hours=3))


def test_a_look_learns_a_missing_end_date():
    e = _entry(end_date=None)
    assert aw.apply_state(e, _live(5.0, 1), T_END - datetime.timedelta(hours=9)) == "keep"
    assert e["end_date"] == END


# --- the rows ------------------------------------------------------------------------------

def _col(row, name):
    return row[pl.PRICE_HEADER.index(name)]


def test_a_sale_is_spread_over_every_detected_button():
    e = _entry(result={"price": 39.0, "bids": 1, "basis": "final"})
    rows = aw.sold_rows(e)
    assert len(rows) == 2
    for r in rows:
        assert _col(r, "kind") == "sold" and _col(r, "source") == "auction"
        assert _col(r, "price_per_button") == 3.25          # $39 / 12
        assert _col(r, "ebay_id") == "256123456789" and _col(r, "sale_date") == "2026-09-22"
        assert _col(r, "bids") == 1 and _col(r, "price_basis") == "final"
        assert _col(r, "sale_format") == "auction" and _col(r, "superseded") == "no"
    assert {_col(r, "n_in_lot") for r in rows} == {1, 2}


def test_no_rows_without_a_result():
    assert aw.sold_rows(_entry()) == []


# --- eBay ---------------------------------------------------------------------------------

def _resp(status, payload):
    r = MagicMock(status_code=status)
    r.json.return_value = payload
    return r


def _look(resp):
    with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
         patch("ebayscout.ebay_client.requests.get", return_value=resp):
        return ebay_client.get_auction_state("id", "secret", "v1|256123456789|0")


def test_a_live_auction_reports_its_bid_bids_end_and_reserve():
    s = _look(_resp(200, {"currentBidPrice": {"value": "31.00"}, "bidCount": 4,
                          "itemEndDate": END, "reservePriceMet": True,
                          "price": {"value": "31.00"}}))
    assert s == {"status": "live", "current_bid": 31.0, "price": 31.0, "bid_count": 4,
                 "end_date": END, "reserve_met": True}


def test_missing_fields_read_as_unknown_not_as_zero_bids_met():
    s = _look(_resp(200, {"bidCount": None, "currentBidPrice": {"value": "nan"}}))
    assert s["current_bid"] is None and s["bid_count"] == 0 and s["reserve_met"] is None


def test_a_404_is_gone_and_anything_else_is_an_error():
    assert _look(_resp(404, {"errors": [{"errorId": 11001}]}))["status"] == "gone"
    assert _look(_resp(500, {}))["status"] == "error"
    with patch("ebayscout.ebay_client._get_app_token", side_effect=RuntimeError("x")):
        assert ebay_client.get_auction_state("i", "s", "1")["status"] == "error"


def test_the_search_keeps_an_auctions_end_date():
    item = {"itemId": "v1|256123456789|0", "title": "Penn State buttons", "seller": {"username": "s"},
            "price": {"value": "9.99", "currency": "USD"}, "itemWebUrl": "u",
            "buyingOptions": ["AUCTION"], "bidCount": 0, "itemEndDate": END}
    with patch("ebayscout.ebay_client._get_app_token", return_value="TOK"), \
         patch("ebayscout.ebay_client.requests.get",
               return_value=_resp(200, {"itemSummaries": [item], "total": 1})):
        got = ebay_client.find_listings("id", "secret", "penn state button", [])
    assert got[0]["end_date"] == END and got[0]["buying_options"] == ["AUCTION"]


# --- main is wired to it --------------------------------------------------------------------

_MAIN = open(os.path.join(os.path.dirname(HERE), "main.py")).read()
_TREE = ast.parse(_MAIN)


def _fn(name):
    f = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_MAIN, f) or ""


def test_the_feed_keeps_what_the_tracker_needs():
    body = _fn("_feed_lot_to_pipeline")
    for field in ('"buying_options"', '"bid_count"', '"end_date"'):
        assert field in body, field


def test_every_priced_lot_logs_its_listing_then_goes_to_the_watch_list():
    body = _fn("process_pipeline_lot")
    built = body.index("record = _scan_log_record(")
    listed = body.index("price_logger.log_listing(record)")
    assert built < listed < body.index("append_scan_log([record])")
    assert body.index('record["run_id"] = run_id') < listed
    assert body.index("_watch_auction(record, ctx)") > listed
    assert "record = None" in body[:built]          # never unbound on a failed build
    # the scan_log row learns the format, so an auction's bid isn't read as a price
    assert '"buying_options": ctx.get("buying_options")' in body


def test_startup_opens_the_price_tab_in_the_logging_workbook():
    body = _fn("startup")
    assert "global buy_rules, match_logger, price_logger" in body
    assert "price_log.open_price_sheet(gclient, logger_id)" in body
    assert "price_logger = price_log.PriceLogger(None)" in body   # fail-open


def test_the_tracker_route_is_authorized_like_run_scan():
    body = _fn("check_auctions")
    assert body.index("_run_scan_authorized(request)") < body.index("_check_auctions()")
    assert "_watch_lock.acquire(" in body and "_watch_lock.release()" in body


def test_a_sale_goes_through_the_shared_count_once_path():
    body = _fn("_check_auctions")
    assert "price_logger.log_sale(rows, eid, source=price_log.SOURCE_AUCTION)" in body
    assert "auction_watch.MAX_CHECKS_PER_RUN" in body
    # an unreadable list is never overwritten with an empty one
    assert body.index("if watch is None:") < body.index("save_auction_watch(watch)")


def test_the_scheduler_may_address_its_token_to_the_tracker():
    assert '[f"{b}/check-auctions" for b in bases]' in _fn("_scheduler_token_ok")
