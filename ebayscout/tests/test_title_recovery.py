"""ebayscout/title_recovery.py: `/crawl titles`, the titles of lots already
handed to buttonmatcher, looked up again by item number."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ebayscout import title_recovery as tr  # noqa: E402

LOG = """
2026-11-10T14:01:11Z >>> EBAY_LOT: v1|237173788601|0 -> Scout job ed9f0edd-0e4c-4024-9508-789c40dc18b2
>>> EBAY_LOT: 307274713875 -> Scout job 0f33e1fb-b5df-49e5-a2cb-cf95c4aea7c4
>>> EBAY_LOT: v1|237173788601|0 not_a_button by U0A1 (job ed9f0edd-0e4c-4024-9508-789c40dc18b2)
!!! EBAY_LOT: photo download failed for v1|1|0: timeout
>>> EBAY_LOT: None -> Scout job aaaa
>>> EBAY_LOT: v1|237173788601|0 -> Scout job ed9f0edd-0e4c-4024-9508-789c40dc18b2
>>> EBAY_LOT: v1|237173788601|0 -> Scout job 11111111-relisted
"""


def test_only_the_titles_form_is_this_command():
    assert tr.parse_command("titles") and tr.parse_command("  Titles ")
    for other in ("800", "seller kling24toys", "titles now", "shipping", "", None):
        assert not tr.parse_command(other), other


def test_hand_off_lines_are_read_each_job_once():
    assert tr.parse_jobs(LOG) == [
        ("ed9f0edd-0e4c-4024-9508-789c40dc18b2", "v1|237173788601|0"),
        ("0f33e1fb-b5df-49e5-a2cb-cf95c4aea7c4", "307274713875"),
        ("11111111-relisted", "v1|237173788601|0"),
    ]
    assert tr.parse_jobs("") == [] and tr.parse_jobs(None) == []
    assert len(tr.parse_jobs(LOG, limit=1)) == 1


def test_a_bare_item_number_is_wrapped_for_browse():
    assert tr.browse_id("307274713875") == "v1|307274713875|0"
    assert tr.browse_id("v1|237173788601|0") == "v1|237173788601|0"


def test_titles_are_collected_for_listings_that_still_exist():
    calls = []

    def fetch(item):
        calls.append(item)
        if item == "v1|237173788601|0":
            return {"title": " Lions Pride - Lot of 8 ", "condition": "Used",
                    "seller": "joepa4ever"}
        if item == "v1|307274713875|0":
            return None                                   # the listing has ended
        raise RuntimeError("boom")

    jobs = tr.parse_jobs(LOG) + [("job-x", "v1|9|0")]
    got = tr.collect(jobs, fetch)
    assert got == {
        "ed9f0edd-0e4c-4024-9508-789c40dc18b2": {
            "item_id": "v1|237173788601|0", "title": "Lions Pride - Lot of 8",
            "condition": "Used"},
        "11111111-relisted": {
            "item_id": "v1|237173788601|0", "title": "Lions Pride - Lot of 8",
            "condition": "Used"},
    }
    # one lookup per distinct item, and the seller is never written
    assert calls == ["v1|237173788601|0", "v1|307274713875|0", "v1|9|0"]
    assert all("seller" not in v for v in got.values())


def test_the_summary_says_what_came_back():
    assert "12 of 40" in tr.summary_text(40, 12)
    assert tr.JOBS_BLOB in tr.summary_text(0, 0)
    assert "failed" in tr.summary_text(5, 0, "Forbidden: 403")


def test_the_command_and_its_route_are_wired():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "main.py")).read()
    assert "if title_recovery.parse_command(raw):\n        _start_title_recovery(ack)" in src
    i = src.index('@flask_app.route("/internal/titles", methods=["POST"])')
    body = src[i:i + 1800]
    assert "if not _internal_request_ok(request):" in body        # the per-process secret
    assert "ebay_client.get_item(app_id, cert_id, item)" in body
    # lookups only: nothing is handed over or marked seen from here
    assert "_handoff_lot_to_buttonmatcher" not in body and "_mark_item_seen" not in body
