"""
ebayscout/sheets_client.py

Google Sheets client for loading buy rules.

Ported from buybot/main.py: load_buy_rules() and get_buy_decision().
Adapted to a stateless interface (no global state) so the job can call
these functions cleanly without a running Flask app.

Expected sheet columns (case-insensitive):
    Year | Slogan | Count | Max Price Single | Max Price Year | Notes | Amount Needed
"""

import json
import re

import gspread
from google.oauth2 import service_account


def get_gspread_client(sheets_json: str):
    """Return an authorized gspread client from the service-account JSON.

    Shares the same credentials path as load_buy_rules(); used to open the
    match_logging workbook (LOGGER_ID). Raises on bad credentials so the caller
    can disable logging fail-open.
    """
    creds_info   = json.loads(sheets_json)
    creds        = service_account.Credentials.from_service_account_info(creds_info)
    scoped_creds = creds.with_scopes(["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(scoped_creds)


def load_buy_rules(sheets_json: str, spreadsheet_id: str) -> dict:
    """
    Connect to Google Sheets and load all buy rules into a dict.

    Returns:
        {(year_str, slogan_str): {
            "max_price_single": str,
            "max_price_year":   str,
            "notes":            str,
            "amount_needed":    str,  # raw string; convert to int when needed
        }}

    Raises RuntimeError if the sheet cannot be loaded (caller should exit).
    """
    try:
        creds_info   = json.loads(sheets_json)
        creds        = service_account.Credentials.from_service_account_info(creds_info)
        scoped_creds = creds.with_scopes(["https://www.googleapis.com/auth/spreadsheets"])
        gc           = gspread.authorize(scoped_creds)
        sheet        = gc.open_by_key(spreadsheet_id).sheet1
        rows         = sheet.get_all_values()
    except Exception as exc:
        raise RuntimeError(f"Failed to connect to Google Sheets: {exc}") from exc

    if not rows:
        raise RuntimeError("Buy-rules sheet is empty.")

    header = [h.strip().lower() for h in rows[0]]

    try:
        year_col         = header.index("year")
        slogan_col       = header.index("slogan")
        price_single_col = header.index("max price single")
        price_year_col   = header.index("max price year")
        notes_col        = header.index("notes")
    except ValueError as exc:
        raise RuntimeError(f"Missing expected column in sheet: {exc}") from exc

    try:
        amount_needed_col: int | None = header.index("amount needed")
    except ValueError:
        amount_needed_col = None

    buy_rules: dict = {}
    count = 0
    for row in rows[1:]:
        if len(row) <= max(year_col, slogan_col):
            continue
        year   = str(row[year_col]).strip()
        slogan = row[slogan_col].strip()
        if not year or not slogan:
            continue

        price_single  = row[price_single_col].strip() if len(row) > price_single_col else ""
        price_year    = row[price_year_col].strip()   if len(row) > price_year_col   else ""
        notes         = row[notes_col].strip()        if len(row) > notes_col        else ""
        amount_needed = ""
        if amount_needed_col is not None and len(row) > amount_needed_col:
            amount_needed = row[amount_needed_col].strip()

        buy_rules[(year, slogan)] = {
            "max_price_single": price_single,
            "max_price_year":   price_year,
            "notes":            notes,
            "amount_needed":    amount_needed,
        }
        count += 1

    print(f">>> SHEETS: Loaded {count} buy rules.", flush=True)
    return buy_rules


def get_buy_decision(
    year: str | int,
    slogan: str,
    buy_rules: dict,
) -> tuple[str, str, str, int]:
    """
    Look up (year, slogan) in buy_rules with fuzzy fallback.

    Returns:
        (max_price_single, max_price_year, notes, amount_needed_int)

    amount_needed is returned as int (0 if missing / non-numeric).
    Returns ("", "", "", 0) if no rule found.
    """
    key  = (str(year).strip(), slogan.strip())
    rule = buy_rules.get(key)

    if rule is None:
        norm_input = _normalize_key(slogan)
        for (r_year, r_slogan), r_rule in buy_rules.items():
            if r_year == str(year).strip() and _normalize_key(r_slogan) == norm_input:
                rule = r_rule
                break

    if rule is None:
        return "", "", "", 0

    try:
        amount_needed = int(rule.get("amount_needed", 0) or 0)
    except (ValueError, TypeError):
        amount_needed = 0

    return (
        rule["max_price_single"],
        rule["max_price_year"],
        rule["notes"],
        amount_needed,
    )


def parse_price(price_str: str) -> float:
    """
    Convert a price string like '$1.50' or '1.50' to float.
    Returns 0.0 if the string is empty or unparseable.
    """
    try:
        return float(price_str.replace("$", "").strip())
    except (ValueError, AttributeError):
        return 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_key(s: str) -> str:
    """Strip punctuation, lowercase, strip whitespace — identical to buybot."""
    return re.sub(r"[^\w\s]", "", s.lower()).strip()
