"""The YOLO week (operator, 2026-10-15): eBay lots through the learned detector
instead of the Gemini Gem, every lot posted to #ebay-checker behind a gate.

With ``EBAYSCOUT_YOLO_TEST=1`` the daily feed and ``/crawl`` stop uploading a
lot's photo for the Gem.  Each lot is read here instead, inside the scan's own
request: buttonmatcher's learned detector (``learned_detector.py``, the same
ONNX model) cuts the crops, CLIP matches them, and the CLIP-only rules confirm
them (``pipeline_classify.classify_crops`` with no Gemini reading).  The lot is
then posted as ONE card: the listing's own photo, unannotated, so the count is
the operator's and not the model's, and three answers:

  ✅ Button                -> "How many buttons do you see?" -> the result in
                              the card's thread: the operator's count against
                              the model's, the photo with the model's numbered
                              boxes, the buttons CLIP confirmed, the deal call
  ⏭️ Button, skip          -> buttons, not reviewed
  🚫 Not a button, reject  -> no buttons in the photo

Everything a click needs is in GCS (``STATE_PREFIX<key>.json``), never only in
memory: a card is answered hours later, after a scale-to-zero.  The answer is
also written into the lot's label sidecar (``pipeline/labels/<job_id>.json``,
``detection.operator``), so a counted lot is a YOLO training example: the
model's full reading, the photo, and the operator's count of what is really
there.

This module is pure (blocks, text, records); main.py does the I/O.
"""

from __future__ import annotations

import os

STATE_PREFIX = "ebay_scout/yolo_test/"
COMMAND = "yolo-test"

ACTION_BUTTON = "yolo_button"
ACTION_SKIP = "yolo_skip"
ACTION_REJECT = "yolo_reject"
VIEW_COUNT = "yolo_count_modal"

VERDICTS = {
    ACTION_BUTTON: "button",
    ACTION_SKIP: "button_skip",
    ACTION_REJECT: "not_a_button",
}


def enabled() -> bool:
    """On with EBAYSCOUT_YOLO_TEST=1 (a config-only Cloud Run update)."""
    return os.environ.get("EBAYSCOUT_YOLO_TEST", "0").strip().lower() in (
        "1", "true", "yes", "on")


def state_blob(key: str) -> str:
    return f"{STATE_PREFIX}{key}.json"


def _money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "?"


def _esc(text) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def card_blocks(state: dict) -> list[dict]:
    """The lot's card: title, price, the listing photo, the three answers.

    The model's count is NOT on the card (the operator counts first); a lot
    CLIP and the buy rules call a deal says so, so a deal is never missed for
    want of a click."""
    lst = state.get("listing") or {}
    title = _esc(lst.get("title") or "(no title)")
    url = lst.get("url") or ""
    head = f"*<{url}|{title}>*" if url else f"*{title}*"
    ship = lst.get("shipping")
    line2 = (f"{_money(lst.get('asking'))}"
             + (f" + {_money(ship)} shipping" if ship not in (None, "") else "")
             + f" · {_esc(lst.get('seller') or '?')} · item {lst.get('item_id')}")
    blocks = [{"type": "section", "text": {"type": "mrkdwn",
                                           "text": f"{head}\n{line2}"}}]
    if state.get("picture_url"):
        blocks.append({"type": "image", "image_url": state["picture_url"],
                       "alt_text": "listing photo"})
    if state.get("deal"):
        blocks.append({"type": "context", "elements": [{
            "type": "mrkdwn",
            "text": "🔥 *Possible deal* — tap ✅ Button for the result."}]})
    val = state["key"]
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "style": "primary", "action_id": ACTION_BUTTON,
         "text": {"type": "plain_text", "text": "✅ Button"}, "value": val},
        {"type": "button", "action_id": ACTION_SKIP,
         "text": {"type": "plain_text", "text": "⏭️ Button, skip"}, "value": val},
        {"type": "button", "style": "danger", "action_id": ACTION_REJECT,
         "text": {"type": "plain_text", "text": "🚫 Not a button, reject"},
         "value": val},
    ]})
    blocks.append({"type": "context", "elements": [{
        "type": "mrkdwn", "text": "_YOLO test · no Gemini_"}]})
    return blocks


