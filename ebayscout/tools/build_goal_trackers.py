"""build_goal_trackers — turn LOGGER_FRONTS.md into the Progress Trackers workbook.

WHY THIS EXISTS
---------------
`LOGGER_FRONTS.md` is the register of every front we are measuring with the
Logger: the question, the instrument, the gate that would settle it, and where
it stands. That file is the source of truth and is meant to be read and edited
by hand.

This script parses it and emits a spreadsheet with one tab per front, so each
front gets somewhere to record its readings batch by batch — which the markdown
deliberately does not do (a doc is a bad place for a time series). The workbook
therefore cannot claim anything the register does not: add a front to the md and
it gets a tab; change a gate and the tab's target changes.

What the workbook adds over the doc is the PROGRESS LOG on each tab: one dated
line per Logger export, so a front's trajectory is visible rather than just its
latest state. The INDEX rolls the newest line of every log back up.

USAGE
-----
    python ebayscout/tools/build_goal_trackers.py \\
        --register LOGGER_FRONTS.md -o "Logger - Progress Trackers.xlsx"
"""

from __future__ import annotations

import argparse
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.hyperlink import Hyperlink

# Every field an entry must carry.  A front missing one fails the build rather
# than producing a tab with a blank target.
FIELDS = ("Track", "Status", "Question", "Instrument", "Gate", "Standing", "Source")

# Statuses, in the order they should sort on the INDEX: live work first,
# settled history last.
STATUS_ORDER = [
    "OPEN", "SHADOW", "PROPOSED", "BUILT-UNGRADED", "BLOCKED",
    "SHIPPED-WATCH", "DECIDED-HOLD", "SETTLED-CONFIRMED", "SETTLED-REFUTED",
]

NAVY = "FF041E42"
LIGHT = "FFEFF3F8"
RULE = "FFC8D2E0"
H1 = Font(size=14, bold=True, color="FFFFFFFF")
WHITE_F = Font(color="FFFFFFFF", bold=True)
BOLD = Font(bold=True)
DIM = Font(color="FF5A6B80", italic=True)
LINK = Font(color="FF1155CC", underline="single")
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")
_thin = Side(style="thin", color=RULE)
BOX = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)


# --- parsing ----------------------------------------------------------------

def parse_register(path):
    """Read LOGGER_FRONTS.md into [{id, title, track, ...}] in file order."""
    text = open(path, encoding="utf-8").read()
    fronts, track = [], ""
    # Sections are "## A. Matching and auto-confirm"; fronts are "### A1 — Title".
    for chunk in re.split(r"\n(?=#{2,3} )", text):
        head, _, body = chunk.partition("\n")
        if head.startswith("## "):
            m = re.match(r"##\s+[A-E]\.\s+(.*)", head)
            if m:
                track = m.group(1).strip()
            continue
        if not head.startswith("### "):
            continue
        m = re.match(r"###\s+([A-E]\d+)\s+—\s+(.*)", head)
        if not m:
            raise SystemExit(f"malformed front heading: {head!r}")
        fid, title = m.group(1), m.group(2).strip()
        fields = {}
        for fm in re.finditer(r"^-\s+\*\*(.+?):\*\*\s*(.*?)(?=\n-\s+\*\*|\n*\Z)",
                              body, re.S | re.M):
            fields[fm.group(1).strip()] = " ".join(fm.group(2).split())
        missing = [f for f in FIELDS if not fields.get(f)]
        if missing:
            raise SystemExit(f"{fid}: missing {', '.join(missing)}")
        if fields["Status"] not in STATUS_ORDER:
            raise SystemExit(f"{fid}: unknown status {fields['Status']!r}")
        fronts.append(dict(id=fid, title=title, section=track, **fields))
    if not fronts:
        raise SystemExit(f"no fronts parsed from {path}")
    return fronts


def tab_name(front):
    """Excel caps sheet names at 31 chars and forbids []:*?/\\ — and the name is
    also a formula reference, so keep it boring."""
    slug = re.sub(r"[\[\]:*?/\\']", "", front["title"])
    name = f"{front['id']} {slug}"[:31].rstrip(" -—")
    return name


# --- writing ----------------------------------------------------------------

def _band(ws, row, text, span, font=None, fill=LIGHT):
    ws.cell(row=row, column=1, value=text).font = font or BOLD
    for c in range(1, span + 1):
        ws.cell(row=row, column=c).fill = PatternFill("solid", fgColor=fill)


