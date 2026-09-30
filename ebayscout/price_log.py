"""
price_log.py — button prices, listed and sold, in the logging workbook.

SHARED, byte-identical: ebayscout/ebayscout/price_log.py and
buttonmatcher/price_log.py.  It imports its sibling relative-first, flat-second,
the way match_logging.py imports sheet_retry, so one file works in ebayscout's
package layout and buttonmatcher's flat one.  Pure apart from the injected
worksheet: no gspread import, no clock beyond the row timestamp.

Three writers, one tab, one key
-------------------------------
Every row is one identified button in one lot, carrying that lot's price per
button.  A lot holding three of one button gives that button ONE row with
n_in_lot = 3, so a lot is one observation whatever its duplicates.

  kind     source      written by
  listing  scan        ebayscout: a lot the daily scan or /crawl read, at the
                       price it was listed at when scanned
  sold     auction     ebayscout: the auction tracker, when an auction the scan
                       saw closes with a bid
  sold     scout_sold  buttonmatcher: /scout sold, a sale the operator reported
                       and confirmed button by button

``ebay_id`` is the eBay item number (the digits in ``.../itm/<number>``), so a
listing and its sale join on one value however each writer first saw it.

One sale counts once
--------------------
The tracker and /scout sold can both record the same sale, and the operator's
entry is the better record (every button confirmed, prices set by hand).  So:
/scout sold marks every earlier sold row for its item superseded; the tracker
writes its rows already superseded when the operator got there first; and the
tracker never writes an item twice.  Both formula tabs read superseded = no only.

Tabs
----
price_log      the rows.
price_avg      per year + slogan, listing vs sold: average price per button and
               the number of lots.  Auction LISTINGS are left out of the listing
               average — an auction's price when scanned is its opening or
               current bid, not what anyone is asking — but their sale counts.
price_by_item  per eBay item: the lot's listing price beside its sold price.
Each formula tab is written once, when it is created; edits to it survive.
"""

from __future__ import annotations

import datetime
import re
import threading
import time
import traceback

try:                                    # ebayscout: ebayscout/price_log.py
    from . import sheet_retry
except ImportError:                     # buttonmatcher: ./price_log.py
    import sheet_retry

PRICE_TAB = "price_log"
AVG_TAB = "price_avg"
ITEM_TAB = "price_by_item"

KIND_LISTING = "listing"
KIND_SOLD = "sold"

SOURCE_SCAN = "scan"
SOURCE_AUCTION = "auction"
SOURCE_SCOUT_SOLD = "scout_sold"

FORMAT_AUCTION = "auction"
FORMAT_BUY_IT_NOW = "buy_it_now"
FORMAT_BEST_OFFER = "best_offer"
FORMAT_OTHER = "other"
FORMATS = (FORMAT_AUCTION, FORMAT_BUY_IT_NOW, FORMAT_BEST_OFFER, FORMAT_OTHER)

BASIS_ASKING = "asking"        # the listing's price when the scan read it
BASIS_FINAL = "final"          # eBay still showed the ended auction: its close
BASIS_LAST_SEEN = "last_seen"  # the last bid seen before close; a late bid is missed
BASIS_ENTERED = "entered"      # typed by the operator

PRICE_HEADER = [
    "ts", "kind", "source", "ebay_id", "year", "slogan", "n_in_lot",
    "buttons_detected", "lot_price", "shipping", "price_per_button",
    "allocation", "sale_format", "bids", "sale_date", "price_basis",
    "superseded", "title", "listing_url", "run_id",
]


def col(name: str) -> str:
    """The sheet column letter of a price_log field (A–Z; the header is short)."""
    return chr(ord("A") + PRICE_HEADER.index(name))


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# --- the shared key --------------------------------------------------------------

_ITEM_PATTERNS = (
    re.compile(r"^v1\|(\d{9,15})\|"),                        # Browse API item id
    re.compile(r"/itm/(?:[^/?#\s]*/)?(\d{9,15})(?:[/?#\s]|$)"),  # listing URL
    re.compile(r"[?&](?:item|itm|itemId|item_id)=(\d{9,15})(?:&|#|$)"),
)


def ebay_item_number(value) -> str:
    """The eBay item number in ``value``, or "" when there is none.

    Takes what either side has in hand: the number itself, a Browse API id
    (``v1|<number>|0``), or a listing URL (``ebay.com/itm/<number>``, with or
    without the title slug).  A short share link (ebay.io / ebay.us) carries no
    number until it is followed, which is the caller's job.
    """
    s = str(value or "").strip()
    if re.fullmatch(r"\d{9,15}", s):
        return s
    for p in _ITEM_PATTERNS:
        m = p.search(s)
        if m:
            return m.group(1)
    return ""


