"""relists.py: a relisted lot is recognised, not run again (2026-11-08).

The cases are real relistings from #inventory-bot.  main.py's wiring is read
via ast (main.py imports the heavy stack).  Run: python -m pytest tests/test_relists.py
"""

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

from ebayscout import price_log as pl     # noqa: E402
from ebayscout import relists as rl       # noqa: E402

AKRON_1017 = ("https://www.ebay.com/itm/880024314871?_skw=Mellon+Bank+button"
              "&hash=itemcce58963f7:g:qDUAAOSwUUxjW-od")
CINCY_1023 = ("https://www.ebay.com/itm/860074104503?_skw=Penn+State+pinback"
              "&hash=itemc8406956b7:g:tGMAAeSwiCxojdKN")


def _price_log(rows):
    """price_log as relists.ranges() reads it: one column each, header
    first, trailing blanks trimmed."""
    cols = []
    for name in rl.COLUMNS:
        col = [[name]] + [[r.get(name, "")] if r.get(name, "") != "" else [] for r in rows]
        while col and not col[-1]:
            col.pop()
        cols.append(col)
    return cols


def _confirmed(**kw):
    row = {"kind": "listing", "source": "scan", "year": "1999",
           "slogan": "Tire Out Akron", "superseded": "no"}
    row.update(kw)
    return row


def _idx():
    return rl.index(_price_log([
        _confirmed(ebay_id="880024314871", listing_url=AKRON_1017,
                   title="Vintage 1999 Penn State Football Pinback Button Tire Out Akron Mellon Bank Promo"),
        _confirmed(ebay_id="860074104503", listing_url=CINCY_1023, year="1981",
                   slogan="Cincinnati, Ho Ho", seller="westmidwest1",
                   title="Central Counties Bank Cincinnati Ho Ho Penn State Pin Back Button P43"),
        # handed over and never finished: nothing to reuse
        {"kind": "listing", "source": "handoff", "ebay_id": "128115146664",
         "seller": "kling24toys", "superseded": "no",
         "title": "2011 PENN STATE CITIZENS BANK FOUR WINTER SPORTS SLOGAN BUTTONS/PINS"},
        # corrected later: the superseded rows are not a confirmation
        _confirmed(ebay_id="111111111111", superseded="yes", seller="s1", title="Old Lot"),
    ]))


def test_photo_id_from_a_picture_or_a_listing_url():
    assert rl.photo_id("https://i.ebayimg.com/images/g/qDUAAOSwUUxjW-od/s-l1600.jpg") == "qDUAAOSwUUxjW-od"
    assert rl.photo_id(AKRON_1017) == "qDUAAOSwUUxjW-od"
    assert rl.photo_id("https://www.ebay.com/itm/880024314871", None, "") == ""


def test_the_stock_code_is_not_part_of_the_title():
    a = rl.title_key("Central Counties Bank Cincinnati Ho Ho Penn State Pin Back Button P43")
    assert a == rl.title_key("Central Counties Bank Cincinnati Ho Ho Penn State Pin Back Button P117")
    assert a == rl.title_key("central counties bank cincinnati ho-ho penn state pin back button")
    assert rl.title_key("1975 Penn State Button") != rl.title_key("1976 Penn State Button")


def test_the_same_photo_relisted_is_the_same_lot():
    """rmn-vintage relisted Tire Out Akron on 11-06 with the 10-17 photo."""
    listing = {"item_id": "v1|840099329089|0", "seller": "rmn-vintage",
               "title": "Vintage 1999 Penn State Football Pinback Button Tire Out Akron Mellon Bank Promo",
               "listing_url": "https://www.ebay.com/itm/840099329089?hash=itemc399d27441:g:qDUAAOSwUUxjW-od"}
    assert rl.match(listing, _idx()) == "880024314871"


def test_the_same_seller_and_title_with_a_new_photo_is_the_same_lot():
    """westmidwest1 relists with a new photo and stock code each week."""
    listing = {"item_id": "v1|860120525381|0", "seller": "westmidwest1",
               "title": "Central Counties Bank Cincinnati Ho Ho Penn State Pin Back Button P117",
               "gallery_url": "https://i.ebayimg.com/images/g/ZnoAAeSwebJpGgSx/s-l225.jpg"}
    assert rl.match(listing, _idx()) == "860074104503"


def test_another_seller_with_the_same_title_is_not_a_relist():
    listing = {"item_id": "v1|1|0", "seller": "someone-else",
               "title": "Central Counties Bank Cincinnati Ho Ho Penn State Pin Back Button P43"}
    assert rl.match(listing, _idx()) == ""


def test_an_unfinished_or_corrected_lot_is_handed_over_again():
    assert rl.match({"item_id": "v1|2|0", "seller": "kling24toys",
                     "title": "2011 PENN STATE CITIZENS BANK FOUR WINTER SPORTS SLOGAN BUTTONS/PINS"},
                    _idx()) == ""
    assert rl.match({"item_id": "v1|3|0", "seller": "s1", "title": "Old Lot"}, _idx()) == ""


def test_a_lot_is_not_its_own_relist():
    assert rl.match({"item_id": "v1|880024314871|0", "listing_url": AKRON_1017}, _idx()) == ""


def test_a_relist_is_one_row_pointing_at_the_confirmed_lot():
    listing = {"item_id": "v1|860120525381|0", "seller": "westmidwest1",
               "title": "Cincinnati Ho Ho P117", "current_price": 11.0,
               "shipping_cost": 6.75, "buying_options": ["FIXED_PRICE"],
               "listing_url": "https://www.ebay.com/itm/860120525381"}
    rec = rl.relist_record(listing, "860074104503")
    row = pl.handoff_row(rec)
    h = pl.PRICE_HEADER
    assert row[h.index("source")] == "relist" and row[h.index("run_id")] == "relist-of-860074104503"
    assert row[h.index("ebay_id")] == "860120525381" and row[h.index("lot_price")] == 11.0
    assert row[h.index("year")] == "" and row[h.index("price_per_button")] == ""  # counted once
    line = rl.slack_lines([rec])
    assert "westmidwest1" in line and "$11.00" in line and "itm/860074104503" in line
    assert rl.slack_lines([]) == ""


def test_the_ranges_are_the_columns_index_reads():
    assert rl.ranges()[rl.COLUMNS.index("seller")] == "U:U"


_MAIN = open(os.path.join(PKG, "main.py")).read()


def _fn(name):
    tree = ast.parse(_MAIN)
    f = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_MAIN, f) or ""


def test_the_feed_checks_for_a_relist_before_handing_a_lot_over():
    crawl = _fn("_run_crawl")
    assert crawl.index("relists.match(") < crawl.index("_handoff_lot_to_buttonmatcher(")
    assert "_mark_item_seen_now(item_id)" in crawl and "relisted.append(rec)" in crawl
    assert "EBAYSCOUT_SKIP_RELISTS" in _fn("_relist_index")
