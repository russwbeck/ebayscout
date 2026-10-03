"""`/crawl catchup` (2026-10-03): still-listed lots read before price logging
began get the price rows they never had, once, without re-alerting or
re-staging anything.

catchup.py is pure and tested directly; main.py imports the heavy stack, so its
wiring is read via ast.  Run: python tests/run_catchup_tests.py
"""

import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

from ebayscout import catchup, config      # noqa: E402

_MAIN = open(os.path.join(PKG, "main.py")).read()
_TREE = ast.parse(_MAIN)
CUT = config.PRICE_LOG_START


def _fn(name):
    f = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_MAIN, f) or ""


def _listing(number):
    return {"item_id": f"v1|{number}|0", "listing_url": f"https://www.ebay.com/itm/{number}"}


# --- the command ------------------------------------------------------------------

def test_the_command_is_read_and_a_lot_count_keeps_its_meaning():
    assert catchup.parse_command("catchup") == (True, 0, None)
    assert catchup.parse_command("  CATCHUP 50 ") == (True, 50, None)
    assert catchup.parse_command("catch-up 7") == (True, 7, None)
    assert catchup.parse_command("800") == (False, None, None)
    assert catchup.parse_command("seller kling24toys") == (False, None, None)


def test_a_number_that_is_not_one_is_refused():
    is_c, n, err = catchup.parse_command("catchup lots")
    assert is_c and n is None and "whole number" in err


# --- which lots qualify -------------------------------------------------------------

def test_the_last_seen_mark_is_the_one_that_counts():
    seen = {"a": "2026-07-01", "b": ["2026-06-01", "2026-10-02"], "c": []}
    assert catchup.last_seen_date("a", seen) == "2026-07-01"
    assert catchup.last_seen_date("b", seen) == "2026-10-02"
    assert catchup.last_seen_date("c", seen) is None
    assert catchup.last_seen_date("zzz", seen) is None


def test_only_listing_rows_count_as_logged():
    rows = [["kind", "source", "ebay_id"],
            ["listing", "scan", "111111111111"],
            ["sold", "scout_sold", "222222222222"],
            ["listing", "own_store"],                     # trimmed: no id
            []]
    assert catchup.logged_listing_numbers(rows) == {"111111111111"}


def test_a_lot_read_before_price_logging_with_no_row_qualifies():
    seen = {"v1|300000000001|0": "2026-08-01"}
    assert catchup.candidates([_listing(300000000001)], seen, set(), CUT) == [_listing(300000000001)]


def test_an_unseen_lot_is_left_to_the_daily_scan():
    assert catchup.candidates([_listing(300000000002)], {}, set(), CUT) == []


def test_a_lot_seen_since_price_logging_began_does_not_qualify():
    """This is what makes it one-time: a fed lot is marked seen today."""
    seen = {"v1|300000000003|0": ["2026-08-01", "2026-10-03"]}
    assert catchup.candidates([_listing(300000000003)], seen, set(), CUT) == []


def test_a_lot_that_already_has_a_listing_row_does_not_qualify():
    seen = {"v1|300000000004|0": "2026-08-01"}
    assert catchup.candidates([_listing(300000000004)], seen, {"300000000004"}, CUT) == []


def test_the_order_given_is_kept():
    seen = {f"v1|30000000000{i}|0": "2026-07-0{i}" for i in range(5, 9)}
    ls = [_listing(300000000000 + i) for i in (8, 5, 7)]
    assert catchup.candidates(ls, seen, set(), CUT) == ls


def test_the_slack_lines_say_the_cost_and_what_is_left():
    assert "one Gemini read" in catchup.preview_text(12, 400, 1000)
    assert "Nothing to feed" in catchup.preview_text(0, 400, 1000)
    assert "8 more qualify" in catchup.summary_text(4, 4, 12)
    assert "all of them" in catchup.summary_text(4, 4, 4)


# --- wiring ---------------------------------------------------------------------------

def test_crawl_reads_the_catchup_form_before_a_lot_count():
    body = _fn("handle_crawl_command")
    assert body.index("catchup.parse_command(raw)") < body.index("n = int(raw)")


def test_catchup_runs_inside_its_own_authenticated_request_under_the_scan_lock():
    body = _fn("internal_catchup")
    assert body.index("_internal_request_ok(request)") < body.index("_run_catchup(")
    assert "_scan_lock.acquire(blocking=False)" in body
    assert "min(config.CRAWL_MAX_LOTS_CAP" in body
    assert "/internal/catchup" in _fn("_start_catchup")


def test_a_bare_command_feeds_nothing():
    run = _fn("_run_catchup")
    assert run.index("if n <= 0:") < run.index("_feed_lot_to_pipeline(")


def test_fed_lots_are_marked_seen_at_feed_time():
    run = _fn("_run_catchup")
    feed = run.index("_feed_lot_to_pipeline(")
    assert feed < run.index("_flush_seen_marks(fed_ids, flushed)")
    assert "len(fed_ids) - flushed >= 25" in run           # checkpointed, not only at the end
    assert run.index("_flush_seen_marks(fed_ids, flushed)") < run.index("catchup.summary_text(")


def test_no_price_log_read_means_no_feed():
    run = _fn("_run_catchup")
    read = run.index("catchup.logged_listing_numbers(")
    assert read < run.index("_feed_lot_to_pipeline(")
    assert "couldn't read price_log" in run


def test_catchup_searches_deep():
    run = _fn("_run_catchup")
    assert run.count("max_pages=config.CRAWL_MAX_PAGES") == 2   # daily + /crawl phrases


def test_a_catchup_lot_posts_no_alert_and_stages_no_crop():
    body = _fn("process_pipeline_lot")
    assert 'is_catchup = ctx.get("command") == catchup.COMMAND' in body
    assert "post_alerts = not is_catchup" in body
    assert "if needed_hits and post_alerts:" in body
    assert "if undervalued and post_alerts:" in body
    assert "alerted=bool(needed_hits or undervalued) and post_alerts" in body
    assert "stageable = ([] if is_catchup else pipeline_classify.staging_candidates(" in body


def test_a_catchup_lot_writes_no_second_set_of_training_records():
    body = _fn("process_pipeline_lot")
    assert "if lharv.harvest_enabled() and not is_catchup:" in body
    assert "if not is_catchup:\n        seen_items.write_crop_vectors(" in body
    assert "if match_logger is not None and diagnostics and not is_catchup:" in body
    assert "if not is_catchup:\n        _log_pipeline_count(" in body


def test_a_catchup_lot_still_writes_its_price_rows():
    body = _fn("process_pipeline_lot")
    flag = body.index('record["catchup"] = True')
    assert flag < body.index("price_logger.log_listing(record)") < body.index("append_scan_log([record])")


def test_nothing_but_the_command_runs_a_catchup():
    callers = [m.start() for m in re.finditer(r"_run_catchup\(", _MAIN)]
    defs = [m.start() for m in re.finditer(r"def _run_catchup\(", _MAIN)]
    assert len(callers) - len(defs) == 1          # only internal_catchup
    assert "_run_catchup(" not in _fn("run_scan")