def answered_blocks(state: dict, verdict: str, count=None, user_id=None) -> list[dict]:
    """The card once answered: the same head and photo, the answer in place of
    the buttons."""
    blocks = [b for b in card_blocks(state) if b.get("type") != "actions"]
    who = f" by <@{user_id}>" if user_id else ""
    text = {"button": f"✅ Button — you counted *{count}*{who}; result in the thread",
            "button_skip": f"⏭️ Button, skipped{who}",
            "not_a_button": f"🚫 Not a button, rejected{who}"}.get(verdict, verdict)
    blocks.insert(-1, {"type": "context", "elements": [
        {"type": "mrkdwn", "text": text}]})
    return blocks


def count_modal(key: str, channel: str, ts: str) -> dict:
    """The one question ✅ Button asks: how many buttons are in the photo."""
    import json
    return {
        "type": "modal", "callback_id": VIEW_COUNT,
        "private_metadata": json.dumps({"key": key, "channel": channel, "ts": ts}),
        "title": {"type": "plain_text", "text": "YOLO test"},
        "submit": {"type": "plain_text", "text": "Show result"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [{
            "type": "input", "block_id": "count_block",
            "label": {"type": "plain_text", "text": "How many buttons do you see?"},
            "element": {"type": "number_input", "action_id": "count",
                        "is_decimal_allowed": False, "min_value": "0"},
        }],
    }


def parse_count(view_state: dict):
    """The typed count as an int >= 0, or None."""
    try:
        v = view_state["values"]["count_block"]["count"]["value"]
        n = int(str(v).strip())
    except (KeyError, TypeError, ValueError):
        return None
    return n if n >= 0 else None


def count_line(operator: int, model: int) -> str:
    d = model - operator
    if d == 0:
        how = "exact"
    else:
        how = f"{abs(d)} {'too many' if d > 0 else 'too few'}"
    return f"You counted *{operator}* · the model counted *{model}* ({how})."


def result_text(state: dict, operator_count: int) -> str:
    """The thread reply after ✅ Button: count, confirmed buttons, deal call,
    and the crops CLIP would not confirm with its best guess."""
    lines = [count_line(operator_count, int(state.get("model_count") or 0))]
    confirmed = state.get("confirmed") or []
    if confirmed:
        lines.append(f"*CLIP confirmed {len(confirmed)}:*")
        for b in confirmed:
            need = (f" · *needed ×{b['amount_needed']}*"
                    if b.get("amount_needed") else "")
            lines.append(f"  #{b['n']} {b['year']} _{_esc(b['slogan'])}_ "
                         f"({b['overall']:.2f}){need}")
    else:
        lines.append("*CLIP confirmed none.*")
    unconf = state.get("unconfirmed") or []
    if unconf:
        lines.append("Not confirmed (best guess): " + "; ".join(
            f"#{u['n']} {u['year']} _{_esc(u['slogan'])}_ ({u['overall']:.2f})"
            if u.get("slogan") else f"#{u['n']} —" for u in unconf))
    asking = (state.get("listing") or {}).get("asking")
    value = state.get("lot_value") or 0.0
    verdict = ("🔥 *Deal*" if state.get("deal") else "No deal")
    why = []
    if state.get("needed"):
        why.append(f"{len(state['needed'])} needed button(s)")
    if state.get("undervalued"):
        why.append(f"matched value {_money(value)} > asking {_money(asking)}")
    lines.append(f"{verdict}" + (f" — {', '.join(why)}" if why else
                                 f" — matched value {_money(value)}, asking {_money(asking)}"))
    return "\n".join(lines)


def answer_record(verdict: str, count=None, user_id=None, ts=None) -> dict:
    """What a click adds to the lot's state and its label sidecar."""
    return {"verdict": verdict, "count": count, "user_id": user_id, "ts": ts}