def _kv(ws, row, key, value, height=None):
    k = ws.cell(row=row, column=1, value=key)
    k.font = BOLD
    k.alignment = TOP
    ws.cell(row=row, column=2, value=value).alignment = WRAP
    if height:
        ws.row_dimensions[row].height = height


LOG_COLS = ["Date", "Logger export", "n", "Reading", "Meets gate?",
            "Status after", "Note"]
LOG_HEADER_ROW = 16
LOG_ROWS = 14


def write_front_tab(wb, front):
    ws = wb.create_sheet(tab_name(front))
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 104
    for L in "CDEFG":
        ws.column_dimensions[L].width = 18

    _band(ws, 1, f"{front['id']} — {front['title']}", 7, font=H1, fill=NAVY)
    ws.cell(row=2, column=1, value=front["Question"]).alignment = WRAP
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=7)
    ws.row_dimensions[2].height = 44

    _band(ws, 4, "THE FRONT", 7)
    _kv(ws, 5, "Track", front["Track"])
    _kv(ws, 6, "Status", front["Status"])
    _kv(ws, 7, "Instrument", front["Instrument"], height=46)
    _kv(ws, 8, "Gate", front["Gate"], height=60)
    _kv(ws, 9, "Standing", front["Standing"], height=74)
    _kv(ws, 10, "Source", front["Source"], height=30)
    _kv(ws, 11, "Owner", "")
    _kv(ws, 12, "Next action", "")
    ws.cell(row=13, column=2,
            value="Standing is as of the register; log new readings below.").font = DIM

    _band(ws, LOG_HEADER_ROW - 1,
          "PROGRESS LOG — one line per Logger export graded against the gate", 7)
    for i, h in enumerate(LOG_COLS, start=1):
        c = ws.cell(row=LOG_HEADER_ROW, column=i, value=h)
        c.font = BOLD
        c.fill = PatternFill("solid", fgColor=LIGHT)
        c.border = BOX
    for k in range(LOG_ROWS):
        r = LOG_HEADER_ROW + 1 + k
        ws.cell(row=r, column=1).number_format = "yyyy-mm-dd"
    ws.freeze_panes = "A4"


IDX_COLS = ["Front", "Title", "Track", "Status", "Latest reading", "As of",
            "Owner", "Next action", "Gate", "Standing"]


