"""Button prices to the price_log sheet (2026-09-30).

One row per identified button per lot, carrying the lot's price per button
(asking / buttons detected), and a price_avg tab whose QUERY averages it per
year + slogan.  gspread is replaced by fakes; main.py imports the heavy stack,
so its wiring is read as source.

Run: python tests/run_price_log_tests.py
"""

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from ebayscout import price_log, scan_log


def _record(asking=24.0, detected=12, confirmed=(), **extra):
    rec = {"ts": "2026-09-30T14:00:00+00:00", "item_id": "v1|123|0",
           "title": "Penn State buttons lot", "listing_url": "https://ebay/itm/123",
           "asking": asking}
    rec.update(scan_log.button_price_fields(
        asking, detected, [{"year": y, "slogan": s} for y, s in confirmed]))
    rec.update(extra)
    return rec


def _col(row, name):
    return row[price_log.PRICE_HEADER.index(name)]


# --- rows ---------------------------------------------------------------------------

def test_one_row_per_named_button_with_the_lots_price_per_button():
    rows = price_log.price_rows(_record(24.0, 12, [
        (1985, "Beat Pitt"), (1985, "Beat Pitt"), (1986, "Whip Miami")], run_id="r7"))
    assert len(rows) == 2
    pitt = next(r for r in rows if _col(r, "slogan") == "Beat Pitt")
    assert _col(pitt, "price_per_button") == 2.0        # $24 / 12 detected
    assert _col(pitt, "n_in_lot") == 2                   # duplicates: one row, counted
    assert _col(pitt, "buttons_detected") == 12
    assert _col(pitt, "asking") == 24.0
    assert _col(pitt, "year") == 1985
    assert _col(pitt, "item_id") == "v1|123|0"
    assert _col(pitt, "run_id") == "r7"
    assert all(len(r) == len(price_log.PRICE_HEADER) for r in rows)


def test_no_rows_without_a_price_per_button():
    """A row with no price would only dilute the sheet's average."""
    assert price_log.price_rows(_record(None, 5, [(1985, "Beat Pitt")])) == []
    assert price_log.price_rows(_record(0.0, 5, [(1985, "Beat Pitt")])) == []
    assert price_log.price_rows(_record(10.0, 0, [(1985, "Beat Pitt")])) == []
    assert price_log.price_rows({"price_per_button": True,
                                 "buttons": [{"year": 1985, "slogan": "x"}]}) == []


def test_no_rows_when_no_button_was_named():
    assert price_log.price_rows(_record(10.0, 4, [])) == []


def test_old_records_without_price_fields_write_nothing():
    old = {"ts": "2026-09-01T00:00:00Z", "item_id": "x", "asking": 50.0,
           "top_matches": [{"year": 1985, "slogan": "Beat Pitt", "overall": 0.9}]}
    assert price_log.price_rows(old) == []


def test_year_is_written_as_a_number_either_way():
    """QUERY keeps a column's majority type and blanks the rest."""
    rows = price_log.price_rows(_record(10.0, 2, [("1985", "A"), (1986, "B")]))
    assert sorted(_col(r, "year") for r in rows) == [1985, 1986]


def test_missing_fields_are_blank_cells_not_none():
    rec = _record(10.0, 2, [(1985, "A")])
    del rec["title"]
    row = price_log.price_rows(rec)[0]
    assert _col(row, "title") == "" and _col(row, "run_id") == ""


# --- the average formula --------------------------------------------------------------

def test_the_average_is_taken_over_the_price_per_button_column():
    f = price_log.avg_formula()
    p = chr(ord("A") + price_log.PRICE_HEADER.index("price_per_button"))
    y = chr(ord("A") + price_log.PRICE_HEADER.index("year"))
    s = chr(ord("A") + price_log.PRICE_HEADER.index("slogan"))
    assert f.startswith(f"=QUERY({price_log.PRICE_TAB}!A:")
    assert f"avg({p})" in f and f"group by {y}, {s}" in f
    assert f"count({p}) 'lots'" in f


# --- the writer -----------------------------------------------------------------------

class _WS:
    def __init__(self, fail=()):
        self.fail = list(fail)
        self.calls = []

    def append_rows(self, rows, value_input_option=None):
        if self.fail:
            raise self.fail.pop(0)
        self.calls.append((rows, value_input_option))


def test_a_lot_is_one_raw_write():
    """RAW, so a listing title that starts with "=" is stored as text."""
    ws = _WS()
    assert price_log.PriceLogger(ws).log_lot(_record(9.0, 3, [(1985, "A"), (1986, "B")]))
    assert len(ws.calls) == 1
    rows, opt = ws.calls[0]
    assert len(rows) == 2 and opt == "RAW"


