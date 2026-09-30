"""ebayscout's side of price_log (2026-09-30): every priced pipeline lot writes
its listing rows, and nothing else runs for prices.

price_log.py itself is shared and tested in test_price_log.py.  main.py imports
the heavy stack, so its wiring is read via ast.

Run: python tests/run_price_log_wiring_tests.py
"""

import ast
import os

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN = open(os.path.join(PKG, "main.py")).read()
_TREE = ast.parse(_MAIN)


def _fn(name):
    f = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_MAIN, f) or ""


def test_startup_opens_the_price_tab_in_the_logging_workbook():
    body = _fn("startup")
    assert "global buy_rules, match_logger, price_logger" in body
    assert "price_log.open_price_sheet(gclient, logger_id)" in body
    assert "price_logger = price_log.PriceLogger(None)" in body   # fail-open


def test_every_priced_lot_logs_its_listing_from_its_scan_log_record():
    body = _fn("process_pipeline_lot")
    built = body.index("record = _scan_log_record(")
    listed = body.index("price_logger.log_listing(record)")
    assert built < listed < body.index("append_scan_log([record])")
    assert body.index('record["run_id"] = run_id') < listed      # the row carries it


def test_the_listing_row_knows_an_auction_from_an_asking_price():
    """An auction's price at scan time is a bid; price_log keeps it out of the
    listing average, which needs the format from the feed."""
    feed = _fn("_feed_lot_to_pipeline")
    assert '"buying_options": listing.get("buying_options")' in feed
    assert '"bid_count":      listing.get("bid_count")' in feed
    assert '"buying_options": ctx.get("buying_options")' in _fn("process_pipeline_lot")


def test_no_sold_price_checks_run_here():
    """Sold prices come from buttonmatcher's /scout sold.  The auction tracker
    (a watch list and eBay lookups at each close) was built and removed on
    2026-09-30 at the operator's request: the 9 AM scan does its usual work
    and nothing more."""
    for gone in ("auction_watch", "/check-auctions", "_settle_auctions",
                 "get_auction_state", "AUCTION_WATCH_BLOB"):
        assert gone not in _MAIN, gone
    assert not os.path.exists(os.path.join(PKG, "auction_watch.py"))
