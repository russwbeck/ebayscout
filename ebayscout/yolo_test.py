"""The YOLO week (operator, 2026-10-15): eBay lots through buttonmatcher's full
Gemini-free process instead of the Gemini Gem.

With ``EBAYSCOUT_YOLO_TEST=1`` the daily feed and ``/crawl`` stop uploading a
lot's photo for the Gem.  Each lot is handed to buttonmatcher instead
(``POST <BUTTONMATCHER_URL>/internal/ebay_scout_lot``, authenticated with the
``PIPELINE_SHARED_SECRET`` the watcher already uses with both services), which
runs it as an automatic Scout lot in #inventory-bot: the photo with the
listing's title, price and link, the count prompt, then the learned detector,
CLIP + OCR, the confirm cards and the Scout valuation against the asking price
(buttonmatcher's ``ebay_handoff.py``).  The lot is marked seen here once
buttonmatcher has accepted it.

This module is pure (the switch, the payload); main.py does the I/O.
"""

from __future__ import annotations

import os

COMMAND = "yolo-test"
ENDPOINT = "/internal/ebay_scout_lot"
DEFAULT_BUTTONMATCHER_URL = "https://button-inventory-404960106109.us-east1.run.app"


def enabled() -> bool:
    """On with EBAYSCOUT_YOLO_TEST=1 (a config-only Cloud Run update)."""
    return os.environ.get("EBAYSCOUT_YOLO_TEST", "0").strip().lower() in (
        "1", "true", "yes", "on")


def buttonmatcher_url() -> str:
    """buttonmatcher's service URL; BUTTONMATCHER_URL overrides the default."""
    return (os.environ.get("BUTTONMATCHER_URL") or DEFAULT_BUTTONMATCHER_URL).rstrip("/")


def handoff_payload(ctx: dict, picture_url: str) -> dict:
    """The body buttonmatcher's /internal/ebay_scout_lot takes."""
    return {
        "image_url": picture_url,
        "item_id":   ctx.get("item_id"),
        "title":     ctx.get("title"),
        "url":       ctx.get("url"),
        "asking":    ctx.get("asking"),
        "shipping":  ctx.get("shipping"),
        "seller":    ctx.get("seller"),
    }
