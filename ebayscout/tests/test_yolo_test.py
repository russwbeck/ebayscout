"""The YOLO week (yolo_test.py, 2026-10-15): with the switch on, each eBay lot
goes to buttonmatcher's full Gemini-free process as an automatic Scout lot
instead of to the Gem.

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


def test_the_switch_is_off_unless_set(monkeypatch):
    monkeypatch.delenv("EBAYSCOUT_YOLO_TEST", raising=False)
    assert not yt.enabled()
    monkeypatch.setenv("EBAYSCOUT_YOLO_TEST", "1")
    assert yt.enabled()


def test_buttonmatcher_url_default_and_override(monkeypatch):
    monkeypatch.delenv("BUTTONMATCHER_URL", raising=False)
    assert yt.buttonmatcher_url() == yt.DEFAULT_BUTTONMATCHER_URL
    monkeypatch.setenv("BUTTONMATCHER_URL", "https://bm.example/")
    assert yt.buttonmatcher_url() == "https://bm.example"


def test_the_payload_is_what_buttonmatcher_takes():
    ctx = {"item_id": "v1|1|0", "title": "T", "url": "https://ebay.com/itm/1",
           "asking": 16.14, "shipping": 0.0, "seller": "s", "command": "/crawl"}
    p = yt.handoff_payload(ctx, "https://i.ebayimg.com/x.jpg")
    assert p == {"image_url": "https://i.ebayimg.com/x.jpg", "item_id": "v1|1|0",
                 "title": "T", "url": "https://ebay.com/itm/1", "asking": 16.14,
                 "shipping": 0.0, "seller": "s"}


def test_the_feed_hands_lots_off_instead_of_feeding_the_gem_when_on():
    crawl = _fn("_run_crawl")
    assert "if yolo_test.enabled():" in crawl
    assert crawl.index("_handoff_lot_to_buttonmatcher(") < crawl.index("_feed_lot_to_pipeline(")


def test_a_lot_is_seen_only_once_buttonmatcher_took_it():
    h = _fn("_handoff_lot_to_buttonmatcher")
    assert '"X-Pipeline-Secret": _PIPELINE_SHARED_SECRET' in h
    assert h.index("resp.status_code != 200") < h.index("_mark_item_seen_now(item_id)")
