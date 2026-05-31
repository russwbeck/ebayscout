"""
match_logging — structured, analysis-grade logging for the button-matching pipeline.

WHY THIS EXISTS
---------------
Google Cloud Run's log viewer is fine for debugging but useless for the thing we
actually want: learning how to *automate* button identification so the human does
less work over time.  To do that we need structured rows we can sort and pivot,
not free-text log lines.  Records are written to a Google Sheet (one tab for
per-crop detection/match data, one tab for human confirmations) so they're
browsable by eye and exportable.

This module is deliberately dependency-light: it imports only the standard
library.  The Sheet write is performed through an injected ``worksheet`` object
(anything with ``append_row`` / ``append_rows``), so the module is fully
unit-testable with no gspread, torch, cv2 or cloud access.  The same file is
copied verbatim into every bot (buttonmatcher, buybot, ebayscout) so there is
*no delta* in how the slash commands log.

SCALE NOTE
----------
An image produces one row per crop.  ``SheetLogger.log_image_crops`` batches all
of those rows into a single ``append_rows`` call so we make ~1 write per image
(plus 1 per confirmation) rather than one per crop — this keeps us well under
Sheets' write-rate quota for human-driven slash commands.  Every write is
fail-open: a logging failure is printed and swallowed, never raised into the bot.

THE TWO SIGNALS WE CARE ABOUT
-----------------------------
1. Detection: how many circles Hough finds *with* the user-supplied count/grid
   (the number that drives the real pipeline) vs *without any user input* (an
   unguided multi-scale sweep).  Only the with-count result is shown to the
   user; the no-input number is logged purely to measure — and eventually close
   — the gap, so detection can be automated.
2. Counterfactual ("shadow") matching: alongside the restricted result the user
   sees (bank era + Football only), we score the *unrestricted* universe (all
   years, all sports, no filter) and log where the eventually-confirmed answer
   ranks in it.  If that rank is consistently 1, the manual limitations are no
   longer earning their keep and we can automate them away.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import traceback


# --- Configuration -----------------------------------------------------------

SCHEMA_MATCH = "match.v1"
SCHEMA_CONFIRM = "confirm.v1"

MATCH_TAB = "match_log"
CONFIRM_TAB = "confirm_log"


def shadow_pass_enabled() -> bool:
    """The unrestricted counterfactual pass + unguided detection count run on
    every crop by default.

    Set BUTTONMATCHER_SHADOW_PASS=0 to disable them if CPU cost ever bites; the
    rest of the logging keeps working.
    """
    return os.environ.get("BUTTONMATCHER_SHADOW_PASS", "1").strip() not in (
        "0", "false", "False", "",
    )


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --- Counterfactual scoring (pure python, unit-testable) ---------------------

def build_leaderboard(
    text_sims,
    year_scores,
    text_years,
    text_phrases,
    text_types,
    *,
    normalize_fn,
    tokenize_fn,
    rarity_fn,
    stopwords,
    allowed_years=None,
    allowed_types=None,
    top_n=None,
):
    """Score *every* year and return them ranked best-first.

    This mirrors the live ``overall = 0.7*image + 0.3*text (+rarity)`` formula
    but, unlike ``score_slogans``, does NOT restrict the candidate pool to the
    dual-signal top-6.  That makes it suitable for the counterfactual question
    "with no limitations, where would the right answer rank?".

    Parameters
    ----------
    text_sims : sequence[float]
        Per-slogan CLIP text similarity, parallel to text_years/phrases/types.
    year_scores : dict[str|int, float]
        Per-year best image similarity (already computed for all years).
    allowed_years / allowed_types : set | None
        Optional filters.  Pass None for the fully-unrestricted leaderboard.
    top_n : int | None
        Trim to this many; None returns the full ranking (needed to compute the
        rank of a confirmed year that may sit deep in the list).

    Returns
    -------
    list[dict] with keys: year, image_score, text_score, overall, phrase, type
    """
    best_by_year = {}  # year(str) -> (best_text_sim, phrase, type)
    n = len(text_sims)
    for k in range(n):
        yr = str(text_years[k])
        if allowed_years is not None and yr not in allowed_years:
            continue
        ty = str(text_types[k])
        if allowed_types is not None and ty not in allowed_types:
            continue
        ts = float(text_sims[k])
        cur = best_by_year.get(yr)
        if cur is None or ts > cur[0]:
            best_by_year[yr] = (ts, text_phrases[k], ty)

    results = []
    for yr, (best_text, phrase, ty) in best_by_year.items():
        img_score = float(year_scores.get(yr, year_scores.get(_maybe_int(yr), 0.0)))
        norm_text = float(normalize_fn(best_text))
        overall = 0.7 * img_score + 0.3 * norm_text
        words = set(tokenize_fn(phrase)) - set(stopwords)
        if words:
            bonus = min(0.04 * sum(rarity_fn(w) for w in words) / len(words), 0.04)
            overall = min(1.0, overall + bonus)
        results.append({
            "year": yr,
            "image_score": round(img_score, 5),
            "text_score": round(norm_text, 5),
            "overall": round(overall, 5),
            "phrase": phrase,
            "type": ty,
        })

    results.sort(key=lambda r: r["overall"], reverse=True)
    if top_n is not None:
        return results[:top_n]
    return results


def _maybe_int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return s


def rank_of(year, ordered_results):
    """1-based rank of ``year`` within an ordered list of result dicts (or list
    of year strings).  Returns None if absent.  This is the headline automation
    metric: rank 1 means the unrestricted pipeline would have nailed it alone.
    """
    target = str(year)
    for i, r in enumerate(ordered_results, 1):
        ry = r["year"] if isinstance(r, dict) else r
        if str(ry) == target:
            return i
    return None


def trim_top(results, n=10):
    """Serialize the top-n results compactly for logging into a single cell.

    Defaults to the top 10 ("bulk slogan detection"): we want to see whether the
    correct slogan even reaches the front of the line across ALL reference
    images and slogans, and which references/slogans get over-promoted over time.
    """
    out = []
    for r in (results or [])[:n]:
        out.append({
            "year": str(r.get("year")),
            "phrase": r.get("phrase") or r.get("slogan"),
            "overall": _round_or_none(r.get("overall")),
            "image_score": _round_or_none(r.get("image_score")),
            "text_score": _round_or_none(r.get("text_score", r.get("slogan_score"))),
            "type": r.get("type"),
        })
    return out


def _round_or_none(x):
    return round(float(x), 5) if x is not None else None


# --- Record builders ---------------------------------------------------------

def build_detection_diag(
    *,
    h,
    w,
    bg_brightness,
    bg_is_white,
    mask_path,
    hough_pass1_count,
    hough_retry_count,
    final_count_user,
    final_count_noinput,
    user_count,
    detector_used,
    n_crops,
    bg_saturation=None,
):
    """Detection diagnostics block.

    ``final_count_user`` and ``final_count_noinput`` are the heart of the
    detection-automation question: the first uses the user-supplied count/grid,
    the second is an unguided sweep with no count.  The delta between them
    measures how much the human's count is still doing for us.

    ``bg_brightness`` / ``bg_saturation`` / ``bg_is_white`` / ``mask_path`` are
    "what the background sampler saw" — logged so we can correlate the sampled
    background against how many circles Hough finds.  The unguided count is NOT
    analyzed beyond counting (that would waste compute); we only want to see
    what it sees.
    """
    return {
        "h": int(h),
        "w": int(w),
        "bg_brightness": round(float(bg_brightness), 2),
        "bg_saturation": (None if bg_saturation is None else round(float(bg_saturation), 2)),
        "bg_is_white": bool(bg_is_white),
        "mask_path": mask_path,                       # "blue_only" | "blue_or_white"
        "hough_pass1_count": int(hough_pass1_count),
        "hough_retry_count": (None if hough_retry_count is None else int(hough_retry_count)),
        "final_count_user": int(final_count_user),
        "final_count_noinput": (None if final_count_noinput is None else int(final_count_noinput)),
        "user_count": (None if user_count in (None, "") else int(user_count)),
        "detector_used": detector_used,               # "hough" | "grid"
        "n_crops": int(n_crops),
    }


def build_match_record(
    *,
    service,
    command,
    mode,
    job_id,
    thread_ts,
    channel_id,
    user_id,
    crop_num,
    check_id,
    detection,
    bank,
    restricted_top,
    shadow_top,
    shadow_enabled,
):
    """One record per crop, written at detection/match time."""
    return {
        "schema": SCHEMA_MATCH,
        "ts": _now_iso(),
        "service": service,
        "command": command,
        "mode": mode,
        "job_id": job_id,
        "thread_ts": thread_ts,
        "channel_id": channel_id,
        "user_id": user_id,
        "crop_num": crop_num,
        "check_id": check_id,          # join key → confirmation record
        "detection": detection,        # dict from build_detection_diag()
        "bank": bank,
        "restricted_top": restricted_top,
        "shadow_enabled": bool(shadow_enabled),
        "shadow_top": shadow_top,
    }


def build_confirm_record(
    *,
    service,
    command,
    job_id,
    thread_ts,
    crop_num,
    check_id,
    user_id,
    chosen_year,
    chosen_phrase,
    chosen_type,
    source,
    rank_restricted,
    rank_shadow,
    shadow_leaderboard_size,
):
    """One record per user confirmation, written when the human picks an answer.

    ``rank_shadow`` is the key signal: the rank of the confirmed year in the
    *unrestricted* leaderboard.
    """
    return {
        "schema": SCHEMA_CONFIRM,
        "ts": _now_iso(),
        "service": service,
        "command": command,
        "job_id": job_id,
        "thread_ts": thread_ts,
        "crop_num": crop_num,
        "check_id": check_id,
        "user_id": user_id,
        "chosen_year": str(chosen_year),
        "chosen_phrase": chosen_phrase,
        "chosen_type": chosen_type,
        "source": source,                 # "pick"|"manual"|"dussellbot"|"other_sports"|"slogan_only"
        "rank_restricted": rank_restricted,
        "rank_shadow": rank_shadow,
        "shadow_leaderboard_size": shadow_leaderboard_size,
    }


# --- Flatteners: nested record → flat Sheet row ------------------------------

MATCH_HEADER = [
    "ts", "service", "command", "mode", "job_id", "thread_ts", "channel_id",
    "user_id", "crop_num", "check_id",
    "det_h", "det_w", "det_bg_brightness", "det_bg_saturation", "det_bg_is_white",
    "det_mask_path", "det_hough_pass1", "det_hough_retry", "det_count_user",
    "det_count_noinput", "det_user_count", "det_detector_used", "det_n_crops",
    "bank", "restricted_top_json", "shadow_enabled", "shadow_top_json",
]

CONFIRM_HEADER = [
    "ts", "service", "command", "job_id", "thread_ts", "crop_num", "check_id",
    "user_id", "chosen_year", "chosen_phrase", "chosen_type", "source",
    "rank_restricted", "rank_shadow", "shadow_leaderboard_size",
]


def _cell(v):
    """Render a value into a Sheet-safe scalar cell."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return v


