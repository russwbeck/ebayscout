"""The YOLO week (yolo_test.py, 2026-10-15): eBay lots read by the learned
detector + CLIP instead of the Gem, every lot carded behind a gate.

yolo_test.py is pure and tested directly; main.py's wiring is read via ast
(main.py imports the heavy stack).  Run: python -m pytest tests/test_yolo_test.py
"""

import ast
import json
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


def _state(**kw):
    s = {"key": "abc123", "job_id": "yolo-abc123",
         "listing": {"item_id": "1", "title": "PSU <buttons> & more",
                     "url": "https://ebay.com/itm/1", "asking": 20.0,
                     "shipping": 4.5, "seller": "s"},
         "picture_url": "https://i.ebayimg.com/x.jpg",
         "model_count": 3, "confirmed": [], "unconfirmed": [], "needed": [],
         "lot_value": 0.0, "undervalued": False, "deal": False}
    s.update(kw)
    return s


def _actions(blocks):
    return [e["action_id"] for b in blocks if b["type"] == "actions"
            for e in b["elements"]]


def test_the_switch_is_off_unless_set(monkeypatch):
    monkeypatch.delenv("EBAYSCOUT_YOLO_TEST", raising=False)
    assert not yt.enabled()
    monkeypatch.setenv("EBAYSCOUT_YOLO_TEST", "1")
    assert yt.enabled()


def test_the_card_offers_the_three_answers_and_hides_the_model_count():
    blocks = yt.card_blocks(_state())
    assert _actions(blocks) == [yt.ACTION_BUTTON, yt.ACTION_SKIP, yt.ACTION_REJECT]
    labels = [e["text"]["text"] for b in blocks if b["type"] == "actions"
              for e in b["elements"]]
    assert labels == ["✅ Button", "⏭️ Button, skip", "🚫 Not a button, reject"]
    assert any(b["type"] == "image" for b in blocks)
    text = json.dumps(blocks, ensure_ascii=False)
    assert "model" not in text.lower(), "the operator counts before seeing the model's count"
    assert "&lt;buttons&gt; &amp;" in text           # the title is escaped


def test_a_deal_is_flagged_on_the_card():
    assert "Possible deal" in json.dumps(yt.card_blocks(_state(deal=True)),
                                         ensure_ascii=False)
    assert "Possible deal" not in json.dumps(yt.card_blocks(_state()),
                                             ensure_ascii=False)


def test_an_answered_card_has_no_buttons_left():
    blocks = yt.answered_blocks(_state(), "button", 4, "U1")
    assert _actions(blocks) == []
    assert "you counted *4*" in json.dumps(blocks, ensure_ascii=False)


def test_the_count_modal_and_its_parse():
    m = yt.count_modal("abc123", "C1", "1.2")
    assert m["callback_id"] == yt.VIEW_COUNT
    assert json.loads(m["private_metadata"]) == {"key": "abc123", "channel": "C1", "ts": "1.2"}
    st = lambda v: {"values": {"count_block": {"count": {"value": v}}}}
    assert yt.parse_count(st("5")) == 5 and yt.parse_count(st("0")) == 0
    assert yt.parse_count(st("-1")) is None and yt.parse_count(st("x")) is None
    assert yt.parse_count({}) is None


def test_the_result_compares_counts_and_gives_the_deal_call():
    s = _state(model_count=5, deal=True, undervalued=True, lot_value=40.0,
               needed=[{"year": 1985, "slogan": "Go"}],
               confirmed=[{"n": 1, "year": 1985, "slogan": "Go", "overall": 0.88,
                           "amount_needed": 2}],
               unconfirmed=[{"n": 2, "year": 1990, "slogan": "Hi", "overall": 0.51}])
    out = yt.result_text(s, 4)
    assert "You counted *4* · the model counted *5* (1 too many)" in out
    assert "#1 1985 _Go_ (0.88) · *needed ×2*" in out
    assert "#2 1990 _Hi_ (0.51)" in out
    assert "🔥 *Deal*" in out and "1 needed button(s)" in out
    assert "(exact)" in yt.count_line(3, 3) and "2 too few" in yt.count_line(5, 3)


def test_the_feed_cards_lots_instead_of_feeding_the_gem_when_on():
    crawl = _fn("_run_crawl")
    assert "if yolo_test.enabled():" in crawl
    assert crawl.index("_yolo_test_lot(") < crawl.index("_feed_lot_to_pipeline(")


def test_a_lot_is_read_by_the_model_and_logged_like_a_pipeline_lot():
    lot = _fn("_yolo_test_lot")
    for needle in ("ldet.shadow_count(det, _ldet_fetch)",
                   "ldet.learned_crops(reading, det, dw, dh)",
                   "pipeline_classify.classify_crops(diagnostics, {}, False, job_id)",
                   "pipeline_classify.lot_value_and_deal(",
                   "lharv.build_label_record(",
                   "price_logger.log_listing(record)",
                   "seen_items.append_scan_log([record])",
                   "_mark_item_seen_now(item_id)"):
        assert needle in lot, needle
    # state is saved before the card can be clicked
    assert lot.index("_gcs_put_json(yolo_test.state_blob(key), state)") < \
        lot.index("chat_postMessage(")


def test_every_answer_reaches_the_training_label():
    rec = _fn("_yolo_record_answer")
    assert '["operator"] = ans' in rec and "chat_update(" in rec
    assert "yolo_test.ACTION_SKIP" in _MAIN and "yolo_test.ACTION_REJECT" in _MAIN
    view = _fn("handle_yolo_count")
    assert 'response_action="errors"' in view and "yolo_test.result_text(" in view