def test_nothing_to_write_makes_no_call():
    ws = _WS()
    assert not price_log.PriceLogger(ws).log_lot(_record(None, 3, [(1985, "A")]))
    assert ws.calls == []


def test_a_rate_limited_write_is_retried():
    slept = []
    ws = _WS(fail=[Exception("APIError: [429]: Quota exceeded")])
    assert price_log.PriceLogger(ws, sleep=slept.append).log_lot(_record(9.0, 3, [(1985, "A")]))
    assert len(ws.calls) == 1 and slept == [1.0]


def test_any_other_failure_is_reported_not_raised_or_retried():
    slept = []
    ws = _WS(fail=[ValueError("tab deleted")])
    assert not price_log.PriceLogger(ws, sleep=slept.append).log_lot(_record(9.0, 3, [(1985, "A")]))
    assert slept == [] and ws.calls == []


def test_a_disabled_logger_does_nothing():
    lg = price_log.PriceLogger(None)
    assert not lg.enabled and not lg.log_lot(_record(9.0, 3, [(1985, "A")]))


# --- opening the sheet ----------------------------------------------------------------

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


class _SS:
    title = "Logger"

    def __init__(self, tabs=(), fail_add=()):
        self.tabs = {t.title: t for t in tabs}
        self.fail_add = set(fail_add)

    def worksheet(self, title):
        if title not in self.tabs:
            raise LookupError(title)
        return self.tabs[title]

    def worksheets(self):
        return list(self.tabs.values())

    def add_worksheet(self, title, rows, cols):
        if title in self.fail_add:
            raise RuntimeError("no permission")
        self.tabs[title] = _Tab(title)
        return self.tabs[title]


class _Client:
    def __init__(self, ss=None, error=None):
        self.ss, self.error, self.keys = ss, error, []

    def open_by_key(self, key):
        self.keys.append(key)
        if self.error:
            raise self.error
        return self.ss


def test_first_open_creates_both_tabs_with_header_and_formula():
    ss = _SS()
    ws = price_log.open_price_sheet(_Client(ss), "abc123")
    assert ws is ss.tabs["price_log"]
    assert ws.values[0] == price_log.PRICE_HEADER
    avg = ss.tabs["price_avg"]
    assert avg.updates == [("A1", [[price_log.avg_formula()]], "USER_ENTERED")]


def test_an_existing_average_tab_is_left_alone():
    """Whatever the user did to price_avg survives every restart."""
    mine = _Tab("price_avg")
    ss = _SS([_Tab("price_log", price_log.PRICE_HEADER), mine])
    assert price_log.open_price_sheet(_Client(ss), "abc123") is ss.tabs["price_log"]
    assert mine.updates == []
    assert ss.tabs["price_log"].values == [price_log.PRICE_HEADER]   # no second header


def test_the_workbook_url_works_as_well_as_the_key():
    c = _Client(_SS())
    price_log.open_price_sheet(c, "https://docs.google.com/spreadsheets/d/abc123/edit#gid=0")
    assert c.keys == ["abc123"]


def test_an_unopenable_workbook_disables_price_logging_quietly():
    assert price_log.open_price_sheet(_Client(error=PermissionError("403")), "abc") is None
    assert price_log.open_price_sheet(_Client(_SS()), "") is None


def test_no_average_tab_still_leaves_the_log_working():
    ss = _SS(fail_add={"price_avg"})
    assert price_log.open_price_sheet(_Client(ss), "abc") is ss.tabs["price_log"]


# --- main is wired to it --------------------------------------------------------------

def _main_fn(name):
    src = open(os.path.join(os.path.dirname(HERE), "main.py")).read()
    f = next(n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(src, f) or ""


def test_startup_opens_the_price_tab_in_the_logging_workbook():
    body = _main_fn("startup")
    assert "global buy_rules, match_logger, price_logger" in body
    assert "price_log.open_price_sheet(gclient, logger_id)" in body
    assert "price_logger = price_log.PriceLogger(None)" in body   # fail-open


def test_every_pipeline_lot_writes_its_prices_from_its_scan_log_record():
    body = _main_fn("process_pipeline_lot")
    built = body.index("record = _scan_log_record(")
    wrote = body.index("price_logger.log_lot(record)")
    assert built < wrote < body.index("append_scan_log([record])")
    # after the run id is stamped, so the sheet row carries it too
    assert body.index('record["run_id"] = run_id') < wrote