def flatten_match_record(rec):
    d = rec.get("detection", {}) or {}
    return [
        _cell(rec.get("ts")), _cell(rec.get("service")), _cell(rec.get("command")),
        _cell(rec.get("mode")), _cell(rec.get("job_id")), _cell(rec.get("thread_ts")),
        _cell(rec.get("channel_id")), _cell(rec.get("user_id")), _cell(rec.get("crop_num")),
        _cell(rec.get("check_id")),
        _cell(d.get("h")), _cell(d.get("w")), _cell(d.get("bg_brightness")),
        _cell(d.get("bg_saturation")),
        _cell(d.get("bg_is_white")), _cell(d.get("mask_path")),
        _cell(d.get("hough_pass1_count")), _cell(d.get("hough_retry_count")),
        _cell(d.get("final_count_user")), _cell(d.get("final_count_noinput")),
        _cell(d.get("user_count")), _cell(d.get("detector_used")), _cell(d.get("n_crops")),
        _cell(rec.get("bank")),
        json.dumps(rec.get("restricted_top") or [], default=str),
        _cell(rec.get("shadow_enabled")),
        json.dumps(rec.get("shadow_top") or [], default=str),
    ]


def flatten_confirm_record(rec):
    return [
        _cell(rec.get("ts")), _cell(rec.get("service")), _cell(rec.get("command")),
        _cell(rec.get("job_id")), _cell(rec.get("thread_ts")), _cell(rec.get("crop_num")),
        _cell(rec.get("check_id")), _cell(rec.get("user_id")), _cell(rec.get("chosen_year")),
        _cell(rec.get("chosen_phrase")), _cell(rec.get("chosen_type")), _cell(rec.get("source")),
        _cell(rec.get("rank_restricted")), _cell(rec.get("rank_shadow")),
        _cell(rec.get("shadow_leaderboard_size")),
    ]


