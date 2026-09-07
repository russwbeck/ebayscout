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
import csv
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.hyperlink import Hyperlink

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from ebayscout import match_logging as ml  # noqa: E402


def _cols(header):
    """name -> column letter, resolved from the live schema so a formula can
    never point at a stale column."""
    return {n: get_column_letter(i) for i, n in enumerate(header, start=1)}


M = _cols(ml.MATCH_HEADER)      # match_log
C = _cols(ml.CONFIRM_HEADER)    # confirm_log

# The two raw tabs the operator pastes Logger exports into, plus the derived
# block that unpacks the leaderboard JSON once for everything downstream.
RAW_M, RAW_C, DER = "match_log", "confirm_log", "derived"

# The Logger workbook these two tabs mirror.
LOGGER_KEY = "11BJAJv4tkPKtkrlPVyZqaMpKkpwfuv9t_tnk5WdjPzI"

# The raw tabs are PASTE targets: their grid ends up exactly as tall as the
# data, so every formula uses open-ended column refs (BI2:BI) and is correct at
# any size — and, because the grid is the data, no faster if bounded.  Only
# `derived` needs a tall grid of its own, since its ARRAYFORMULAs must have
# somewhere to spill.
#
# IMPORTRANGE was tried here and does not work: pulling match_log whole (4,020
# rows x 87 columns) returns "Results too large" regardless of destination
# size.  Paste also pools across exports, which a live link cannot do once a
# schema change forces the Logger tab to be recreated.
DERIVED_ROWS = 25000

# derived columns, in order.  ARRAYFORMULA down each so pasting more rows into
# confirm_log extends them with no further action.
DERIVED = [
    ("ts", f'{RAW_C}!{C["ts"]}2:{C["ts"]}'),
    ("source", f'{RAW_C}!{C["source"]}2:{C["source"]}'),
    ("top1_overall", f'VALUE(REGEXEXTRACT({RAW_C}!{C["restricted_top_json"]}2:'
                     f'{C["restricted_top_json"]},"""overall"": ([0-9.]+)"))'),
    ("top2_overall", f'VALUE(REGEXEXTRACT({RAW_C}!{C["restricted_top_json"]}2:'
                     f'{C["restricted_top_json"]},'
                     f'"""overall"":.*?""overall"": ([0-9.]+)"))'),
    ("gap", "C2:C-D2:D"),
    ("top1_phrase", f'REGEXEXTRACT({RAW_C}!{C["restricted_top_json"]}2:'
                    f'{C["restricted_top_json"]},"""phrase"": ""([^""]*)""")'),
    ("top1_year", f'REGEXEXTRACT({RAW_C}!{C["restricted_top_json"]}2:'
                  f'{C["restricted_top_json"]},"""year"": ""(\\d{{4}})""")'),
    # Slogan-aware key: lowercase, strip everything but letters and digits —
    # the sheet-side equivalent of _normalize_key, so "I-O-Wasn't" and
    # "i o wasnt" compare equal.
    ("key_top1", 'REGEXREPLACE(LOWER(F2:F),"[^a-z0-9]","")'),
    ("key_chosen", f'REGEXREPLACE(LOWER({RAW_C}!{C["chosen_phrase"]}2:'
                   f'{C["chosen_phrase"]}),"[^a-z0-9]","")'),
    ("correct", f'(H2:H=I2:I)*(G2:G=TEXT({RAW_C}!{C["chosen_year"]}2:'
                f'{C["chosen_year"]},"0"))=1'),
]

# Every field an entry must carry.  A front missing one fails the build rather
# than producing a tab with a blank target.
FIELDS = ("Track", "Status", "Stage", "Question", "Instrument", "Gate",
          "Standing", "Source")

# The evidence ladder — the one axis that is comparable across every front.
# Stage is about how far the EVIDENCE has got, not how much work is left.
LADDER = [
    "0 · No instrument — nothing logs it yet",
    "1 · Instrumented, zero data",
    "2 · Accruing — below the volume the gate needs",
    "3 · Graded once at volume",
    "4 · Held on a batch it was NOT tuned on (§4.2)",
    "5 · Gate met — shipped, held, or refuted",
    "6 · Closed into tested_hypothesis.md; watching only",
]

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