def write_index(wb, fronts):
    ws = wb.create_sheet("INDEX", 1)
    _band(ws, 1, f"INDEX — {len(fronts)} fronts", len(IDX_COLS), font=H1, fill=NAVY)
    ws.cell(row=2, column=1, value=(
        "Latest reading is the newest filled line of each front's progress log. "
        "Filter by Track or Status; click a front to jump to its tab.")).font = DIM

    hr = 4
    for i, h in enumerate(IDX_COLS, start=1):
        c = ws.cell(row=hr, column=i, value=h)
        c.font = WHITE_F
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.border = BOX
    for i, w in enumerate([8, 34, 26, 18, 22, 12, 12, 28, 70, 70], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    first, last = LOG_HEADER_ROW + 1, LOG_HEADER_ROW + LOG_ROWS
    for k, f in enumerate(fronts):
        r = hr + 1 + k
        q = f"'{tab_name(f)}'"
        c = ws.cell(row=r, column=1, value=f["id"])
        c.hyperlink = Hyperlink(ref=c.coordinate, location=f"{q}!A1")
        c.font = LINK
        ws.cell(row=r, column=2, value=f["title"])
        ws.cell(row=r, column=3, value=f["Track"])
        ws.cell(row=r, column=4, value=f["Status"])
        # LOOKUP(2, 1/(range<>""), range) returns the LAST non-empty cell.
        for col, letter in ((5, "D"), (6, "A")):
            rng = f"{q}!{letter}{first}:{letter}{last}"
            ws.cell(row=r, column=col,
                    value=f'=IFERROR(LOOKUP(2,1/({rng}<>""),{rng}),"—")')
        ws.cell(row=r, column=6).number_format = "yyyy-mm-dd"
        ws.cell(row=r, column=7, value=f"={q}!B11")
        ws.cell(row=r, column=8, value=f"={q}!B12")
        ws.cell(row=r, column=9, value=f["Gate"]).alignment = WRAP
        ws.cell(row=r, column=10, value=f["Standing"]).alignment = WRAP
    ws.freeze_panes = f"C{hr + 1}"
    ws.auto_filter.ref = (f"A{hr}:{get_column_letter(len(IDX_COLS))}"
                          f"{hr + len(fronts)}")


def write_rollup(wb, fronts):
    """Status and track counts — how the program is distributed, at a glance."""
    ws = wb.create_sheet("ROLLUP", 2)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 76
    _band(ws, 1, "ROLLUP", 3, font=H1, fill=NAVY)

    r = 3
    _band(ws, r, "By status", 3); r += 1
    counts = {s: sum(1 for f in fronts if f["Status"] == s) for s in STATUS_ORDER}
    for s in STATUS_ORDER:
        ws.cell(row=r, column=1, value=s)
        ws.cell(row=r, column=2, value=counts[s])
        ws.cell(row=r, column=3,
                value=", ".join(f["id"] for f in fronts
                                if f["Status"] == s)).alignment = WRAP
        r += 1
    ws.cell(row=r, column=1, value="TOTAL").font = BOLD
    ws.cell(row=r, column=2, value=len(fronts)).font = BOLD
    r += 2

    _band(ws, r, "By track", 3); r += 1
    for t in dict.fromkeys(f["Track"] for f in fronts):
        ws.cell(row=r, column=1, value=t)
        ws.cell(row=r, column=2, value=sum(1 for f in fronts if f["Track"] == t))
        ws.cell(row=r, column=3,
                value=", ".join(f["id"] for f in fronts
                                if f["Track"] == t)).alignment = WRAP
        r += 1
    r += 1

    _band(ws, r, "Blocked on something that does not exist yet", 3); r += 1
    for f in fronts:
        if f["Status"] == "BLOCKED":
            ws.cell(row=r, column=1, value=f["id"])
            ws.cell(row=r, column=3, value=f"{f['title']} — {f['Gate']}").alignment = WRAP
            r += 1


def write_readme(wb, fronts, register):
    ws = wb.create_sheet("README", 0)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 104
    _band(ws, 1, "Logger — Progress Trackers", 2, font=H1, fill=NAVY)
    rows = [
        ("What this is",
         f"One tab per front — the {len(fronts)} things we are measuring with "
         "the Logger. Each tab carries the front's question, instrument, gate "
         "and standing, plus a progress log to record readings batch by batch."),
        ("Source of truth",
         f"`{register}` in the repo. This workbook is generated from it by "
         "ebayscout/tools/build_goal_trackers.py — the register decides what a "
         "front is, what its gate is, and where it stands. Do not add or "
         "rename tabs by hand; edit the register and rebuild."),
        ("How to use it",
         "After each Logger export, open the fronts you graded and add one "
         "line to the PROGRESS LOG: date, which export, n, the reading, "
         "whether it meets the gate, and the status it moves to. INDEX pulls "
         "the newest line of every log back up."),
        ("When a front settles",
         "Record the closing reading here, then move the verdict into "
         "tested_hypothesis.md and update the register's Status and Standing. "
         "The workbook is the trail; the docs are the record."),
        ("Statuses", " · ".join(STATUS_ORDER)),
        ("Not live-linked",
         "These fronts are graded by joining and pooling Logger exports, not "
         "by reading one column, so nothing here auto-populates from the "
         "Logger. The readings are entered by whoever graded the batch."),
    ]
    r = 3
    for k, v in rows:
        _kv(ws, r, k, v, height=52)
        r += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", default="LOGGER_FRONTS.md")
    ap.add_argument("-o", "--out", default="Logger - Progress Trackers.xlsx")
    args = ap.parse_args()

    fronts = parse_register(args.register)
    seen = {}
    for f in fronts:
        n = tab_name(f)
        if n in seen:
            raise SystemExit(f"tab name collision: {f['id']} and {seen[n]} → {n}")
        seen[n] = f["id"]

    wb = Workbook()
    wb.remove(wb.active)
    write_readme(wb, fronts, args.register)
    write_index(wb, fronts)
    write_rollup(wb, fronts)
    for f in fronts:
        write_front_tab(wb, f)
    wb.save(args.out)
    print(f"{len(fronts)} fronts → {len(wb.sheetnames)} tabs → {args.out}")
    for s in STATUS_ORDER:
        n = sum(1 for f in fronts if f["Status"] == s)
        if n:
            print(f"  {s:<18} {n}")


if __name__ == "__main__":
    main()
