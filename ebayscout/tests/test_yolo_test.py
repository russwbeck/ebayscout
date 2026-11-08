"""The handoff (yolo_test.py; the YOLO week, 2026-10-15): each eBay lot goes to
buttonmatcher's full Gemini-free process as an automatic Scout lot.  Its
switch went with the Gemini pipeline on 2026-10-25.

yolo_test.py is pure and tested directly; main.py's wiring is read via ast
(main.py imports the heavy stack).  Run: python -m pytest tests/test_yolo_test.py
"""

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

from ebayscout import yolo_test as yt      # noqa: E402

_MAIN = open(os.path.join(PKG, "main.py")).read()
_TREE = ast.parse(_MAIN)


def _fn(name):
    f = next(n for n in ast.walk(_TREE)
             if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_MAIN, f) or ""


def test_there_is_no_switch_back_to_the_gem():
    """EBAYSCOUT_YOLO_TEST=0 fed the Gemini pipeline, which is gone."""
    assert not hasattr(yt, "enabled")
    assert "EBAYSCOUT_YOLO_TEST" not in _MAIN


def test_buttonmatcher_url_default_and_override(monkeypatch):
    monkeypatch.delenv("BUTTONMATCHER_URL", raising=False)
    assert yt.buttonmatcher_url() == yt.DEFAULT_BUTTONMATCHER_URL
    monkeypatch.setenv("BUTTONMATCHER_URL", "https://bm.example/")
    assert yt.buttonmatcher_url() == "https://bm.example"


def test_the_payload_is_what_buttonmatcher_takes():
    ctx = {"item_id": "v1|1|0", "title": "T", "url": "https://ebay.com/itm/1",
           "asking": 16.14, "shipping": 0.0, "seller": "s", "command": "/crawl",
           "buying_options": ["AUCTION"], "bid_count": 3}
    p = yt.handoff_payload(ctx, "https://i.ebayimg.com/x.jpg")
    assert p == {"image_url": "https://i.ebayimg.com/x.jpg", "item_id": "v1|1|0",
                 "title": "T", "url": "https://ebay.com/itm/1", "asking": 16.14,
                 "shipping": 0.0, "seller": "s", "condition": None,
                 "buying_options": ["AUCTION"], "bid_count": 3}


def test_the_feed_hands_every_lot_to_buttonmatcher():
    crawl = _fn("_run_crawl")
    assert "_handoff_lot_to_buttonmatcher(" in crawl
    assert "_feed_lot_to_pipeline" not in _MAIN


def test_a_lot_is_seen_only_once_buttonmatcher_took_it():
    h = _fn("_handoff_lot_to_buttonmatcher")
    assert '"X-Pipeline-Secret": _PIPELINE_SHARED_SECRET' in h
    assert h.index("resp.status_code != 200") < h.index("_mark_item_seen_now(item_id)")



def test_every_handed_over_lot_gets_a_price_log_row():
    """2026-11-08: price_log had only finished Scout lots, so a lot nobody
    finished left no trace, and no row said who listed it.  Each lot handed
    over now writes one row, source handoff, with its seller and condition."""
    from ebayscout import price_log as pl
    ctx = {"key": "k1", "item_id": "v1|960109577823|0", "title": "ELEVEN 1975",
           "url": "https://www.ebay.com/itm/960109577823", "asking": 35.0,
           "shipping": 7.5, "seller": "kling24toys", "condition": "Used",
           "buying_options": ["FIXED_PRICE"], "bid_count": None, "command": "/crawl"}
    rec = yt.handoff_record(ctx, "job-9")
    assert rec["run_id"] == "handoff-crawl-job-9"
    assert yt.handoff_record(dict(ctx, command="daily"), None)["run_id"] == "handoff-daily-k1"
    row = pl.handoff_row(rec)
    h = pl.PRICE_HEADER
    assert row[h.index("source")] == "handoff" and row[h.index("ebay_id")] == "960109577823"
    assert row[h.index("seller")] == "kling24toys" and row[h.index("lot_price")] == 35.0
    assert yt.handoff_payload(ctx, "https://i.ebayimg.com/x.jpg")["condition"] == "Used"


def test_the_feed_writes_the_handoff_rows_in_batches():
    h = _fn("_handoff_lot_to_buttonmatcher")
    assert "yolo_test.handoff_record(ctx, job_id)" in h
    assert h.index("_mark_item_seen_now(item_id)") < h.index("handoff_record(")
    crawl = _fn("_run_crawl")
    assert "handed.append(rec)" in crawl and "_HANDOFF_ROW_BATCH" in crawl
    assert crawl.rstrip().count("_log_handoff_rows(handed)") == 2
    assert '"condition":' in _fn("_lot_ctx")