# --- live readings ----------------------------------------------------------
# One or more formulas per front, evaluated against the pasted Logger tabs.
# Only fronts a formula can genuinely grade appear here; the rest stay manual
# because grading them means pooled offline analysis, not a cell.
#   {m} / {c} / {d} = the match_log / confirm_log / derived tab
def _live():
    m, c, d = RAW_M, RAW_C, DER
    band = (lambda lo, hi: f'=IFERROR(COUNTIFS({d}!C:C,">={lo}",{d}!C:C,"<{hi}",'
                           f'{d}!J:J,TRUE)/COUNTIFS({d}!C:C,">={lo}",'
                           f'{d}!C:C,"<{hi}"),"—")')
    gapband = (lambda lo, hi: f'=IFERROR(COUNTIFS({d}!E:E,">={lo}",{d}!E:E,"<{hi}",'
                              f'{d}!J:J,TRUE)/COUNTIFS({d}!E:E,">={lo}",'
                              f'{d}!E:E,"<{hi}"),"—")')
    rows = f'=COUNTA({m}!{M["ts"]}2:{M["ts"]})'
    crows = f'=COUNTA({c}!{C["ts"]}2:{C["ts"]})'
    share = lambda col, val: (f'=IFERROR(COUNTIF({m}!{col}2:{col},"{val}")'
                              f'/COUNTA({m}!{col}2:{col}),"—")')
    return {
 "A1": [("Rows with a full-res shadow",
         f'=COUNTIF({m}!{M["fullres_top_json"]}2:{M["fullres_top_json"]},"?*")'),
        ("Shadow #1 differs from live #1",
         f'=SUMPRODUCT(({m}!{M["fullres_top_json"]}2:{M["fullres_top_json"]}<>"")*'
         f'(IFERROR(REGEXEXTRACT({m}!{M["fullres_top_json"]}2:'
         f'{M["fullres_top_json"]},"""phrase"": ""([^""]*)"""),"")<>'
         f'IFERROR(REGEXEXTRACT({m}!{M["restricted_top_json"]}2:'
         f'{M["restricted_top_json"]},"""phrase"": ""([^""]*)"""),"")))')],
 "A2": [("Rows with a variant shadow",
         f'=COUNTIF({m}!{M["variant_top_json"]}2:{M["variant_top_json"]},"?*")'),
        ("Variant #1 differs from live #1",
         f'=SUMPRODUCT(({m}!{M["variant_top_json"]}2:{M["variant_top_json"]}<>"")*'
         f'(IFERROR(REGEXEXTRACT({m}!{M["variant_top_json"]}2:'
         f'{M["variant_top_json"]},"""phrase"": ""([^""]*)"""),"")<>'
         f'IFERROR(REGEXEXTRACT({m}!{M["restricted_top_json"]}2:'
         f'{M["restricted_top_json"]},"""phrase"": ""([^""]*)"""),"")))')],
 "A3": [("Confirms scored", f'=COUNT({d}!C:C)'),
        ("≥ 0.90", band("0.90", "1.01")), ("[0.85, 0.90)", band("0.85", "0.90")),
        ("[0.82, 0.85)  ← the band in question", band("0.82", "0.85")),
        ("[0.80, 0.82)", band("0.80", "0.82")), ("[0.75, 0.80)", band("0.75", "0.80")),
        ("[0.70, 0.75)", band("0.70", "0.75"))],
 "A4": [("Confirms scored", f'=COUNT({d}!E:E)'),
        ("≥ 0.20", gapband("0.20", "9")), ("[0.15, 0.20)  ← GAP_ONLY", gapband("0.15", "0.20")),
        ("[0.12, 0.15)", gapband("0.12", "0.15")), ("[0.10, 0.12)", gapband("0.10", "0.12")),
        ("[0.05, 0.10)", gapband("0.05", "0.10")), ("[0.00, 0.05)", gapband("0", "0.05"))],
 "A7": [("Rows with a within-year read",
         f'=COUNTIF({m}!{M["within_year_json"]}2:{M["within_year_json"]},"?*")'),
        ("Median runner-up margin",
         f'=IFERROR(MEDIAN(IFERROR(VALUE(REGEXEXTRACT({m}!'
         f'{M["within_year_json"]}2:{M["within_year_json"]},'
         f'"""runner_up_margin"": ([0-9.eE-]+)")),"")),"—")'),
        ("Margins below 0.01 (the incident read ~0.001)",
         f'=COUNTIF(ARRAYFORMULA(IFERROR(VALUE(REGEXEXTRACT({m}!'
         f'{M["within_year_json"]}2:{M["within_year_json"]},'
         f'"""runner_up_margin"": ([0-9.eE-]+)")),"")),"<0.01")')],
 "A8": [("Mean rendered diameter, px",
         f'=IFERROR(AVERAGE({m}!{M["det_radius_mean"]}2:'
         f'{M["det_radius_mean"]})*2,"—")'),
        ("Crops below the 64px floor",
         f'=COUNTIF({m}!{M["det_radius_mean"]}2:{M["det_radius_mean"]},"<32")')],
 "A10": [("Correction rows logged  ← the whole front",
          f'=COUNTIF({c}!{C["source"]}2:{C["source"]},"correction")'
          f'+COUNTIF({c}!{C["source"]}2:{C["source"]},"skip_correction")'),
         ("Confirms total", crows)],
 "A11": [("Confirms by type — Football share",
          f'=IFERROR(COUNTIF({c}!{C["chosen_type"]}2:{C["chosen_type"]},"Football")'
          f'/COUNTA({c}!{C["chosen_type"]}2:{C["chosen_type"]}),"—")'),
         ("Non-football confirms",
          f'=COUNTA({c}!{C["chosen_type"]}2:{C["chosen_type"]})'
          f'-COUNTIF({c}!{C["chosen_type"]}2:{C["chosen_type"]},"Football")')],
 "A12": [("Centered better than live",
          f'=SUMPRODUCT(({c}!{C["rank_centered"]}2:{C["rank_centered"]}<>"")*'
          f'({c}!{C["rank_centered"]}2:{C["rank_centered"]}<'
          f'{c}!{C["rank_restricted"]}2:{C["rank_restricted"]}))'),
         ("Centered worse",
          f'=SUMPRODUCT(({c}!{C["rank_centered"]}2:{C["rank_centered"]}<>"")*'
          f'({c}!{C["rank_centered"]}2:{C["rank_centered"]}>'
          f'{c}!{C["rank_restricted"]}2:{C["rank_restricted"]}))')],
 "A13": [("Correct #1s won with gap < 0.15  ← the shelf-fill list",
          f'=COUNTIFS({d}!E:E,"<0.15",{d}!J:J,TRUE)')],
 "A16": [("Distinct #1 phrases seen",
          f'=IFERROR(COUNTA(UNIQUE(FILTER({d}!F:F,{d}!F:F<>""))),"—")'),
         ("Wrong #1s (the swap-pair pool)", f'=COUNTIF({d}!J:J,FALSE)')],
 "A23": [("image_only strictly better than live",
          f'=SUMPRODUCT(({c}!{C["rank_image_only"]}2:{C["rank_image_only"]}<>"")*'
          f'({c}!{C["rank_image_only"]}2:{C["rank_image_only"]}<'
          f'{c}!{C["rank_restricted"]}2:{C["rank_restricted"]}))'),
         ("image_only worse",
          f'=SUMPRODUCT(({c}!{C["rank_image_only"]}2:{C["rank_image_only"]}<>"")*'
          f'({c}!{C["rank_image_only"]}2:{C["rank_image_only"]}>'
          f'{c}!{C["rank_restricted"]}2:{C["rank_restricted"]}))')],
 "A24": [("Confirms by source",
          f'=IFERROR(QUERY({c}!{C["source"]}1:{C["source"]},"select {C["source"]}, '
          f'count({C["source"]}) where {C["source"]} is not null group by '
          f'{C["source"]} order by count({C["source"]}) desc '
          f'label count({C["source"]}) \'rows\'",1),"—")')],
 "A25": [("edition_pick rank > 1",
          f'=COUNTIFS({c}!{C["source"]}2:{C["source"]},"edition_pick",'
          f'{c}!{C["rank_restricted"]}2:{C["rank_restricted"]},">1")'),
         ("edition_pick share of confirms",
          f'=IFERROR(COUNTIF({c}!{C["source"]}2:{C["source"]},"edition_pick")'
          f'/COUNTA({c}!{C["source"]}2:{C["source"]}),"—")')],
 "B2": [("Saturated lots (coverage > 0.75)",
         f'=IFERROR(COUNTIF({m}!{M["det_mask_coverage"]}2:{M["det_mask_coverage"]},'
         f'">0.75")/COUNT({m}!{M["det_mask_coverage"]}2:'
         f'{M["det_mask_coverage"]}),"—")'),
        ("Grid fallback on saturated lots",
         f'=COUNTIFS({m}!{M["det_mask_coverage"]}2:{M["det_mask_coverage"]},">0.75",'
         f'{m}!{M["det_detector_used"]}2:{M["det_detector_used"]},"grid")')],
 "B3": [("Fused lots (components < Gemini count)",
         f'=SUMPRODUCT(({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}<>"")*'
         f'({m}!{M["det_mask_components"]}2:{M["det_mask_components"]}<'
         f'{m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}))'),
        ("Of those, DT peaks within ±1 of Gemini  ← the ≥80% gate",
         f'=IFERROR(SUMPRODUCT(({m}!{M["det_mask_components"]}2:{M["det_mask_components"]}<'
         f'{m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]})*'
         f'(ABS({m}!{M["det_dt_peaks_total"]}2:{M["det_dt_peaks_total"]}-'
         f'{m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]})<=1))'
         f'/SUMPRODUCT(({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}<>"")*'
         f'({m}!{M["det_mask_components"]}2:{M["det_mask_components"]}<'
         f'{m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]})),"—")'),
        ("Dense lots (7+ buttons) seen",
         f'=COUNTIF({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]},">=7")')],
 "B4": [("Small lots overcounting unguided",
         f'=SUMPRODUCT(({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}>0)*'
         f'({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}<=3)*'
         f'({m}!{M["ni_selected"]}2:{M["ni_selected"]}>'
         f'{m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}))'),
        ("Mean concentric circles removed",
         f'=IFERROR(AVERAGE({m}!{M["det_overlap_removed"]}2:'
         f'{M["det_overlap_removed"]}),"—")')],
 "B5": [("Lots at gate=auto", share(M["ni_gate"], "auto")),
        ("auto AND scale_first  ← the trusted stratum",
         f'=IFERROR(COUNTIFS({m}!{M["ni_gate"]}2:{M["ni_gate"]},"auto",'
         f'{m}!{M["ni_scale_path"]}2:{M["ni_scale_path"]},"scale_first")'
         f'/COUNTA({m}!{M["ni_gate"]}2:{M["ni_gate"]}),"—")'),
        ("Loophole check — auto on a bailed detector (must be 0)",
         f'=COUNTIFS({m}!{M["ni_gate"]}2:{M["ni_gate"]},"auto",'
         f'{m}!{M["det_detector_used"]}2:{M["det_detector_used"]},"grid")')],
 "B9": [("Lots on a rescue mask path",
         f'=IFERROR(COUNTIF({m}!{M["det_mask_path"]}2:{M["det_mask_path"]},"*+*")'
         f'/COUNTA({m}!{M["det_mask_path"]}2:{M["det_mask_path"]}),"—")'),
        ("Mask path distribution",
         f'=IFERROR(QUERY({m}!{M["det_mask_path"]}1:{M["det_mask_path"]},'
         f'"select {M["det_mask_path"]}, count({M["det_mask_path"]}) where '
         f'{M["det_mask_path"]} is not null group by {M["det_mask_path"]} '
         f'order by count({M["det_mask_path"]}) desc limit 12 '
         f'label count({M["det_mask_path"]}) \'rows\'",1),"—")')],
 "B11": [("Mean mask coverage",
          f'=IFERROR(AVERAGE({m}!{M["det_mask_coverage"]}2:'
          f'{M["det_mask_coverage"]}),"—")'),
         ("Coverage > 0.75",
          f'=COUNTIF({m}!{M["det_mask_coverage"]}2:{M["det_mask_coverage"]},">0.75")')],
 "B14": [("Lots where the swap fired",
          f'=COUNTIF({m}!{M["det_n_swapped"]}2:{M["det_n_swapped"]},">0")'),
         ("Lots with an unbacked circle (the population it exists for)",
          f'=COUNTIF({m}!{M["det_gem_unmatched"]}2:{M["det_gem_unmatched"]},">0")')],
 "B7": [("Unbacked-circle lots  ← the population",
         f'=COUNTIF({m}!{M["det_gem_unmatched"]}2:{M["det_gem_unmatched"]},">0")'),
        ("Swap fired on",
         f'=COUNTIF({m}!{M["det_n_swapped"]}2:{M["det_n_swapped"]},">0")'),
        ("not_a_button confirmations to grade against",
         f'=COUNTIF({c}!{C["source"]}2:{C["source"]},"not_a_button")')],
 "B19": [("scale_first share", share(M["ni_scale_path"], "scale_first")),
         ("Mean scale confidence",
          f'=IFERROR(AVERAGE({m}!{M["ni_scale_conf"]}2:{M["ni_scale_conf"]}),"—")'),
         ("Rows with zero scale confidence",
          f'=COUNTIF({m}!{M["ni_scale_conf"]}2:{M["ni_scale_conf"]},0)')],
 "B20": [("Buttons recovered by the rim pass",
          f'=IFERROR(SUM({m}!{M["det_white_recovered"]}2:'
          f'{M["det_white_recovered"]}),"—")')],
 "B21": [("DT peaks within ±1 of Gemini",
          f'=IFERROR(SUMPRODUCT(({m}!{M["gemini_button_count"]}2:'
          f'{M["gemini_button_count"]}<>"")*'
          f'(ABS({m}!{M["det_dt_peaks_total"]}2:{M["det_dt_peaks_total"]}-'
          f'{m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]})<=1))'
          f'/COUNT({m}!{M["gemini_button_count"]}2:'
          f'{M["gemini_button_count"]}),"—")')],
 "B22": [("Lots with an unbacked Hough circle",
          f'=IFERROR(COUNTIF({m}!{M["det_gem_unmatched"]}2:{M["det_gem_unmatched"]},">0")'
          f'/COUNT({m}!{M["det_gem_unmatched"]}2:{M["det_gem_unmatched"]}),"—")'),
         ("Rows where the match could not run (blank ≠ zero)",
          f'=COUNTBLANK({m}!{M["det_gem_unmatched"]}2:{M["det_gem_unmatched"]})')],
 "B23": [("not_a_button rate",
          f'=IFERROR(COUNTIF({c}!{C["source"]}2:{C["source"]},"not_a_button")'
          f'/COUNTA({c}!{C["source"]}2:{C["source"]}),"—")'),
         ("missed_button rate",
          f'=IFERROR(COUNTIF({c}!{C["source"]}2:{C["source"]},"missed_button")'
          f'/COUNTA({c}!{C["source"]}2:{C["source"]}),"—")')],
 "B25": [("Fused lots by size — 7+ buttons",
          f'=SUMPRODUCT(({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}>=7)*'
          f'({m}!{M["det_mask_components"]}2:{M["det_mask_components"]}<'
          f'{m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}))'),
         ("Fused lots — 1-6 buttons",
          f'=SUMPRODUCT(({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}>0)*'
          f'({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}<7)*'
          f'({m}!{M["det_mask_components"]}2:{M["det_mask_components"]}<'
          f'{m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}))')],
 "B26": [("scale_first share of the feed", share(M["ni_scale_path"], "scale_first")),
         ("Rows", rows)],
 "B27": [("Grid fallback rate", share(M["det_detector_used"], "grid")),
         ("Of grid lots, how many were flooded",
          f'=COUNTIFS({m}!{M["det_detector_used"]}2:{M["det_detector_used"]},"grid",'
          f'{m}!{M["det_mask_coverage"]}2:{M["det_mask_coverage"]},">0.6")')],
 "B28": [("Lots taking the whitepass rescue",
          f'=COUNTIF({m}!{M["det_mask_path"]}2:{M["det_mask_path"]},"*whitepass*")'),
         ("Lots taking a saturation fallback",
          f'=COUNTIF({m}!{M["det_mask_path"]}2:{M["det_mask_path"]},"*satfallback*")')],
 "B29": [("Preprocessing variant distribution",
          f'=IFERROR(QUERY({m}!{M["ni_variant"]}1:{M["ni_variant"]},'
          f'"select {M["ni_variant"]}, count({M["ni_variant"]}) where '
          f'{M["ni_variant"]} is not null group by {M["ni_variant"]} '
          f'label count({M["ni_variant"]}) \'rows\'",1),"—")')],
 "B30": [("Lots where Hough engaged",
          f'=COUNTIF({m}!{M["det_hough_pass1"]}2:{M["det_hough_pass1"]},">0")'),
         ("Small lots (1-3) where it engaged",
          f'=SUMPRODUCT(({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}>0)*'
          f'({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}<=3)*'
          f'({m}!{M["det_hough_pass1"]}2:{M["det_hough_pass1"]}>0))')],
 "C4": [("Confirms by sport",
         f'=IFERROR(QUERY({c}!{C["chosen_type"]}1:{C["chosen_type"]},'
         f'"select {C["chosen_type"]}, count({C["chosen_type"]}) where '
         f'{C["chosen_type"]} is not null group by {C["chosen_type"]} '
         f'label count({C["chosen_type"]}) \'rows\'",1),"—")')],
 "D2": [("Rows carrying a rerank read",
         f'=COUNTIF({c}!{C["rank_rerank"]}2:{C["rank_rerank"]},"?*")'),
        ("Rerank better than live",
         f'=SUMPRODUCT(({c}!{C["rank_rerank"]}2:{C["rank_rerank"]}<>"")*'
         f'({c}!{C["rank_rerank"]}2:{C["rank_rerank"]}<'
         f'{c}!{C["rank_restricted"]}2:{C["rank_restricted"]}))')],
 "E2": [("Gated lots (auto + scale_first)",
         f'=COUNTIFS({m}!{M["ni_gate"]}2:{M["ni_gate"]},"auto",'
         f'{m}!{M["ni_scale_path"]}2:{M["ni_scale_path"]},"scale_first")'),
        ("Of those, unguided count == Gemini  ← the ≥98% gate",
         f'=IFERROR(SUMPRODUCT(({m}!{M["ni_gate"]}2:{M["ni_gate"]}="auto")*'
         f'({m}!{M["ni_scale_path"]}2:{M["ni_scale_path"]}="scale_first")*'
         f'({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}<>"")*'
         f'({m}!{M["ni_selected"]}2:{M["ni_selected"]}='
         f'{m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}))'
         f'/SUMPRODUCT(({m}!{M["ni_gate"]}2:{M["ni_gate"]}="auto")*'
         f'({m}!{M["ni_scale_path"]}2:{M["ni_scale_path"]}="scale_first")*'
         f'({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}<>"")),"—")'),
        ("Disagreements (rollback fires above 2% of any 50)",
         f'=SUMPRODUCT(({m}!{M["ni_gate"]}2:{M["ni_gate"]}="auto")*'
         f'({m}!{M["ni_scale_path"]}2:{M["ni_scale_path"]}="scale_first")*'
         f'({m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}<>"")*'
         f'({m}!{M["ni_selected"]}2:{M["ni_selected"]}<>'
         f'{m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}))')],
 "E3": [("Lots below gate=auto (what Gemini would still be called on)",
         f'=IFERROR(1-COUNTIF({m}!{M["ni_gate"]}2:{M["ni_gate"]},"auto")'
         f'/COUNTA({m}!{M["ni_gate"]}2:{M["ni_gate"]}),"—")')],
 "E4": [("Confirmations accrued  ← the ≥300 gate", crows),
        ("Of those, auto-path",
         f'=COUNTIF({c}!{C["source"]}2:{C["source"]},"*auto*")'),
        ("Corrections logged (precision denominator)",
          f'=COUNTIF({c}!{C["source"]}2:{C["source"]},"correction")')],
    }


