"""price_log — button prices, listed and sold, in the logging workbook.

SHARED, byte-identical in ebayscout/ebayscout/tests/ and buttonmatcher/tests/,
like the module it tests.  It finds the module whichever layout it is in.
gspread is replaced by fakes; each service's wiring is tested in its own repo.

Run: python tests/run_price_log_tests.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(os.path.dirname(HERE), "price_log.py")):
    sys.path.insert(0, os.path.dirname(HERE))                    # buttonmatcher
    import price_log as pl
else:
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))   # ebayscout
    from ebayscout import price_log as pl


def _col(row, name):
    return row[pl.PRICE_HEADER.index(name)]


# --- the shared key ----------------------------------------------------------------

def test_the_item_number_is_found_in_everything_either_side_holds():
    n = "256123456789"
    for v in (n, f" {n} ", f"v1|{n}|0",
              f"https://www.ebay.com/itm/{n}",
              f"https://www.ebay.com/itm/{n}?hash=item3b9",
              f"https://www.ebay.com/itm/Vintage-1972-Penn-State/{n}",
              f"https://www.ebay.com/itm/{n}/",
              f"https://cgi.ebay.com/ws/eBayISAPI.dll?ViewItem&item={n}"):
        assert pl.ebay_item_number(v) == n, v


def test_no_number_is_no_number():
    for v in (None, "", "abc", "12345", "https://ebay.io/m/hsOh9Q",
              "https://www.ebay.com/sch/i.html?_nkw=penn+state"):
        assert pl.ebay_item_number(v) == "", v


def test_listing_format_from_ebays_buying_options():
    assert pl.listing_format(["AUCTION"]) == "auction"
    assert pl.listing_format(["AUCTION", "FIXED_PRICE"]) == "auction"
    assert pl.listing_format(["FIXED_PRICE", "BEST_OFFER"]) == "buy_it_now"
    assert pl.listing_format([]) == "other"
    assert pl.listing_format(None) == "other"


# --- pricing a sold lot --------------------------------------------------------------

_1972 = {(1972, "Pulverize the Panthers"): 3, (1972, "Wallop the Wolfpack"): 2,
         (1972, "Trip the Terrapins"): 2, (1972, "Crush the Orange"): 1,
         (1972, "Hammer the Hawkeyes"): 1, (1972, "Get the Goat"): 1,
         (1972, "I'd Sooner Be a Nittany Lion"): 1}


def test_even_split_is_the_sale_over_every_button():
    prices = pl.split_sale(250.0, 11, _1972)
    assert set(prices.values()) == {22.73}


def test_the_operators_prices_stand_and_the_rest_share_what_is_left():
    prices = pl.split_sale(250.0, 11, _1972,
                           {(1972, "I'd Sooner Be a Nittany Lion"): 120.0})
    assert prices[(1972, "I'd Sooner Be a Nittany Lion")] == 120.0
    assert prices[(1972, "Pulverize the Panthers")] == 13.0      # $130 / 10
    assert round(sum(prices[k] * n for k, n in _1972.items()), 2) == 250.0


def test_an_override_prices_every_copy_of_that_button():
    prices = pl.split_sale(100.0, 4, {(1985, "A"): 3, (1985, "B"): 1}, {(1985, "A"): 20.0})
    assert prices == {(1985, "A"): 20.0, (1985, "B"): 40.0}


def test_unidentified_buttons_share_the_remainder_without_a_price_of_their_own():
    prices = pl.split_sale(30.0, 3, {(1990, "A"): 1}, {})
    assert prices == {(1990, "A"): 10.0}


def test_prices_above_the_sale_are_refused():
    try:
        pl.split_sale(39.0, 12, {(1975, "A"): 2}, {(1975, "A"): 25.0})
    except ValueError as e:
        assert "$50.00" in str(e) and "$39.00" in str(e)
    else:
        raise AssertionError("should refuse")


def test_every_button_priced_must_add_up_to_the_sale():
    counts = {(1975, "A"): 1, (1975, "B"): 1}
    assert pl.split_sale(40.0, 2, counts, {(1975, "A"): 30.0, (1975, "B"): 10.0}) == {
        (1975, "A"): 30.0, (1975, "B"): 10.0}
    try:
        pl.split_sale(40.0, 2, counts, {(1975, "A"): 30.0, (1975, "B"): 5.0})
    except ValueError as e:
        assert "$35.00" in str(e)
    else:
        raise AssertionError("should refuse")


def test_nonsense_sales_are_refused():
    one = {(1, "A"): 1}
    for total, n, counts, over in ((0, 3, one, {}), (10, 0, {}, {}),
                                   (10, 2, one, {(1, "A"): -1})):
        try:
            pl.split_sale(total, n, counts, over)
        except ValueError:
            continue
        raise AssertionError((total, n, over))


def test_a_lot_has_at_least_as_many_buttons_as_were_named():
    assert pl.split_sale(10.0, 0, {(1, "A"): 2}) == {(1, "A"): 5.0}


def test_counts_come_from_the_scan_logs_shape_or_one_entry_per_button():
    assert pl.counts_from_buttons([{"year": 1985, "slogan": "A", "n": 2},
                                   {"year": "1986", "slogan": "B"},
                                   {"year": 1986, "slogan": "B"},
                                   {"year": None, "slogan": "C"}, {"year": 1990, "slogan": ""}]
                                  ) == {(1985, "A"): 2, (1986, "B"): 2}


# --- rows -----------------------------------------------------------------------------

def _scan_record(**kw):
    rec = {"ts": "2026-09-30T14:00:00+00:00", "item_id": "v1|256123456789|0",
           "title": "=HYPERLINK(1)", "listing_url": "https://ebay/itm/256123456789",
           "asking": 24.0, "buttons_detected": 12, "price_per_button": 2.0,
           "buttons": [{"year": 1985, "slogan": "Beat Pitt", "n": 2},
                       {"year": 1986, "slogan": "Whip Miami", "n": 1}],
           "buying_options": ["FIXED_PRICE"], "bid_count": None, "run_id": "r7"}
    rec.update(kw)
    return rec


def test_a_scanned_lot_is_one_listing_row_per_named_button():
    rows = pl.listing_rows(_scan_record())
    assert len(rows) == 2 and all(len(r) == len(pl.PRICE_HEADER) for r in rows)
    pitt = next(r for r in rows if _col(r, "slogan") == "Beat Pitt")
    assert _col(pitt, "kind") == "listing" and _col(pitt, "source") == "scan"
    assert _col(pitt, "ebay_id") == "256123456789"
    assert _col(pitt, "n_in_lot") == 2 and _col(pitt, "buttons_detected") == 12
    assert _col(pitt, "lot_price") == 24.0 and _col(pitt, "price_per_button") == 2.0
    assert _col(pitt, "sale_format") == "buy_it_now" and _col(pitt, "price_basis") == "asking"
    assert _col(pitt, "superseded") == "no" and _col(pitt, "run_id") == "r7"
    assert _col(pitt, "year") == 1985 and _col(pitt, "bids") == ""


def test_an_auction_listing_is_marked_so_the_average_can_leave_it_out():
    rows = pl.listing_rows(_scan_record(buying_options=["AUCTION"], bid_count=3))
    assert {_col(r, "sale_format") for r in rows} == {"auction"}
    assert {_col(r, "bids") for r in rows} == {3}


def test_no_listing_rows_without_a_price_or_a_named_button():
    assert pl.listing_rows(_scan_record(price_per_button=None)) == []
    assert pl.listing_rows(_scan_record(price_per_button=0)) == []
    assert pl.listing_rows(_scan_record(price_per_button=True)) == []
    assert pl.listing_rows(_scan_record(buttons=[])) == []
    assert pl.listing_rows({"asking": 5.0, "top_matches": []}) == []


def test_sold_rows_carry_each_buttons_own_price():
    counts = {(1972, "Get the Goat"): 1, (1972, "Trip the Terrapins"): 2}
    rows = pl.lot_rows(kind="sold", source="scout_sold", ebay_id="256123456789",
                       counts=counts, prices={(1972, "Get the Goat"): 50.0,
                                              (1972, "Trip the Terrapins"): 10.0},
                       buttons_in_lot=3, lot_price=70.0, shipping=7.0,
                       allocation="custom", sale_format="auction", bids=1,
                       sale_date="2026-09-22", price_basis="entered")
    goat = next(r for r in rows if _col(r, "slogan") == "Get the Goat")
    assert _col(goat, "price_per_button") == 50.0 and _col(goat, "shipping") == 7.0
    assert _col(goat, "allocation") == "custom" and _col(goat, "sale_date") == "2026-09-22"
    assert _col(goat, "ts")                      # stamped when not given


# --- one sale counts once -----------------------------------------------------------

class _Sheet:
    """A price_log worksheet: rows in memory, API-shaped reads (trimmed)."""

    def __init__(self, rows=(), fail=()):
        self.rows = [list(pl.PRICE_HEADER)] + [list(r) for r in rows]
        self.fail = list(fail)       # exceptions for the next calls, in order
        self.calls = []

    def _maybe_fail(self):
        if self.fail:
            e = self.fail.pop(0)
            if e is not None:
                raise e

    def append_rows(self, rows, value_input_option=None):
        self._maybe_fail()
        self.calls.append(("append", value_input_option))
        self.rows.extend(list(r) for r in rows)

    def _cols(self, a, b):
        lo, hi = ord(a) - ord("A"), ord(b) - ord("A")
        out = []
        for r in self.rows:
            cells = [("" if v is None else str(v)) for v in r[lo:hi + 1]]
            while cells and cells[-1] == "":
                cells.pop()
            out.append(cells)
        while out and not out[-1]:
            out.pop()
        return out

    def batch_get(self, ranges):
        self._maybe_fail()
        self.calls.append(("get", tuple(ranges)))
        return [self._cols(*r.split(":")) for r in ranges]

    def batch_update(self, data, value_input_option=None):
        self._maybe_fail()
        self.calls.append(("update", value_input_option))
        for d in data:
            letter, row = d["range"][0], int(d["range"][1:])
            self.rows[row - 1][ord(letter) - ord("A")] = d["values"][0][0]

    def sold(self, source=None):
        k, s, q = (pl.PRICE_HEADER.index(n) for n in ("kind", "source", "superseded"))
        return [r for r in self.rows[1:] if r[k] == "sold" and (source is None or r[s] == source)]


def _sale(source, ebay_id="256123456789", price=39.0):
    return pl.lot_rows(kind="sold", source=source, ebay_id=ebay_id,
                       counts={(1975, "Temple Hoo?"): 1}, prices={(1975, "Temple Hoo?"): price},
                       buttons_in_lot=1, lot_price=price)


def _sup(row):
    return _col(row, "superseded")


def test_the_operators_entry_replaces_the_trackers_row_for_the_same_item():
    ws = _Sheet(_sale("auction"))
    res = pl.PriceLogger(ws).log_sale(_sale("scout_sold", price=45.0), "256123456789",
                                      source="scout_sold")
    assert res == {"status": "written", "replaced": 1}
    assert [_sup(r) for r in ws.sold("auction")] == ["yes"]
    assert [_sup(r) for r in ws.sold("scout_sold")] == ["no"]


def test_the_tracker_arriving_second_writes_its_row_already_superseded():
    ws = _Sheet(_sale("scout_sold"))
    res = pl.PriceLogger(ws).log_sale(_sale("auction"), "256123456789", source="auction")
    assert res["status"] == "superseded"
    assert [_sup(r) for r in ws.sold("auction")] == ["yes"]
    assert [_sup(r) for r in ws.sold("scout_sold")] == ["no"]


def test_the_tracker_never_writes_an_item_twice():
    ws = _Sheet(_sale("auction"))
    res = pl.PriceLogger(ws).log_sale(_sale("auction"), "256123456789", source="auction")
    assert res["status"] == "duplicate" and len(ws.sold()) == 1


def test_logging_a_sale_again_replaces_the_earlier_entry():
    ws = _Sheet(_sale("scout_sold"))
    pl.PriceLogger(ws).log_sale(_sale("scout_sold", price=41.0), "256123456789",
                                source="scout_sold")
    assert [_sup(r) for r in ws.sold()] == ["yes", "no"]


def test_other_items_and_listing_rows_are_left_alone():
    listing = pl.lot_rows(kind="listing", source="scan", ebay_id="256123456789",
                          counts={(1975, "A"): 1}, prices={(1975, "A"): 3.0},
                          buttons_in_lot=1, lot_price=3.0)
    ws = _Sheet(listing + _sale("auction", ebay_id="111111111111"))
    res = pl.PriceLogger(ws).log_sale(_sale("scout_sold"), "256123456789", source="scout_sold")
    assert res == {"status": "written", "replaced": 0}
    assert all(_sup(r) == "no" for r in ws.rows[1:])


def test_a_sale_with_no_item_number_is_written_without_a_lookup():
    ws = _Sheet()
    res = pl.PriceLogger(ws).log_sale(_sale("scout_sold", ebay_id=""), "", source="scout_sold")
    assert res["status"] == "written"
    assert [c[0] for c in ws.calls] == ["append"]


def test_a_failed_write_supersedes_nothing():
    ws = _Sheet(_sale("auction"), fail=[None, ValueError("tab deleted")])
    res = pl.PriceLogger(ws).log_sale(_sale("scout_sold"), "256123456789", source="scout_sold")
    assert res["status"] == "failed"
    assert [_sup(r) for r in ws.sold()] == ["no"]


def test_a_failed_lookup_still_records_the_sale():
    ws = _Sheet(fail=[ValueError("read failed")])
    res = pl.PriceLogger(ws).log_sale(_sale("auction"), "256123456789", source="auction")
    assert res["status"] == "written" and len(ws.sold()) == 1


def test_live_sales_reads_the_trimmed_columns_the_api_returns():
    bd = [["kind", "source", "ebay_id"], ["sold", "auction", "9"], ["sold", "auction", "256123456789"],
          [], ["listing", "scan", "256123456789"], ["sold", "scout_sold", "256123456789"]]
    q = [["superseded"], ["no"], ["yes"]]
    assert pl.live_sales(bd, q, "256123456789") == [(6, "scout_sold")]
    assert pl.live_sales(bd, q, "") == []


# --- writing ----------------------------------------------------------------------------

def test_a_scanned_lot_is_one_raw_write():
    """RAW, so a listing title that starts with "=" is stored as text."""
    ws = _Sheet()
    assert pl.PriceLogger(ws).log_listing(_scan_record())
    assert ws.calls == [("append", "RAW")] and len(ws.rows) == 3


def test_nothing_to_write_makes_no_call():
    ws = _Sheet()
    assert not pl.PriceLogger(ws).log_listing(_scan_record(price_per_button=None))
    assert pl.PriceLogger(ws).log_sale([], "1", source="auction")["status"] == "empty"
    assert ws.calls == []


def test_a_rate_limited_write_is_retried():
    slept = []
    ws = _Sheet(fail=[Exception("APIError: [429]: Quota exceeded")])
    assert pl.PriceLogger(ws, sleep=slept.append).log_listing(_scan_record())
    assert slept == [1.0] and len(ws.rows) == 3


def test_any_other_failure_is_reported_not_raised_or_retried():
    slept = []
    ws = _Sheet(fail=[ValueError("tab deleted")])
    assert not pl.PriceLogger(ws, sleep=slept.append).log_listing(_scan_record())
    assert slept == [] and len(ws.rows) == 1


def test_a_disabled_logger_does_nothing():
    lg = pl.PriceLogger(None)
    assert not lg.enabled and not lg.log_listing(_scan_record())
    assert lg.log_sale(_sale("auction"), "1", source="auction")["status"] == "failed"


# --- the averages --------------------------------------------------------------------

def _letter(name):
    return chr(ord("A") + pl.PRICE_HEADER.index(name))


def test_the_average_is_price_per_button_listing_beside_sold():
    f = pl.avg_formula()
    p, k = _letter("price_per_button"), _letter("kind")
    assert f.startswith(f"=QUERY({pl.PRICE_TAB}!A:{_letter('run_id')},")
    assert f"avg({p})" in f and f"count({p})" in f and f"pivot {k}" in f
    assert f"group by {_letter('year')}, {_letter('slogan')}" in f
    assert f"{_letter('superseded')} = 'no'" in f
    # an auction's price at scan time is a bid, not an asking price
    assert f"not ({k} = 'listing' and {_letter('sale_format')} = 'auction')" in f


def test_the_by_item_tab_joins_on_the_item_number():
    f = pl.item_formula()
    assert f"select {_letter('ebay_id')}, max({_letter('lot_price')})" in f
    assert f"pivot {_letter('kind')}" in f and f"{_letter('superseded')} = 'no'" in f


# --- opening the workbook ---------------------------------------------------------

class _Tab:
    def __init__(self, title, header=None):
        self.title = title
        self.values = [header] if header else []
        self.updates = []

    def row_values(self, i):
        return self.values[i - 1] if len(self.values) >= i else []

    def append_row(self, row, value_input_option=None):
        self.values.append(row)

    def update(self, values=None, range_name=None, value_input_option=None):
        self.updates.append((range_name, values, value_input_option))


class _Book:
    title = "Logger"

    def __init__(self, tabs=(), fail_add=()):
        self.tabs = {t.title: t for t in tabs}
        self.fail_add = set(fail_add)

    def worksheets(self):
        return list(self.tabs.values())

    def add_worksheet(self, title, rows, cols):
        if title in self.fail_add:
            raise RuntimeError("no permission")
        self.tabs[title] = _Tab(title)
        return self.tabs[title]


class _Client:
    def __init__(self, book=None, error=None):
        self.book, self.error, self.keys = book, error, []

    def open_by_key(self, key):
        self.keys.append(key)
        if self.error:
            raise self.error
        return self.book


def test_first_open_creates_the_log_and_both_formula_tabs():
    book = _Book()
    ws = pl.open_price_sheet(_Client(book), "abc123")
    assert ws is book.tabs["price_log"] and ws.values[0] == pl.PRICE_HEADER
    assert book.tabs["price_avg"].updates == [("A1", [[pl.avg_formula()]], "USER_ENTERED")]
    assert book.tabs["price_by_item"].updates == [("A1", [[pl.item_formula()]], "USER_ENTERED")]


def test_existing_formula_tabs_are_left_alone():
    """Whatever the operator did to them survives every restart."""
    avg, item = _Tab("price_avg"), _Tab("price_by_item")
    book = _Book([_Tab("price_log", list(pl.PRICE_HEADER)), avg, item])
    assert pl.open_price_sheet(_Client(book), "abc123") is book.tabs["price_log"]
    assert avg.updates == [] and item.updates == []
    assert book.tabs["price_log"].values == [pl.PRICE_HEADER]   # no second header


def test_a_log_with_another_header_is_not_written_under_the_wrong_columns():
    book = _Book([_Tab("price_log", ["ts", "item_id", "year"])])
    assert pl.open_price_sheet(_Client(book), "abc123") is None


def test_the_workbook_url_works_as_well_as_the_key():
    c = _Client(_Book())
    pl.open_price_sheet(c, "https://docs.google.com/spreadsheets/d/abc123/edit#gid=0")
    assert c.keys == ["abc123"]


def test_an_unopenable_workbook_disables_price_logging_quietly():
    assert pl.open_price_sheet(_Client(error=PermissionError("403")), "abc") is None
    assert pl.open_price_sheet(_Client(_Book()), "") is None


def test_a_formula_tab_that_cannot_be_made_leaves_the_log_working():
    book = _Book(fail_add={"price_avg"})
    assert pl.open_price_sheet(_Client(book), "abc") is book.tabs["price_log"]
    assert "price_by_item" in book.tabs
