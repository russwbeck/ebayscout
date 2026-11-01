"""The handoff to buttonmatcher (named for the YOLO week, operator 2026-10-15,
when it was a test): every eBay lot goes through buttonmatcher's full
Gemini-free process.

The daily feed and ``/crawl`` hand each lot to buttonmatcher
(``POST <BUTTONMATCHER_URL>/internal/ebay_scout_lot``, authenticated with the
``PIPELINE_SHARED_SECRET`` both services hold), which runs it as an automatic
Scout lot in #inventory-bot: the photo with the listing's title, price and
link, the count prompt, then the learned detector, CLIP + OCR, the confirm
cards and the Scout valuation against the asking price (buttonmatcher's
``ebay_handoff.py``).  The lot is marked seen here once buttonmatcher has
accepted it.

``EBAYSCOUT_YOLO_TEST`` switched between this and the Gemini pipeline until
2026-10-25, when the pipeline and the switch were removed.

This module is pure (the payload); main.py does the I/O.
"""

from __future__ import annotations

import os

COMMAND = "yolo-test"
ENDPOINT = "/internal/ebay_scout_lot"
DEFAULT_BUTTONMATCHER_URL = "https://button-inventory-404960106109.us-east1.run.app"


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
        # price_log keeps auctions out of the averages by these
        "buying_options": ctx.get("buying_options") or [],
        "bid_count": ctx.get("bid_count"),
    }