LIVE = _live()

# front id -> index into LIVE[id] whose value IS the gate's accrual count.
VOLUME_LIVE = {"A10": 0, "E4": 0, "B3": 0, "B4": 0, "A13": 0}


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
        if fields["Stage"] not in list("0123456"):
            raise SystemExit(f"{fid}: Stage must be 0-6, got {fields['Stage']!r}")
        fields["Stage"] = int(fields["Stage"])
        # "Volume: 100 — auto-confirms with corrections logged"
        vol = fields.get("Volume", "")
        m2 = re.match(r"(\d+)\s*—\s*(.*)", vol)
        fields["VolumeN"] = int(m2.group(1)) if m2 else None
        fields["VolumeOf"] = m2.group(2) if m2 else ""
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
LOG_HEADER_ROW = 18
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
    _kv(ws, 7, "Stage", front["Stage"])
    ws.cell(row=7, column=3, value=f'=REPT("\u2588",B7)&REPT("\u2591",6-B7)')
    ws.cell(row=7, column=4, value=LADDER[front["Stage"]]).font = DIM
    if front["VolumeN"]:
        _kv(ws, 8, "Volume needed", f'{front["VolumeN"]} {front["VolumeOf"]}')
        vol_cell = ws.cell(row=8, column=3)  # filled once log_row is known
    else:
        _kv(ws, 8, "Volume needed", "— the gate names no n")
    _kv(ws, 9, "Instrument", front["Instrument"], height=46)
    _kv(ws, 10, "Gate", front["Gate"], height=60)
    _kv(ws, 11, "Standing", front["Standing"], height=74)
    _kv(ws, 12, "Source", front["Source"], height=30)
    _kv(ws, 13, "Owner", "")
    _kv(ws, 14, "Next action", "")
    ws.cell(row=15, column=2, value=(
        "Bump Stage when the evidence moves; log the reading that moved it "
        "below.")).font = DIM

    live = LIVE.get(front["id"])
    if live:
        _band(ws, 17, "LIVE — recomputed from the pasted Logger tabs", 7)
        for k, (label, formula) in enumerate(live):
            r = 18 + k
            lab = ws.cell(row=r, column=1, value=label)
            lab.alignment = WRAP
            ws.cell(row=r, column=2, value=formula)
        ws.cell(row=18 + len(live), column=1, value=(
            "Pooled over whatever is currently in match_log / confirm_log.")
        ).font = DIM
    else:
        _band(ws, 17, "LIVE — none; this front is graded offline", 7)
        ws.cell(row=18, column=1, value=(
            "No cell formula can grade this one — see Instrument above. Record "
            "the reading from the offline analysis in the log below.")
        ).font = DIM

    log_row = 18 + (len(live) + 2 if live else 2)
    _band(ws, log_row - 1,
          "PROGRESS LOG — one line per Logger export graded against the gate", 7)
    for i, h in enumerate(LOG_COLS, start=1):
        c = ws.cell(row=log_row, column=i, value=h)
        c.font = BOLD
        c.fill = PatternFill("solid", fgColor=LIGHT)
        c.border = BOX
    for k in range(LOG_ROWS):
        ws.cell(row=log_row + 1 + k, column=1).number_format = "yyyy-mm-dd"
    front["_log_row"] = log_row
    if front["VolumeN"]:
        if front["id"] in VOLUME_LIVE and live:
            src = f"B{18 + VOLUME_LIVE[front['id']]}"
            vol_cell.value = f'=IFERROR({src}/{front["VolumeN"]},"")'
            ws.cell(row=8, column=4, value="counts itself — no typing").font = DIM
        else:
            lo, hi = log_row + 1, log_row + LOG_ROWS
            vol_cell.value = (f'=IFERROR(LOOKUP(2,1/(C{lo}:C{hi}<>""),'
                              f'C{lo}:C{hi})/{front["VolumeN"]},"")')
        vol_cell.number_format = "0%"
    ws.freeze_panes = "A4"


