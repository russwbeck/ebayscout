"""
ebayscout/price_log.py

Button prices to a Google Sheet, so a button's average price can be read there.

Two tabs in the logging workbook (LOGGER_ID), beside match_log / confirm_log:

  price_log  One row per identified button per lot: the lot's asking price, how
             many buttons were detected in it, and the price per button
             (asking / buttons detected) credited to that button.  A lot holding
             three of one button gives that button ONE row with n_in_lot = 3, so
             a lot is one observation of its price whatever its duplicates.
  price_avg  One QUERY formula over price_log: per year + slogan, the number of
             lots and the average / min / max price per button.  Written once,
             when the tab is created; the sheet keeps it current, and edits to it
             are never overwritten.

The rows come from the lot's scan_log record (scan_log.button_price_fields), so
the sheet and tools/market_report.py read the same numbers.

ebayscout-only: match_logging.py is byte-shared with buttonmatcher, which sees no
asking prices.  Pure apart from the injected worksheet, as match_logging is.
"""

from __future__ import annotations

import threading
import time
import traceback

from . import match_logging as mlog
from . import sheet_retry

PRICE_TAB = "price_log"
AVG_TAB = "price_avg"

PRICE_HEADER = [
    "ts", "item_id", "year", "slogan", "n_in_lot", "buttons_detected",
    "asking", "price_per_button", "title", "listing_url", "run_id",
]


def _col(name: str) -> str:
    """The sheet column letter of a price_log field (A–Z; the header is short)."""
    return chr(ord("A") + PRICE_HEADER.index(name))


def avg_formula() -> str:
    """price_avg's one cell: average price per button, per year + slogan.

    Built from PRICE_HEADER so a reordered column cannot silently average the
    wrong one.
    """
    y, s, p = _col("year"), _col("slogan"), _col("price_per_button")
    last = _col(PRICE_HEADER[-1])
    return (f"=QUERY({PRICE_TAB}!A:{last}, "
            f"\"select {y}, {s}, count({p}), avg({p}), min({p}), max({p}) "
            f"where {p} is not null group by {y}, {s} order by {y}, {s} "
            f"label count({p}) 'lots', avg({p}) 'avg $/button', "
            f"min({p}) 'min $/button', max({p}) 'max $/button'\", 1)")


def _year(y):
    """A year as a number when it is one.  QUERY treats a column of mixed types
    as its majority type and blanks the rest, so "1985" and 1985 must not mix."""
    s = str(y).strip()
    return int(s) if s.isdigit() else s


def _cell(v):
    return "" if v is None else v


def price_rows(record: dict) -> list[list]:
    """The price_log rows for one lot's scan_log record: one per named button.

    No rows when the lot has no price per button (no asking price, or nothing
    detected): a row without a price would only dilute the average.
    """
    per = record.get("price_per_button")
    if isinstance(per, bool) or not isinstance(per, (int, float)) or per <= 0:
        return []
    rows = []
    for b in record.get("buttons") or []:
        if b.get("year") is None or not b.get("slogan"):
            continue
        rows.append([
            _cell(record.get("ts")), _cell(record.get("item_id")),
            _year(b["year"]), b["slogan"], _cell(b.get("n")),
            _cell(record.get("buttons_detected")), _cell(record.get("asking")),
            per, _cell(record.get("title")), _cell(record.get("listing_url")),
            _cell(record.get("run_id")),
        ])
    return rows


class PriceLogger:
    """Appends a lot's price rows to the injected price_log worksheet.

    ``ws`` needs only ``append_rows``; None disables it.  One write per lot,
    retried on the Sheets per-minute quota (sheet_retry) and never raised: a
    price row is a record of the scan, not a reason to fail it.
    """

    def __init__(self, ws, *, sleep=time.sleep):
        self._ws = ws
        self._sleep = sleep
        self._lock = threading.Lock()   # sheet_retry's delays assume serialized writers

    @property
    def enabled(self) -> bool:
        return self._ws is not None

    def log_lot(self, record: dict) -> bool:
        """Write the lot's rows.  True when they landed (False if there were none)."""
        rows = price_rows(record)
        if not rows or self._ws is None:
            return False
        item = record.get("item_id")
        with self._lock:
            for delay in sheet_retry.retry_delays():
                try:
                    self._ws.append_rows(rows, value_input_option="RAW")
                    return True
                except Exception as e:
                    if delay is None or not sheet_retry.is_rate_limited(e):
                        print(f">>> PRICE_LOG: write for {item} FAILED: "
                              f"{type(e).__name__}: {e}", flush=True)
                        return False
                    print(f">>> PRICE_LOG: write for {item} rate-limited — "
                          f"retrying in {delay:g}s", flush=True)
                    self._sleep(delay)
        return False


def open_price_sheet(gspread_client, spreadsheet_id):
    """Open (creating if needed) price_log and price_avg; return price_log.

    None on any failure, so the caller's PriceLogger is disabled — never fatal.
    price_avg is a convenience: failing to create it leaves price_log working.
    Titles are written RAW, so a listing title starting with "=" stays text.
    """
    key = mlog._extract_spreadsheet_key(spreadsheet_id)
    if not key:
        print(">>> PRICE_LOG: disabled — LOGGER_ID is empty.", flush=True)
        return None
    try:
        ss = gspread_client.open_by_key(key)
        ws = mlog._ensure_tab(ss, PRICE_TAB, PRICE_HEADER)
    except Exception as e:
        print(f">>> PRICE_LOG: open FAILED (price logging disabled): "
              f"{type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return None
    try:
        existing = {w.title for w in ss.worksheets()}
        if AVG_TAB not in existing:
            avg = ss.add_worksheet(title=AVG_TAB, rows=1000, cols=6)
            avg.update(values=[[avg_formula()]], range_name="A1",
                       value_input_option="USER_ENTERED")
    except Exception as e:
        print(f">>> PRICE_LOG: '{AVG_TAB}' tab not created ({type(e).__name__}: "
              f"{e}); '{PRICE_TAB}' still logs.", flush=True)
    print(f">>> PRICE_LOG: '{PRICE_TAB}' ready in '{ss.title}'.", flush=True)
    return ws