# --- Sheet logger ------------------------------------------------------------

class SheetLogger:
    """Writes structured rows to two injected gspread-style worksheets.

    ``match_ws`` and ``confirm_ws`` need only support ``append_rows(rows)`` and
    ``append_row(row)``.  Either may be None (logging silently disabled).  Never
    raises into the caller: logging must not break the bot.
    """

    def __init__(self, match_ws, confirm_ws, *, service):
        self._match_ws = match_ws
        self._confirm_ws = confirm_ws
        self._service = service
        self._warned_disabled = False   # so a disabled logger says so ONCE
        self._logged_first_write = False

    @property
    def service(self):
        return self._service

    @property
    def enabled(self):
        return self._match_ws is not None or self._confirm_ws is not None

    def _warn_disabled_once(self, what):
        if not self._warned_disabled:
            print(f">>> MATCH_LOG: SKIPPED {what} — logging is DISABLED "
                  f"(no worksheet handle; check LOGGER_ID + sheet sharing at "
                  f"startup).", flush=True)
            self._warned_disabled = True

    def log_image_crops(self, job_id, records):
        """Append all per-crop match rows for one image in a single batched call."""
        if not records:
            return
        if self._match_ws is None:
            self._warn_disabled_once("match write")
            return
        try:
            rows = [flatten_match_record(r) for r in records]
            self._match_ws.append_rows(rows, value_input_option="RAW")
            if not self._logged_first_write:
                print(f">>> MATCH_LOG: ✅ first match write OK "
                      f"({len(rows)} row(s), job {job_id}).", flush=True)
                self._logged_first_write = True
        except Exception as e:
            print(f">>> MATCH_LOG: match write FAILED for job {job_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            traceback.print_exc()

    def log_confirmation(self, check_id, record):
        if self._confirm_ws is None:
            self._warn_disabled_once("confirm write")
            return
        try:
            self._confirm_ws.append_row(
                flatten_confirm_record(record), value_input_option="RAW"
            )
        except Exception as e:
            print(f">>> MATCH_LOG: confirm write FAILED for {check_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            traceback.print_exc()


def _extract_spreadsheet_key(raw):
    """Accept either a bare spreadsheet key or a full Google Sheets URL (and
    tolerate stray whitespace/newlines in the secret).  Returns the key.

    A common setup mistake is pasting the whole URL into the LOGGER_ID secret;
    gspread.open_by_key needs just the key, so we extract it here.
    """
    s = (raw or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", s)
    if m:
        return m.group(1)
    return s


def open_log_sheets(gspread_client, spreadsheet_id):
    """Open (creating if needed) the match_log / confirm_log tabs and ensure
    headers exist.  Returns (match_ws, confirm_ws).  On any failure returns
    (None, None) so the caller's SheetLogger is simply disabled — never fatal,
    but the reason is printed loudly so a silent disable is diagnosable.

    Not unit-tested for the live gspread path; the key extraction is.
    """
    key = _extract_spreadsheet_key(spreadsheet_id)
    _hint = f"key='{key[:6]}…{key[-4:]}' (len={len(key)})" if key else "key=EMPTY"
    if not key:
        print(">>> MATCH_LOG: open_log_sheets ABORT — LOGGER_ID is empty. "
              "Set the LOGGER_ID secret to the logging spreadsheet's key.", flush=True)
        return None, None
    try:
        ss = gspread_client.open_by_key(key)
        match_ws = _ensure_tab(ss, MATCH_TAB, MATCH_HEADER)
        confirm_ws = _ensure_tab(ss, CONFIRM_TAB, CONFIRM_HEADER)
        print(f">>> MATCH_LOG: opened logging workbook '{ss.title}' ({_hint}); "
              f"tabs '{MATCH_TAB}' + '{CONFIRM_TAB}' ready.", flush=True)
        return match_ws, confirm_ws
    except Exception as e:
        print(f">>> MATCH_LOG: open_log_sheets FAILED ({_hint}): {type(e).__name__}: {e}. "
              "Most likely the logging spreadsheet is NOT shared with the bot's "
              "service-account email (give it Editor), or LOGGER_ID is wrong.",
              flush=True)
        traceback.print_exc()
        return None, None


def _ensure_tab(ss, title, header):
    try:
        ws = ss.worksheet(title)
    except Exception:
        ws = ss.add_worksheet(title=title, rows=1000, cols=max(26, len(header)))
        ws.append_row(header, value_input_option="RAW")
        return ws
    # Backfill header if the tab is empty.
    try:
        first = ws.row_values(1)
        if not first:
            ws.append_row(header, value_input_option="RAW")
    except Exception:
        pass
    return ws