IDX_COLS = ["Front", "Title", "Track", "Stage", "Progress", "Toward",
            "Evidence", "Status", "Latest reading", "As of", "Owner",
            "Next action", "Gate"]


def write_index(wb, fronts):
    ws = wb.create_sheet("INDEX")
    _band(ws, 1, f"INDEX — {len(fronts)} fronts", len(IDX_COLS), font=H1, fill=NAVY)
    ws.cell(row=2, column=1, value=(
        "Stage is the evidence ladder (0-6) — the one axis comparable across "
        "all fronts. Bump it on the front's own tab; this pulls it. Evidence "
        "is the latest logged n against the volume the gate names.")).font = DIM

    hr = 6
    # Distribution strip: how many fronts sit on each rung, right at the top.
    ws.cell(row=4, column=1, value="Fronts per stage").font = BOLD
    for st in range(7):
        c = ws.cell(row=4, column=2 + st,
                    value=f'{st}: {sum(1 for f in fronts if f["Stage"] == st)}')
        c.fill = PatternFill("solid", fgColor=LIGHT)
        c.font = BOLD
    ws.cell(row=4, column=10, value=(
        f'{sum(1 for f in fronts if f["Stage"] <= 2)} fronts are below stage 3 '
        f'— not yet graded at volume.')).font = DIM
    for i, h in enumerate(IDX_COLS, start=1):
        c = ws.cell(row=hr, column=i, value=h)
        c.font = WHITE_F
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.border = BOX
    for i, w in enumerate([8, 34, 26, 7, 10, 40, 10, 18, 22, 12, 12, 28, 70],
                          start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for k, f in enumerate(fronts):
        first = f["_log_row"] + 1
        last = f["_log_row"] + LOG_ROWS
        r = hr + 1 + k
        q = f"'{tab_name(f)}'"
        c = ws.cell(row=r, column=1, value=f["id"])
        c.hyperlink = Hyperlink(ref=c.coordinate, location=f"{q}!A1")
        c.font = LINK
        ws.cell(row=r, column=2, value=f["title"])
        ws.cell(row=r, column=3, value=f["Track"])
        # Stage lives on the front tab so bumping it there moves the landing
        # tab; the bar and the ladder text are derived, never typed.
        ws.cell(row=r, column=4, value=f"={q}!B7")
        ws.cell(row=r, column=5,
                value=f'=REPT("\u2588",D{r})&REPT("\u2591",6-D{r})')
        ws.cell(row=r, column=6,
                value=f'=IFERROR(VLOOKUP(D{r},ROLLUP!$E$3:$F$9,2,FALSE),"")'
                ).alignment = TOP
        # Evidence: latest logged n against the volume the gate names.
        ws.cell(row=r, column=7,
                value=(f"={q}!C8" if f["VolumeN"] else "—")
                ).number_format = "0%" if f["VolumeN"] else "General"
        ws.cell(row=r, column=8, value=f["Status"])
        # LOOKUP(2, 1/(range<>""), range) returns the LAST non-empty cell.
        for col, letter in ((9, "D"), (10, "A")):
            rng = f"{q}!{letter}{first}:{letter}{last}"
            ws.cell(row=r, column=col,
                    value=f'=IFERROR(LOOKUP(2,1/({rng}<>""),{rng}),"—")')
        ws.cell(row=r, column=10).number_format = "yyyy-mm-dd"
        ws.cell(row=r, column=11, value=f"={q}!B13")
        ws.cell(row=r, column=12, value=f"={q}!B14")
        ws.cell(row=r, column=13, value=f["Gate"]).alignment = WRAP
    ws.freeze_panes = f"C{hr + 1}"
    ws.auto_filter.ref = (f"A{hr}:{get_column_letter(len(IDX_COLS))}"
                          f"{hr + len(fronts)}")


def write_rollup(wb, fronts):
    """Status and track counts — how the program is distributed, at a glance."""
    ws = wb.create_sheet("ROLLUP", 1)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 76
    _band(ws, 1, "ROLLUP", 6, font=H1, fill=NAVY)
    ws.column_dimensions["E"].width = 8
    ws.column_dimensions["F"].width = 52
    # E3:F9 is the ladder legend INDEX looks up — keep it where it is.
    ws.cell(row=2, column=5, value="Stage").font = BOLD
    ws.cell(row=2, column=6, value="Means").font = BOLD
    for i, text in enumerate(LADDER):
        ws.cell(row=3 + i, column=5, value=i)
        ws.cell(row=3 + i, column=6, value=text).alignment = WRAP

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

    _band(ws, r, "By stage", 3); r += 1
    for st in range(7):
        ws.cell(row=r, column=1, value=LADDER[st])
        ws.cell(row=r, column=2, value=sum(1 for f in fronts if f["Stage"] == st))
        ws.cell(row=r, column=3,
                value=", ".join(f["id"] for f in fronts
                                if f["Stage"] == st)).alignment = WRAP
        r += 1
    r += 1

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


def write_data_tabs(wb):
    """The two tabs the operator pastes Logger exports into, plus the derived
    block that unpacks the leaderboard JSON once for everything downstream."""
    for tab, header in ((RAW_M, ml.MATCH_HEADER), (RAW_C, ml.CONFIRM_HEADER)):
        ws = wb.create_sheet(tab)
        last = get_column_letter(len(header))
        for i, h in enumerate(header, start=1):
            c = ws.cell(row=1, column=i, value=h)
            c.font = WHITE_F
            c.fill = PatternFill("solid", fgColor=NAVY)
        ws.freeze_panes = "A2"
        note = ws.cell(row=1, column=len(header) + 2, value=(
            f"PASTE the Logger's {tab} rows here, starting at A2, under this "
            "header. Easiest route: in the Logger, right-click the tab → Copy "
            "to → Existing spreadsheet → this file, then delete THIS tab and "
            f"rename the copy to '{tab}'. To pool across exports instead, "
            "paste values and append each new export below the last. Every "
            "formula reads whole columns, so either works at any size."))
        note.alignment = WRAP
        note.font = DIM
        ws.cell(row=2, column=len(header) + 2,
                value='=COUNTA(A2:A)&" rows"')

    ws = wb.create_sheet(DER)
    ws.cell(row=1, column=1, value=(
        "Derived from confirm_log — the leaderboard JSON unpacked once so "
        "every front can read it. ARRAYFORMULA, so it extends itself as rows "
        "are pasted. Do not type here.")).font = DIM
    for i, (name, expr) in enumerate(DERIVED, start=1):
        L = get_column_letter(i)
        ws.column_dimensions[L].width = 22
        c = ws.cell(row=2, column=i, value=name)
        c.font = WHITE_F
        c.fill = PatternFill("solid", fgColor=NAVY)
        # Column A anchors the block; the rest key off confirm_log being filled.
        ws.cell(row=3, column=i, value=(
            f'=ARRAYFORMULA(IF({RAW_C}!{C["ts"]}2:{C["ts"]}="","",'
            f'IFERROR({expr},"")))'))
    # derived is the one tab that needs headroom: its ARRAYFORMULAs spill.
    ws.cell(row=DERIVED_ROWS, column=len(DERIVED), value="")
    ws.freeze_panes = "A3"


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
         "whether it meets the gate, and the status it moves to. Then bump "
         "Stage (B7) if the evidence moved. INDEX pulls the newest line of "
         "every log back up, alongside the stage bar and — where the gate "
         "names a required n — the latest n as a percentage of it."),
        ("When a front settles",
         "Record the closing reading here, then move the verdict into "
         "tested_hypothesis.md and update the register's Status and Standing. "
         "The workbook is the trail; the docs are the record."),
        ("Stage — the progress axis",
         "0-6 on the evidence ladder, the same scale for every front so they "
         "compare: " + " | ".join(LADDER) + ". Stage 3 to 4 is the §4.2 rule "
         "(a result tuned on its own pool has not been tested) and is where "
         "most fronts stall."),
        ("Where to bump it",
         "On the front's own tab, cell B7. INDEX pulls it, draws the bar, and "
         "looks up the label — never type a stage on INDEX."),
        ("Statuses", " · ".join(STATUS_ORDER)),
        ("Getting the data in",
         "Copy the Logger's two tabs into this file: in the Logger, "
         "right-click match_log → Copy to → Existing spreadsheet → this one, "
         "then delete the empty match_log tab here and rename the copy to "
         "match_log. Repeat for confirm_log. To pool across exports instead, "
         "paste values under the header and append each new export below the "
         "last. Formulas read whole columns, so both work at any size."),
        ("Why not IMPORTRANGE",
         "Tried and rejected: pulling match_log whole (4,020 rows x 87 "
         "columns) returns 'Results too large' whatever the destination size. "
         "A live link also cannot pool across exports, which is needed once a "
         "schema change forces the Logger tab to be recreated."),
        ("derived",
         "Unpacks confirm_log's restricted_top_json once — #1's overall, the "
         "#1-to-#2 gap, #1's phrase and year, and a slogan-aware correctness "
         "flag (lowercased, non-alphanumerics stripped: the sheet-side "
         "_normalize_key). Every band front reads it. ARRAYFORMULA, so it "
         "extends itself. Do not type in it."),
        ("What is live vs typed",
         "38 of the 69 fronts have a LIVE block that recomputes on every "
         "paste. The other 31 have no cell formula that can grade them — the "
         "instrument is a Cloud Run stdout line, a GCS sidecar, or an "
         "operator decision — and their readings are typed into the progress "
         "log from the offline analysis."),
    ]
    r = 3
    for k, v in rows:
        _kv(ws, r, k, v, height=52)
        r += 1