def listing_format(buying_options) -> str:
    """The sale_format of a listing from eBay's buyingOptions."""
    opts = {str(o).upper() for o in (buying_options or [])}
    if "AUCTION" in opts:
        return FORMAT_AUCTION
    if "FIXED_PRICE" in opts:
        return FORMAT_BUY_IT_NOW
    return FORMAT_OTHER


# --- pricing a lot ---------------------------------------------------------------

def counts_from_buttons(buttons) -> dict:
    """{(year, slogan): n} from ``[{"year", "slogan", "n"}]`` (scan_log's shape)
    or from one entry per button (n absent means one)."""
    counts: dict = {}
    for b in buttons or []:
        if b.get("year") is None or not b.get("slogan"):
            continue
        k = (_year(b["year"]), b["slogan"])
        counts[k] = counts.get(k, 0) + int(b.get("n") or 1)
    return counts


def split_sale(total, buttons_in_lot, counts, overrides=None) -> dict:
    """The price of each identified button of a lot sold for ``total``.

    ``counts`` is {(year, slogan): n} for the identified buttons; ``overrides``
    is {(year, slogan): price each} for the ones the operator priced by hand.
    Every other button in the lot — identified or not — shares what the
    overrides leave, so the lot always adds back up to what it sold for.

    Raises ValueError, worded for the operator, when the overrides come to more
    than the sale, or price every button without adding up to it.
    """
    total = float(total or 0)
    counts = {k: int(n) for k, n in (counts or {}).items() if int(n) > 0}
    overrides = {k: float(v) for k, v in (overrides or {}).items() if k in counts}
    n_named = sum(counts.values())
    n_lot = max(int(buttons_in_lot or 0), n_named)
    if total <= 0 or n_lot <= 0:
        raise ValueError("A sale needs a price above $0 and at least one button.")
    if any(v < 0 for v in overrides.values()):
        raise ValueError("A button's price can't be negative.")
    fixed = sum(v * counts[k] for k, v in overrides.items())
    rest = n_lot - sum(counts[k] for k in overrides)
    left = total - fixed
    if left < -0.005:
        raise ValueError(f"Those prices add up to ${fixed:.2f}, more than the "
                         f"${total:.2f} it sold for.")
    if rest == 0 and abs(left) >= 0.005:
        raise ValueError(f"Every button has a price and they add up to "
                         f"${fixed:.2f}, not the ${total:.2f} it sold for.")
    share = left / rest if rest else 0.0
    return {k: round(overrides.get(k, share), 2) for k in counts}


# --- rows --------------------------------------------------------------------------

def _year(y):
    """A year as a number when it is one.  QUERY treats a column of mixed types
    as its majority type and blanks the rest, so "1985" and 1985 must not mix."""
    s = str(y).strip()
    return int(s) if s.isdigit() else s


def _cell(v):
    return "" if v is None else v


