"""ebayscout/ticket_photos.py: `/crawl tickets`, the first photo of each
listing a ticket search finds, saved for training."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ebayscout import ticket_photos as tp  # noqa: E402


def _item(number="123", image="https://i.ebayimg.com/images/g/abc/s-l225.jpg", **kw):
    item = {"itemId": f"v1|{number}|0", "title": "1980 Penn State ticket stub",
            "price": {"value": "12.50"}, "itemWebUrl": f"https://www.ebay.com/itm/{number}"}
    if image:
        item["image"] = {"imageUrl": image}
    item.update(kw)
    return item


def test_only_the_tickets_form_is_this_command():
    assert tp.parse_command("tickets") and tp.parse_command("  Tickets ")
    for other in ("800", "titles", "tickets now", "seller tickets", "", None):
        assert not tp.parse_command(other), other


def test_item_number_is_digits_or_nothing():
    assert tp.item_number("v1|237173788601|0") == "237173788601"
    assert tp.item_number("237173788601") == "237173788601"
    for bad in ("", None, "v1|../x|0", "abc"):
        assert tp.item_number(bad) == ""


def test_full_size_rewrites_only_the_size():
    assert tp.full_size("https://i.ebayimg.com/images/g/abc/s-l225.jpg") == \
        "https://i.ebayimg.com/images/g/abc/s-l1600.jpg"
    assert tp.full_size("https://i.ebayimg.com/images/g/abc/s-l500.webp?x=1") == \
        "https://i.ebayimg.com/images/g/abc/s-l1600.webp?x=1"
    assert tp.full_size("https://example.com/photo.jpg") == "https://example.com/photo.jpg"
    assert tp.full_size(None) == ""


def test_record_needs_a_number_and_a_photo():
    number, rec = tp.record(_item(), "2026-11-14T00:00:00+00:00")
    assert number == "123"
    assert rec["photo"] == "ebay_scout/tickets/photos/123.jpg"
    assert rec["image_url"].endswith("/s-l1600.jpg") and rec["thumb_url"].endswith("/s-l225.jpg")
    assert rec["price"] == 12.5 and rec["title"].startswith("1980")
    assert tp.record(_item(image=""), "t") is None
    assert tp.record({"itemId": "junk", "image": {"imageUrl": "u"}}, "t") is None
    thumb = _item(image="", thumbnailImages=[{"imageUrl": "https://i.ebayimg.com/t/s-l96.jpg"}])
    assert tp.record(thumb, "t")[1]["thumb_url"].endswith("s-l96.jpg")
    assert tp.record(_item(price={}), "t")[1]["price"] is None


def test_collect_keeps_each_listing_once_up_to_the_limit():
    items = [_item("1"), _item("2"), _item("1"), _item("3")]
    assert list(tp.collect(items, "t")) == ["1", "2", "3"]
    assert list(tp.collect(items, "t", limit=2)) == ["1", "2"]
    assert tp.collect(None, "t") == {}


def test_save_photos_skips_kept_and_falls_back_to_the_thumbnail():
    def item(n):
        return _item(n, image=f"https://i.ebayimg.com/images/g/p{n}/s-l225.jpg")

    recs = tp.collect([item("1"), item("2"), item("3"), item("4")], "t")
    stored = {}

    def fetch(url):
        if "/p2/s-l1600" in url:
            raise IOError("404")          # item 2's full size is missing
        if "/p3/" in url:
            return b""                    # item 3: nothing at either size
        return b"jpg:" + url.encode()

    def store(blob, data):
        stored[blob] = data

    saved, kept, failed = tp.save_photos(
        recs, lambda blob: blob.endswith("/1.jpg"), fetch, store)
    assert kept == ["1"] and saved == ["2", "4"] and failed == ["3"]
    assert stored[tp.photo_blob("2")].endswith(b"s-l225.jpg")
    assert recs["2"]["image_url"].endswith("s-l225.jpg")
    assert stored[tp.photo_blob("4")].endswith(b"s-l1600.jpg")
    assert tp.photo_blob("1") not in stored


def test_save_photos_counts_a_failed_store():
    recs = tp.collect([_item("1")], "t")

    def store(blob, data):
        raise IOError("bucket")

    assert tp.save_photos(recs, lambda b: False, lambda u: b"x", store) == ([], [], ["1"])


def test_merge_index_adds_keeps_and_drops_failures():
    old = json.dumps({"1": {"title": "old", "pulled_at": "first"}, "9": {"title": "ended"}})
    recs = tp.collect([_item("1"), _item("2"), _item("3")], "second")
    index = json.loads(tp.merge_index(old, recs, failed=["3"]))
    assert sorted(index) == ["1", "2", "9"]
    assert index["1"]["pulled_at"] == "first" and index["1"]["title"].startswith("1980")
    assert index["2"]["pulled_at"] == "second" and "thumb_url" not in index["2"]
    assert index["9"] == {"title": "ended"}
    assert sorted(json.loads(tp.merge_index("not json", recs, []))) == ["1", "2", "3"]
    assert sorted(json.loads(tp.merge_index("[1]", recs, []))) == ["1", "2", "3"]


def test_price_row_is_a_price_log_listing_row():
    from ebayscout import price_log as pl
    item = _item("123", buyingOptions=["FIXED_PRICE"], bidCount=None,
                 seller={"username": "SomeSeller"}, condition="Used",
                 shippingOptions=[{"shippingCost": {"value": "4.50"}}])
    row = tp.price_row(item, "2026-11-14T00:00:00+00:00")
    assert len(row) == len(pl.PRICE_HEADER)
    got = dict(zip(pl.PRICE_HEADER, row))
    assert got["kind"] == pl.KIND_LISTING and got["source"] == "ticket"
    assert got["ebay_id"] == "123" and got["lot_price"] == 12.5 and got["shipping"] == 4.5
    assert got["year"] == "" and got["slogan"] == "" and got["price_per_button"] == ""
    assert got["sale_format"] == pl.FORMAT_BUY_IT_NOW and got["superseded"] == "no"
    assert got["price_basis"] == pl.BASIS_ASKING
    assert got["seller"] == "someseller" and got["condition"] == "Used"
    assert got["run_id"] == "tickets-2026-11-14T00:00:00+00:00"
    assert got["title"].startswith("1980") and got["listing_url"].endswith("/123")
    calc = _item("5", shippingOptions=[{"shippingCostType": "CALCULATED"}], bidCount=3)
    got = dict(zip(pl.PRICE_HEADER, tp.price_row(calc, "t")))
    assert got["shipping"] == "" and got["bids"] == 3
    assert tp.price_row({"itemId": "junk"}, "t") is None


def test_price_rows_need_no_photo_and_count_each_listing_once():
    rows = tp.price_rows([_item("1"), _item("2", image=""), _item("1"), {"itemId": "x"}], "t")
    assert [r[3] for r in rows] == ["1", "2"]
    assert len(tp.price_rows([_item("1"), _item("2")], "t", limit=1)) == 1
    assert tp.price_rows(None, "t") == []


def test_summary_text():
    assert "failed" in tp.summary_text(0, 0, 0, 0, 0, error="boom")
    assert "no listings" in tp.summary_text(0, 0, 0, 0, 0)
    text = tp.summary_text(419, 400, 15, 4, 415, 419)
    assert "419 listings" in text and "400 photos" in text and "4 could not" in text
    assert "419 price rows" in text and "source `ticket`" in text
    assert "could not" not in tp.summary_text(10, 10, 0, 0, 10, 10)
    bad = tp.summary_text(10, 10, 0, 0, 10, 0, price_error="quota")
    assert "not written" in bad and "quota" in bad and "price rows added" not in bad