CSV_COLS = ["Front", "Title", "Track", "Stage", "Progress", "Toward",
            "Status", "Volume needed", "Question", "Instrument", "Gate",
            "Standing", "Source", "Owner", "Next action"]


def emit_csv(fronts, path):
    """Flat one-row-per-front dump, for building the landing tab in Sheets by
    hand (File > Import, or just paste).  Same fields the workbook uses."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLS)
        for f in fronts:
            st = f["Stage"]
            w.writerow([
                f["id"], f["title"], f["Track"], st,
                "\u2588" * st + "\u2591" * (6 - st), LADDER[st], f["Status"],
                (f'{f["VolumeN"]} {f["VolumeOf"]}' if f["VolumeN"] else ""),
                f["Question"], f["Instrument"], f["Gate"], f["Standing"],
                f["Source"], "", "",
            ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", default="LOGGER_FRONTS.md")
    ap.add_argument("--csv", help="also write a flat one-row-per-front CSV")
    ap.add_argument("-o", "--out", default="Logger - Progress Trackers.xlsx")
    args = ap.parse_args()

    fronts = parse_register(args.register)
    seen = {}
    for f in fronts:
        n = tab_name(f)
        if n in seen:
            raise SystemExit(f"tab name collision: {f['id']} and {seen[n]} → {n}")
        seen[n] = f["id"]

    if args.csv:
        emit_csv(fronts, args.csv)
        print(f"{len(fronts)} fronts → {args.csv}")

    wb = Workbook()
    wb.remove(wb.active)
    write_readme(wb, fronts, args.register)
    write_index_placeholder = None
    write_rollup(wb, fronts)
    write_data_tabs(wb)
    for f in fronts:
        write_front_tab(wb, f)
    # INDEX last: it needs each front's log row, which write_front_tab sets.
    write_index(wb, fronts)
    wb.move_sheet("INDEX", offset=-(len(wb.sheetnames) - 1))
    wb.save(args.out)
    print(f"{len(fronts)} fronts → {len(wb.sheetnames)} tabs → {args.out}")
    for s in STATUS_ORDER:
        n = sum(1 for f in fronts if f["Status"] == s)
        if n:
            print(f"  {s:<18} {n}")


if __name__ == "__main__":
    main()