def lot_rows(*, kind, source, ebay_id, counts, prices, buttons_in_lot, lot_price,
             shipping=None, allocation="even", sale_format=FORMAT_OTHER,
             bids=None, sale_date="", price_basis="", superseded=False,
             title="", listing_url="", run_id="", ts=None) -> list[list]:
    """One price_log row per identified button of one lot, in PRICE_HEADER order.

    ``prices`` is {(year, slogan): price per button}, from split_sale or the
    listing's even split.  Written RAW, so a title starting with "=" stays text.
    """
    ts = ts or _now_iso()
    rows = []
    for (year, slogan), n in sorted(counts.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        rows.append([
            ts, kind, source, _cell(ebay_id), _year(year), slogan, n,
            _cell(buttons_in_lot), _cell(lot_price), _cell(shipping),
            prices[(year, slogan)], allocation, sale_format or FORMAT_OTHER,
            _cell(bids), _cell(sale_date), price_basis,
            "yes" if superseded else "no",
            _cell(title), _cell(listing_url), _cell(run_id),
        ])
    return rows


def listing_rows(record: dict) -> list[list]:
    """The listing rows for one lot's scan_log record (ebayscout).

    No rows when the lot has no price per button (no price, or nothing
    detected): a row without a price would only dilute the average.
    """
    per = record.get("price_per_button")
    if isinstance(per, bool) or not isinstance(per, (int, float)) or per <= 0:
        return []
    counts = counts_from_buttons(record.get("buttons"))
    if not counts:
        return []
    return lot_rows(
        kind=KIND_LISTING, source=SOURCE_SCAN,
        ebay_id=(ebay_item_number(record.get("item_id"))
                 or ebay_item_number(record.get("listing_url"))),
        counts=counts, prices={k: per for k in counts},
        buttons_in_lot=record.get("buttons_detected"), lot_price=record.get("asking"),
        sale_format=listing_format(record.get("buying_options")),
        bids=record.get("bid_count"), price_basis=BASIS_ASKING,
        title=record.get("title"), listing_url=record.get("listing_url"),
        run_id=record.get("run_id"), ts=record.get("ts"),
    )


# --- one sale counts once --------------------------------------------------------

def live_sales(bd_rows, q_rows, ebay_id) -> list[tuple[int, str]]:
    """(sheet row, source) of every sold row for ``ebay_id`` not yet superseded.

    ``bd_rows`` / ``q_rows`` are the kind..ebay_id (B:D) and superseded (Q:Q)
    columns as the Sheets API returns them: row 1 is the header, and trailing
    empty cells and rows are trimmed, so any cell may be missing.
    """
    if not ebay_id:
        return []
    out = []
    for i, r in enumerate(bd_rows or []):
        if i == 0:
            continue
        kind = r[0] if len(r) > 0 else ""
        source = r[1] if len(r) > 1 else ""
        eid = str(r[2]).strip() if len(r) > 2 else ""
        sup = q_rows[i][0] if i < len(q_rows or []) and q_rows[i] else ""
        if kind == KIND_SOLD and eid == ebay_id and sup != "yes":
            out.append((i + 1, source))
    return out


class PriceLogger:
    """Writes price rows to the injected price_log worksheet.

    ``ws`` needs ``append_rows``, and for sales ``batch_get`` / ``batch_update``;
    None disables it.  Every call is retried on the Sheets per-minute quota
    (sheet_retry) and never raises: a price row is a record of the work, not a
    reason to fail it.
    """

    def __init__(self, ws, *, sleep=time.sleep):
        self._ws = ws
        self._sleep = sleep
        self._lock = threading.Lock()   # sheet_retry's delays assume serialized writers

    @property
    def enabled(self) -> bool:
        return self._ws is not None

    def _call(self, fn, what):
        """(True, result) once ``fn`` succeeds; (False, None) when it fails for
        good.  Callers hold self._lock."""
        for delay in sheet_retry.retry_delays():
            try:
                return True, fn()
            except Exception as e:
                if delay is None or not sheet_retry.is_rate_limited(e):
                    print(f">>> PRICE_LOG: {what} FAILED: {type(e).__name__}: {e}",
                          flush=True)
                    return False, None
                print(f">>> PRICE_LOG: {what} rate-limited — retrying in {delay:g}s",
                      flush=True)
                self._sleep(delay)
        return False, None

    def _append(self, rows, what) -> bool:
        ok, _ = self._call(lambda: self._ws.append_rows(rows, value_input_option="RAW"), what)
        return ok

    def log_listing(self, record: dict) -> bool:
        """A scanned lot's listing rows.  True when they landed."""
        rows = listing_rows(record)
        if not rows or self._ws is None:
            return False
        with self._lock:
            return self._append(rows, f"listing write for {record.get('item_id')}")

    def log_sale(self, rows, ebay_id, *, source) -> dict:
        """Write one sale's rows so that the sale counts once.

        Returns {"status": ..., "replaced": n}: "written"; "superseded" (the
        tracker's rows, written already superseded because the operator logged
        this item first); "duplicate" (the tracker already has it — nothing
        written); "empty"; or "failed".  ``replaced`` counts the earlier rows a
        /scout sold entry superseded.
        """
        if not rows:
            return {"status": "empty", "replaced": 0}
        if self._ws is None:
            return {"status": "failed", "replaced": 0}
        sup_i = PRICE_HEADER.index("superseded")
        with self._lock:
            live = []
            if ebay_id:
                ok, got = self._call(
                    lambda: self._ws.batch_get([f"{col('kind')}:{col('ebay_id')}",
                                                f"{col('superseded')}:{col('superseded')}"]),
                    f"sale lookup for {ebay_id}")
                if ok and got and len(got) == 2:
                    live = live_sales(got[0], got[1], ebay_id)
            sources = {s for _, s in live}
            status = "written"
            if source == SOURCE_AUCTION:
                if SOURCE_AUCTION in sources:
                    return {"status": "duplicate", "replaced": 0}
                if sources:
                    rows = [r[:sup_i] + ["yes"] + r[sup_i + 1:] for r in rows]
                    status = "superseded"
            if not self._append(rows, f"sale write for {ebay_id or '(no item number)'}"):
                return {"status": "failed", "replaced": 0}
            replaced = 0
            if source != SOURCE_AUCTION and live:
                cells = [{"range": f"{col('superseded')}{r}", "values": [["yes"]]}
                         for r, _ in live]
                ok, _ = self._call(
                    lambda: self._ws.batch_update(cells, value_input_option="RAW"),
                    f"superseding {len(cells)} earlier row(s) for {ebay_id}")
                replaced = len(cells) if ok else 0
            return {"status": status, "replaced": replaced}


# --- the workbook ------------------------------------------------------------------

def avg_formula() -> str:
    """price_avg: average price per button and lot count, per year + slogan,
    listing beside sold.  Built from PRICE_HEADER so a reordered column cannot
    silently average the wrong one."""
    y, s, p, k = col("year"), col("slogan"), col("price_per_button"), col("kind")
    f, sup = col("sale_format"), col("superseded")
    return (f"=QUERY({PRICE_TAB}!A:{col(PRICE_HEADER[-1])}, "
            f"\"select {y}, {s}, avg({p}), count({p}) "
            f"where {p} is not null and {sup} = 'no' "
            f"and not ({k} = '{KIND_LISTING}' and {f} = '{FORMAT_AUCTION}') "
            f"group by {y}, {s} pivot {k} order by {y}, {s} "
            f"label avg({p}) 'avg $/button', count({p}) 'lots'\", 1)")


def item_formula() -> str:
    """price_by_item: each eBay item's lot price as listed and as sold."""
    i, p, k, sup = col("ebay_id"), col("lot_price"), col("kind"), col("superseded")
    return (f"=QUERY({PRICE_TAB}!A:{col(PRICE_HEADER[-1])}, "
            f"\"select {i}, max({p}) where {i} != '' and {sup} = 'no' "
            f"group by {i} pivot {k} label max({p}) 'lot $'\", 1)")


def _spreadsheet_key(raw) -> str:
    """A bare key, or the key out of a pasted spreadsheet URL."""
    s = (raw or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", s)
    return m.group(1) if m else s


def open_price_sheet(gspread_client, spreadsheet_id):
    """Open (creating if needed) price_log and its two formula tabs; return
    price_log's worksheet.

    None on any failure, so the caller's PriceLogger is disabled — never fatal.
    Also None when price_log exists with a different header: appending under
    the wrong columns would corrupt every average without an error anywhere.
    The formula tabs are a convenience: failing to create one leaves the log
    working.
    """
    key = _spreadsheet_key(spreadsheet_id)
    if not key:
        print(">>> PRICE_LOG: disabled — LOGGER_ID is empty.", flush=True)
        return None
    try:
        ss = gspread_client.open_by_key(key)
        existing = {w.title: w for w in ss.worksheets()}
        ws = existing.get(PRICE_TAB)
        if ws is None:
            ws = ss.add_worksheet(title=PRICE_TAB, rows=1000, cols=len(PRICE_HEADER))
            ws.append_row(PRICE_HEADER, value_input_option="RAW")
        else:
            first = ws.row_values(1)
            if not first:
                ws.append_row(PRICE_HEADER, value_input_option="RAW")
            elif first != PRICE_HEADER:
                print(f">>> PRICE_LOG: disabled — '{PRICE_TAB}' has a different "
                      f"header ({first[:4]}…); rename that tab to start a new one.",
                      flush=True)
                return None
    except Exception as e:
        print(f">>> PRICE_LOG: open FAILED (price logging disabled): "
              f"{type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return None
    for title, formula in ((AVG_TAB, avg_formula()), (ITEM_TAB, item_formula())):
        if title in existing:
            continue
        try:
            tab = ss.add_worksheet(title=title, rows=1000, cols=8)
            tab.update(values=[[formula]], range_name="A1",
                       value_input_option="USER_ENTERED")
        except Exception as e:
            print(f">>> PRICE_LOG: '{title}' tab not created ({type(e).__name__}: "
                  f"{e}); '{PRICE_TAB}' still logs.", flush=True)
    print(f">>> PRICE_LOG: '{PRICE_TAB}' ready in '{ss.title}'.", flush=True)
    return ws
