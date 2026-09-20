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
import hashlib
import json
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
# derived columns, in order.  Each one is computed straight from confirm_log —
# never from another derived column.  Two reasons: derived's data starts one
# row lower than confirm_log's (header offset), and its grid is far taller, so
# an internal reference would be both off by one and a length mismatch inside
# the ARRAYFORMULA.  Verbose, but every column is the same length as its input.
def _derived():
    j = f'{RAW_C}!{C["restricted_top_json"]}2:{C["restricted_top_json"]}'
    top1 = f'VALUE(REGEXEXTRACT({j},"""overall"": ([0-9.]+)"))'
    top2 = f'VALUE(REGEXEXTRACT({j},"""overall"":.*?""overall"": ([0-9.]+)"))'
    phrase = f'REGEXEXTRACT({j},"""phrase"": ""([^""]*)""")'
    year = f'REGEXEXTRACT({j},"""year"": ""(\\d{{4}})""")'
    # The sheet-side _normalize_key: lowercase, drop everything but a-z0-9.
    key1 = f'REGEXREPLACE(LOWER({phrase}),"[^a-z0-9]","")'
    keyc = (f'REGEXREPLACE(LOWER({RAW_C}!{C["chosen_phrase"]}2:'
            f'{C["chosen_phrase"]}),"[^a-z0-9]","")')
    chosen_year = f'TEXT({RAW_C}!{C["chosen_year"]}2:{C["chosen_year"]},"0")'
    return [
        ("ts", f'{RAW_C}!{C["ts"]}2:{C["ts"]}'),
        ("source", f'{RAW_C}!{C["source"]}2:{C["source"]}'),
        ("top1_overall", top1),
        ("top2_overall", top2),
        ("gap", f'{top1}-{top2}'),
        ("top1_phrase", phrase),
        ("top1_year", year),
        ("key_top1", key1),
        ("key_chosen", keyc),
        ("correct", f'({key1}={keyc})*({year}={chosen_year})=1'),
    ]


DERIVED = _derived()


def derived_formula():
    """All ten columns as ONE array formula in A3.

    Ten separate ARRAYFORMULAs meant ten chances for a paste to go wrong, and
    the .xlsx import mangles every one of them.  {a,b,c} joins them
    horizontally, so the whole block is a single cell to paste and a single
    cell to check."""
    cols = ",".join(f'IFERROR({expr},"")' for _, expr in DERIVED)
    return "=ARRAYFORMULA({" + cols + "})"

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

    # --- three corrections, applied throughout (2026-09-07 log review) -------
    #
    # 1. A JSON column is NOT blank when its shadow didn't run — it holds the
    #    literal "[]" or "{}".  COUNTIF(col,"?*") matches any text, so it
    #    counted every row: A1 read "4170 rows with a full-res shadow" against
    #    310 real ones.  `populated` excludes the empty markers.
    #
    # 2. A blank rank compares as 0, which is less than every real rank, so
    #    `rank_x < rank_restricted` scored "better" on every row where
    #    rank_restricted never got written.  A12 read 113 improvements against
    #    29, A23 152 against 70.  `beats` requires BOTH ranks present.
    #
    # 3. Detection facts are per-IMAGE but match_log has one row per CROP, so
    #    any det_*/ni_* count is weighted by lot size — an 80-button sheet
    #    counts 80 times.  B27 read grid fallback at 27.3% against 22.6%
    #    per image; E2 read "1349 gated lots" against 215.  `per_image` pins
    #    the count to crop_num = 1.
    CROP1 = f'{m}!{M["crop_num"]}2:{M["crop_num"]},1'
    CROPR = f'{m}!{M["crop_num"]}2:{M["crop_num"]}'

    def populated(tab, col):
        """Rows where a JSON column actually carries a leaderboard."""
        r = f'{tab}!{col}2:{col}'
        return f'=SUMPRODUCT(({r}<>"")*({r}<>"[]")*({r}<>"{{}}"))'

    def beats(col_a, col_b, op):
        """Rows where BOTH ranks are present and col_a `op` col_b."""
        a, b = f'{c}!{col_a}2:{col_a}', f'{c}!{col_b}2:{col_b}'
        return f'=SUMPRODUCT(({a}<>"")*({b}<>"")*({a}{op}{b}))'

    def per_image(*conds):
        """COUNTIFS over IMAGES, not crops."""
        return f'=COUNTIFS({CROP1},' + ",".join(conds) + ")"

    def dist(tab, col, limit=12, per_image=False):
        """A value distribution in ONE cell.

        A grouped QUERY returns a row per value, and a LIVE block budgets one
        row, so the spill has nowhere to go.  It failed two different ways at
        once: A24 and B9 were refused outright and rendered `#REF!` for the
        whole life of the workbook, while B29 — which happened to have few
        enough variants to fit — SPILLED, writing its counts down column C
        across the "Pooled over…" note and into the PROGRESS LOG's `n`
        column.  One more distinct variant and it would have flipped to
        `#REF!` like the others.

        Same lesson as C4, but these three fronts want the distribution
        itself, not a scalar, so collapse it to a string one cell can hold.
        TEXTJOIN consumes the array instead of spilling it, which is what
        makes this structurally safe rather than merely currently-fitting.
        """
        r = f'{tab}!{col}2:{col}'
        if per_image:
            # Mask path and preprocessing variant are facts about the PHOTO,
            # so an 80-button sheet must not vote 80 times — the same pin the
            # rest of the detection cells carry.
            keys = f'UNIQUE(FILTER({r},{r}<>"",{CROPR}=1))'
            cnt = f'ARRAYFORMULA(COUNTIFS({CROPR},1,{r},k))'
        else:
            keys = f'UNIQUE(FILTER({r},{r}<>""))'
            cnt = f'ARRAYFORMULA(COUNTIF({r},k))'
        return (f'=IFERROR(LET(k,{keys},n,{cnt},'
                f't,ARRAY_CONSTRAIN(SORT({{k,n}},2,FALSE),{limit},2),'
                f'TEXTJOIN(" · ",TRUE,'
                f'ARRAYFORMULA(INDEX(t,,1)&" "&INDEX(t,,2)))),"—")')

    def typed(tab, col):
        """Cells carrying actual text.

        `COUNTA` counts a pasted empty string as data: `chosen_type` is blank
        on the 478 confirmations that never resolved a type, and COUNTA scored
        all of them — A11 read 483 non-football confirms against 5.  `"?*"`
        matches text of length >= 1, so an empty paste does not count.
        """
        return f'COUNTIF({tab}!{col}2:{col},"?*")'

    images = f'=COUNTIF({CROP1})'
    # Bare (no leading '=') fragments, for composing into larger formulas.
    _images = f'COUNTIF({CROP1})'
    _crop1c = f'{m}!{M["crop_num"]}2:{M["crop_num"]}=1'

    def numeric(rng, *extra):
        """Images where `rng` holds a NUMBER.

        `COUNTIFS(..., "<>")` does NOT mean this.  A pasted export writes a
        zero-length STRING into an empty field, and a zero-length string is
        not blank, so the criterion counts it as present.  B22's "lots where
        the match could not run (blank != zero)" read 0 against 157 that way --
        the one cell whose entire caption is about blanks -- and E2's gate read
        74.4% against 79.6% because its denominator counted 215 lots instead of
        the 201 that carry a Gemini count.  ISNUMBER is the only test that
        separates a written number from an empty paste.
        """
        return (f'SUMPRODUCT(({_crop1c})*ISNUMBER({rng})'
                + ''.join(f'*({e})' for e in extra) + ')')

    # The same trap as `numeric`, on the other side of the comparison.  A
    # pasted empty field is a zero-length STRING, and Sheets ranks text above
    # every number, so `gemini_button_count >= 7` is TRUE on every row that
    # never got a count.  A comparison with a bound above it (`>0` AND `<7`)
    # is already safe because text fails the upper half; one with nothing
    # above it needs this guard.
    gnum = f'ISNUMBER({m}!{M["gemini_button_count"]}2:'\
           f'{M["gemini_button_count"]})'
    _comp = f'{m}!{M["det_mask_components"]}2:{M["det_mask_components"]}'
    _dt = f'{m}!{M["det_dt_peaks_total"]}2:{M["det_dt_peaks_total"]}'
    _hough = f'{m}!{M["det_hough_pass1"]}2:{M["det_hough_pass1"]}'

    _gem = f'{m}!{M["det_gem_unmatched"]}2:{M["det_gem_unmatched"]}'
    _gem_lots = f'COUNTIFS({CROP1},{_gem},">0")'
    _gem_scored = numeric(_gem)

    # The Stage-B gated stratum, per image.  `ni_gate=auto` is always
    # `scale_first` in practice, but both are named so the gate stays explicit.
    _gate = f'{m}!{M["ni_gate"]}2:{M["ni_gate"]}'
    _path = f'{m}!{M["ni_scale_path"]}2:{M["ni_scale_path"]}'
    _gcount = f'{m}!{M["gemini_button_count"]}2:{M["gemini_button_count"]}'
    _nisel = f'{m}!{M["ni_selected"]}2:{M["ni_selected"]}'
    _gate_auto = f'COUNTIFS({CROP1},{_gate},"auto")'
    _gated = f'COUNTIFS({CROP1},{_gate},"auto",{_path},"scale_first")'
    _gated_scored = numeric(_gcount, f'{_gate}="auto"', f'{_path}="scale_first"')
    # Numerator and denominator have to use ONE guard.  These two counted
    # `<>""` while `_gated_scored` below counts ISNUMBER, so the ratio mixed
    # two definitions of "scored" — a row carrying text in the count column
    # could reach the numerator and not the denominator.  It did not bite on
    # the 2026-09-20 pool (237/302 reconciles exactly against the strata),
    # but it is the same text-ranks-above-numbers trap as the rest, and half
    # a ratio is the worst place to keep it.
    _agree = (f'SUMPRODUCT(({_crop1c})*({_gate}="auto")*({_path}="scale_first")'
              f'*{gnum}*({_nisel}={_gcount}))')
    _agree1 = (f'SUMPRODUCT(({_crop1c})*({_gate}="auto")*({_path}="scale_first")'
               f'*{gnum}*(ABS({_nisel}-{_gcount})<=1))')

    # The gated stratum, split by lot shape.  E2's ≥98% gate has sat at ~78%
    # while the corpus turned out to hold two different failure regimes: on
    # 2026-09-20, 43% of dense lots are fused against 5.3% of small ones, the
    # sub-64px crops are all in dense lots, and scale confidence fails on
    # SMALL lots instead.  A gate pooled across both can be missed forever by
    # the harder half while the easier half is already shippable, so measure
    # them apart before concluding Stage B is blocked.  Same axis C7's gate
    # already names ("the rate, split by lot shape").
    _small = f'({_gcount}>=1)*({_gcount}<=6)'
    _dense = f'({_gcount}>=7)'

    # count_source, and the shape of what the auto path proposed.  Both are
    # per-lot, so both ride the crop_num=1 row like the strata above.
    _cs = f'{m}!{M["count_source"]}2:{M["count_source"]}'
    _ni_small = f'({_nisel}>=1)*({_nisel}<=6)'
    _ni_dense = f'({_nisel}>=7)'

    def _override(shape):
        """auto_overridden / (auto + auto_overridden), within one lot shape."""
        base = f'({_crop1c})*{shape}'
        return (f'=IFERROR(SUMPRODUCT({base}*({_cs}="auto_overridden"))'
                f'/SUMPRODUCT({base}*(({_cs}="auto")+({_cs}="auto_overridden"))),'
                f'"—")')

    def _strat(cond, agree=False):
        base = (f'({_crop1c})*({_gate}="auto")*({_path}="scale_first")'
                f'*{gnum}*{cond}')
        if not agree:
            return f'=SUMPRODUCT({base})'
        return (f'=IFERROR(SUMPRODUCT({base}*({_nisel}={_gcount}))'
                f'/SUMPRODUCT({base}),"—")')

    # Bands: `src` restricts to human-confirmed rows.  gemini_auto fires only
    # when CLIP already agreed with Gemini, so grading a band against those
    # rows asks the board whether it agrees with itself — and they are ~75% of
    # confirmations.  Every machine source is `gemini*` or `auto*`; excluding
    # both leaves the rows a person actually decided.
    # `audit_*` rows are SR-05's sample bookkeeping, not a human tap: the
    # shadow row carries what the auto would have written and the hit/miss row
    # annotates a confirm that already logged its own row.  Counted as human, a
    # sampled crop would land in these bands up to three times.
    HUMAN = (f'{d}!B:B,"<>gemini*",{d}!B:B,"<>auto*",'
             f'{d}!B:B,"<>audit_*"')

    def _band(col, lo, hi, human=False):
        src = f",{HUMAN}" if human else ""
        return (f'=IFERROR(COUNTIFS({d}!{col}:{col},">={lo}",'
                f'{d}!{col}:{col},"<{hi}"{src},{d}!J:J,TRUE)'
                f'/COUNTIFS({d}!{col}:{col},">={lo}",'
                f'{d}!{col}:{col},"<{hi}"{src}),"—")')

    def _band_n(col, lo, hi, human=False):
        src = f",{HUMAN}" if human else ""
        return (f'=COUNTIFS({d}!{col}:{col},">={lo}",'
                f'{d}!{col}:{col},"<{hi}"{src})')

    band = lambda lo, hi: _band("C", lo, hi)
    hband = lambda lo, hi: _band("C", lo, hi, human=True)
    hband_n = lambda lo, hi: _band_n("C", lo, hi, human=True)
    gapband = lambda lo, hi: _band("E", lo, hi)
    hgapband = lambda lo, hi: _band("E", lo, hi, human=True)
    hgapband_n = lambda lo, hi: _band_n("E", lo, hi, human=True)

    # Confirmations a person or the auto path actually made.  `gemini_count`
    # rows are bookkeeping for a Gemini count, not a decision, and B23 already
    # excludes them from its denominator for the same reason.
    # Rows in confirm_log that are NOT a confirmation anyone or anything made.
    # `gemini_count` is bookkeeping for a Gemini count; `audit_shadow` /
    # `audit_hit` / `audit_miss` are SR-05's sample bookkeeping, and a sampled
    # crop writes up to three of them around the ONE confirm the operator
    # actually made.  Any cell whose denominator says "confirmations" has to
    # take all of them out, or turning the sampler on inflates the very gate it
    # exists to inform.
    bookkeeping = (f'(COUNTIF({c}!{C["source"]}2:{C["source"]},"gemini_count")'
                   f'+COUNTIF({c}!{C["source"]}2:{C["source"]},"audit_*"))')

    real_confirms = f'={typed(c, C["source"])}-{bookkeeping}'

    rows = f'=COUNTA({m}!{M["ts"]}2:{M["ts"]})'
    crows = f'=COUNTA({c}!{C["ts"]}2:{C["ts"]})'
    share = lambda col, val: (
        f'=IFERROR(COUNTIFS({CROP1},{m}!{col}2:{col},"{val}")'
        f'/COUNTIF({CROP1}),"—")')
    return {
 "A1": [("Rows with a full-res shadow",
         populated(m, M["fullres_top_json"])),
        ("Shadow #1 differs from live #1",
         f'=SUMPRODUCT(({m}!{M["fullres_top_json"]}2:{M["fullres_top_json"]}<>"")*'
         f'({m}!{M["fullres_top_json"]}2:{M["fullres_top_json"]}<>"[]")*'
         f'(IFERROR(REGEXEXTRACT({m}!{M["fullres_top_json"]}2:'
         f'{M["fullres_top_json"]},"""phrase"": ""([^""]*)"""),"")<>'
         f'IFERROR(REGEXEXTRACT({m}!{M["restricted_top_json"]}2:'
         f'{M["restricted_top_json"]},"""phrase"": ""([^""]*)"""),"")))')],
 "A2": [("Rows with a variant shadow",
         populated(m, M["variant_top_json"])),
        ("Variant #1 differs from live #1",
         f'=SUMPRODUCT(({m}!{M["variant_top_json"]}2:{M["variant_top_json"]}<>"")*'
         f'({m}!{M["variant_top_json"]}2:{M["variant_top_json"]}<>"[]")*'
         f'(IFERROR(REGEXEXTRACT({m}!{M["variant_top_json"]}2:'
         f'{M["variant_top_json"]},"""phrase"": ""([^""]*)"""),"")<>'
         f'IFERROR(REGEXEXTRACT({m}!{M["restricted_top_json"]}2:'
         f'{M["restricted_top_json"]},"""phrase"": ""([^""]*)"""),"")))')],
 # 7 rows each, matching the built workbook's LIVE block exactly — the
 # repair script writes in place, and anything longer would overwrite the
 # PROGRESS LOG below it.  The bands are graded HUMAN-CONFIRMED: gemini_auto
 # fires only when CLIP already agreed with Gemini, so a pooled band asks the
 # board whether it agrees with itself.  Pooled [0.82,0.85) reads 97.4%,
 # human 90.3% — the second is the one a threshold move must answer to.
 "A3": [("Human-confirmed confirms scored", hband_n("0", "1.01")),
        ("≥ 0.90 (human)", hband("0.90", "1.01")),
        ("[0.85, 0.90) (human)", hband("0.85", "0.90")),
        ("[0.82, 0.85) (human)  ← the band in question", hband("0.82", "0.85")),
        ("[0.80, 0.82) (human)", hband("0.80", "0.82")),
        ("[0.75, 0.80) (human)", hband("0.75", "0.80")),
        ("[0.70, 0.75) (human)", hband("0.70", "0.75"))],
 "A4": [("Human-confirmed confirms scored", hgapband_n("0", "9")),
        ("≥ 0.20 (human)", hgapband("0.20", "9")),
        ("[0.15, 0.20) (human)  ← GAP_ONLY", hgapband("0.15", "0.20")),
        ("[0.12, 0.15) (human)", hgapband("0.12", "0.15")),
        ("[0.10, 0.12) (human)", hgapband("0.10", "0.12")),
        ("[0.05, 0.10) (human)", hgapband("0.05", "0.10")),
        ("[0.00, 0.05) (human)", hgapband("0", "0.05"))],
 "A7": [("Rows with a within-year read",
         populated(m, M["within_year_json"])),
        # Read "—" since the build: without ARRAYFORMULA, REGEXEXTRACT
        # evaluates the FIRST cell only, so MEDIAN got one value or an
        # error, never the distribution.  The row below it already wrapped
        # correctly and always worked — which is why only this one was
        # blank.  MEDIAN ignores the "" that IFERROR leaves on a miss.
        ("Median runner-up margin",
         f'=IFERROR(MEDIAN(ARRAYFORMULA(IFERROR(VALUE(REGEXEXTRACT({m}!'
         f'{M["within_year_json"]}2:{M["within_year_json"]},'
         f'"""runner_up_margin"": ([0-9.eE-]+)")),""))),"—")'),
        ("Margins below 0.01 (the incident read ~0.001)",
         f'=COUNTIF(ARRAYFORMULA(IFERROR(VALUE(REGEXEXTRACT({m}!'
         f'{M["within_year_json"]}2:{M["within_year_json"]},'
         f'"""runner_up_margin"": ([0-9.eE-]+)")),"")),"<0.01")')],
 # `det_radius_mean` is a per-PHOTO aggregate, so averaging it over crop rows
 # weights each lot by its button count — and a dense lot's buttons are
 # smaller, so the mean was pulled down by exactly the lots that drag it.
 #
 # The second cell is a different problem and is NOT fixed by a crop pin.
 # A8's shipped rule acts on ONE CROP ("when rendered button diameter is
 # below ~64px, downgrade gemini_auto"), but `MATCH_HEADER` carries no
 # per-crop radius — `det_radius_min/max/mean/std` are all lot-level.  So the
 # honest reading is the blast radius the guard would touch, under a lot-level
 # proxy, and the caption has to say so rather than implying each crop was
 # measured.  Instrumenting a per-crop radius would settle it properly.
 "A8": [("Mean rendered diameter, px (per image)",
         f'=IFERROR(AVERAGEIFS({m}!{M["det_radius_mean"]}2:'
         f'{M["det_radius_mean"]},{CROP1})*2,"—")'),
        ("Crops in lots whose MEAN diameter is < 64px  ← lot-level proxy",
         f'=COUNTIF({m}!{M["det_radius_mean"]}2:{M["det_radius_mean"]},"<32")')],
 # The first two cells read the VOLUNTEERED half: corrections the operator
 # chose to report.  That rate is biased by the thing it measures — a wrong
 # auto is only reported if it is noticed, and an auto nobody eyeballs is
 # never noticed — so it is a floor on the error rate, not a measurement of
 # it.  The last two read the SAMPLED half (SR-05, 2026-09-20): a hash picks
 # 1 lot in N, every auto on a picked lot is put to the operator, and the
 # answer is graded against what the auto would have written.  That one is
 # unbiased WITHIN ITS POPULATION, and its population is truncated: lots of
 # 15+ buttons are never picked, because handing back a 100-button lot costs
 # 100 clicks.  So this is SMALL-LOT auto precision.  Dense-lot autos are not
 # in it and E4's gate spans both shapes — do not close E4 on this cell alone.
 "A10": [("Correction rows logged  ← volunteered, a floor not a rate",
          f'=COUNTIF({c}!{C["source"]}2:{C["source"]},"correction")'
          f'+COUNTIF({c}!{C["source"]}2:{C["source"]},"skip_correction")'),
         # Same population as E4's gate — see there.
         ("Confirms total (no bookkeeping)", real_confirms),
         ("Autos put to the operator  ← sampled, 1-in-N, lots under 15",
          f'=COUNTIF({c}!{C["source"]}2:{C["source"]},"audit_shadow")'),
         # Denominator is answered crops only, NOT audit_shadow: a sampled lot
         # the operator walked away from leaves shadows with no verdict, and
         # counting those as misses would read abandonment as imprecision.
         # The gap between this pair and the cell above is how much of the
         # sample went unanswered, which is worth seeing.
         ("Sampled auto precision  ← the unbiased read",
          f'=IFERROR(COUNTIF({c}!{C["source"]}2:{C["source"]},"audit_hit")'
          f'/(COUNTIF({c}!{C["source"]}2:{C["source"]},"audit_hit")'
          f'+COUNTIF({c}!{C["source"]}2:{C["source"]},"audit_miss")),"—")')],
 # Both cells divided by COUNTA, which counts the blank `chosen_type` a
 # confirmation writes when it resolved no type: the share read 0.804 against
 # 0.997, and "non-football confirms" read 483 against 5.  `typed` counts only
 # cells carrying a sport.
 "A11": [("Football share of TYPED confirms",
          f'=IFERROR(COUNTIF({c}!{C["chosen_type"]}2:{C["chosen_type"]},"Football")'
          f'/{typed(c, C["chosen_type"])},"—")'),
         ("Non-football confirms  ← the cross-sport surface",
          f'={typed(c, C["chosen_type"])}'
          f'-COUNTIF({c}!{C["chosen_type"]}2:{C["chosen_type"]},"Football")')],
 "A12": [("Centered better than live",
          beats(C["rank_centered"], C["rank_restricted"], "<")),
         ("Centered worse",
          beats(C["rank_centered"], C["rank_restricted"], ">"))],
 "A13": [("Correct #1s won with gap < 0.15  ← the shelf-fill list",
          f'=COUNTIFS({d}!E:E,"<0.15",{d}!J:J,TRUE)')],
 # The wrong-#1 pool counted every `derived` row whose `correct` flag is
 # FALSE, and a `gemini_count` row is bookkeeping, not a confirmation
 # somebody made: it carries no `chosen_phrase`, so if it reaches the flag at
 # all it reaches it as FALSE.  Whether it does depends on whether those rows
 # carry a `restricted_top_json` — if they do not, the regex errors and the
 # flag is "" rather than FALSE, and this filter is a harmless no-op.  Either
 # way the cell is right afterwards, and the number says which it was: 820
 # before, so a drop means the pool was inflated by bookkeeping and a hold
 # means it never was.  Same exclusion B23 already applies to its denominator.
 "A16": [("Distinct #1 phrases seen",
          f'=IFERROR(COUNTA(UNIQUE(FILTER({d}!F:F,{d}!F:F<>""))),"—")'),
         ("Wrong #1s (the swap-pair pool, no bookkeeping)",
          f'=COUNTIFS({d}!J:J,FALSE,{d}!B:B,"<>gemini_count",'
          f'{d}!B:B,"<>audit_*")')],
 # The front is about TYPED rows, so the population comes first — the
 # all-confirms count read 152 against 5 typed rows that actually qualify.
 "A23": [("Typed rows carrying both ranks  ← the actual population",
          f'=SUMPRODUCT(({c}!{C["source"]}2:{C["source"]}="typed_search")'
          f'*ISNUMBER({c}!{C["rank_image_only"]}2:{C["rank_image_only"]})'
          f'*ISNUMBER({c}!{C["rank_restricted"]}2:{C["rank_restricted"]}))'),
         ("image_only better than live (all confirms)",
          beats(C["rank_image_only"], C["rank_restricted"], "<"))],
 # Was a grouped QUERY in a one-row block: `#REF!` since the workbook was
 # built, so the front whose whole question is "what do the picker clicks
 # cost?" has never shown a single number.  See `dist`.
 "A24": [("Confirms by source  ← the click economics",
          dist(c, C["source"], limit=14))],
 "A25": [("edition_pick rank > 1",
          f'=COUNTIFS({c}!{C["source"]}2:{C["source"]},"edition_pick",'
          f'{c}!{C["rank_restricted"]}2:{C["rank_restricted"]},">1")'),
         # Denominator was a raw COUNTA of confirm_log, which already counted
         # `gemini_count` bookkeeping; the audit sample would have added three
         # more row types to it.  It says "of confirms", so it counts confirms.
         ("edition_pick share of confirms",
          f'=IFERROR(COUNTIF({c}!{C["source"]}2:{C["source"]},"edition_pick")'
          f'/({typed(c, C["source"])}-{bookkeeping}),"—")')],
 # Saturation is a property of the IMAGE — per crop it read 162 saturated rows
 # and 160 grid-fallback rows against 39 lots and 37.  Note what the column
 # holds: `det_mask_coverage` is the FINAL adopted mask, so a lot the
 # `+satfallback_*` chooser rescued records its post-rescue coverage and is no
 # longer counted here.  Coverage > 0.75 is therefore the UNRESCUED residual —
 # the lots where neither variant was plausible — not the saturation rate.
 "B2": [("Unrescued saturated lots (coverage > 0.75, per image)",
         f'=IFERROR(COUNTIFS({CROP1},{m}!{M["det_mask_coverage"]}2:'
         f'{M["det_mask_coverage"]},">0.75")/COUNTIF({CROP1}),"—")'),
        ("Of those, how many fell to the grid  ← the gate",
         per_image(f'{m}!{M["det_mask_coverage"]}2:{M["det_mask_coverage"]},">0.75"',
                   f'{m}!{M["det_detector_used"]}2:{M["det_detector_used"]},"grid"'))],
 # Every cell here counted CROPS while the gate counts LOTS ("~100 pipeline
 # fused lots, incl. ~20 with 7+ buttons") — and a fused lot is a dense lot
 # by definition, so the denser the lot the more times it voted.  Fused read
 # 3601 and dense 5774 against a 100-lot gate, which is what put 3601% in
 # INDEX's Evidence column.  Same pin as B2/B4/B27/E2.
 "B3": [("Fused lots (components < Gemini count, per image)",
         f'=SUMPRODUCT(({_crop1c})*({_gcount}<>"")*'
         f'({_comp}<{_gcount}))'),
        # The numerator needs {gnum} in its own right: without it a row whose
        # Gemini count never landed counts as fused, because a component
        # count is a number and Sheets ranks every number below text.
        ("Of those, DT peaks within ±1 of Gemini  ← the ≥80% gate",
         f'=IFERROR(SUMPRODUCT(({_crop1c})*{gnum}*({_comp}<{_gcount})*'
         f'(ABS({_dt}-{_gcount})<=1))'
         f'/SUMPRODUCT(({_crop1c})*{gnum}*({_comp}<{_gcount})),"—")'),
        ("Dense lots (7+ buttons) seen (per image)",
         per_image(f'{_gcount},">=7"'))],
 # Second cell used to average `det_overlap_removed`, which reads 0 on every
 # row — and would whatever the fix did.  That counter belongs to the GUIDED
 # dedup in `_detect_buttons_once`; B4 shipped its radius-band + concentric
 # collapse in `_detect_unguided_once`, which reports only to the
 # `>>> DETECT_UNGUIDED:` stdout line and writes no column.  Until that is
 # instrumented, the honest second reading is the defect's own signature: the
 # exactly-+1 cluster, which is the concentric glare rim the fix targets.
 "B4": [("Small lots overcounting unguided (per image)",
         f'=SUMPRODUCT(({_crop1c})*({_gcount}>0)*({_gcount}<=3)*({_nisel}>{_gcount}))'),
        ("Of those, overcounting by exactly +1  ← the concentric rim",
         f'=SUMPRODUCT(({_crop1c})*({_gcount}>0)*({_gcount}<=3)*'
         f'({_nisel}-{_gcount}=1))')],
 "B5": [("Lots at gate=auto (per image)", share(M["ni_gate"], "auto")),
        ("auto AND scale_first  ← the trusted stratum",
         f'=IFERROR({_gated}/{_images},"—")'),
        ("Loophole check — auto on a bailed detector (must be 0)",
         per_image(f'{m}!{M["ni_gate"]}2:{M["ni_gate"]},"auto"',
                   f'{m}!{M["det_detector_used"]}2:{M["det_detector_used"]},"grid"'))],
 # The first cell says "Lots" and counted CROPS — the distribution directly
 # below it is per image, so the two rows of one block disagreed about their
 # own unit (67.0% against ~73%).
 "B9": [("Lots on a rescue mask path (per image)",
         share(M["det_mask_path"], "*+*")),
        # `#REF!` since the build — same grouped-QUERY spill as A24.
        ("Mask path distribution (per image)",
         dist(m, M["det_mask_path"], limit=12, per_image=True))],
 # Both per-image: coverage is one fact per photo, and a dense sheet used to
 # contribute its coverage once per button (read 162 lots against 39).
 "B11": [("Mean mask coverage (per image)",
          f'=IFERROR(SUMPRODUCT(({_crop1c})*{m}!{M["det_mask_coverage"]}2:'
          f'{M["det_mask_coverage"]})/'
          + numeric(f'{m}!{M["det_mask_coverage"]}2:{M["det_mask_coverage"]}')
          + ',"—")'),
         ("Coverage > 0.75 (per image)",
          per_image(f'{m}!{M["det_mask_coverage"]}2:{M["det_mask_coverage"]},">0.75"'))],
 # Read 51 swaps against 5 and a 698-lot population against 65 — a 10x
 # inflation on the one front whose whole question is "does this ever fire?".
 "B14": [("Lots where the swap fired (per image)",
          per_image(f'{m}!{M["det_n_swapped"]}2:{M["det_n_swapped"]},">0"')),
         ("Lots with an unbacked circle (the population it exists for)",
          f'={_gem_lots}')],
 # Per-image, like B14: these are the same two facts and were reading the
 # same 10x-inflated per-crop counts (698 and 51 against 65 and 5).
 "B7": [("Unbacked-circle lots  ← the population", f'={_gem_lots}'),
        ("Swap fired on",
         per_image(f'{m}!{M["det_n_swapped"]}2:{M["det_n_swapped"]},">0"')),
        ("not_a_button confirmations to grade against",
         f'=COUNTIF({c}!{C["source"]}2:{C["source"]},"not_a_button")')],
 # `ni_scale_conf` is written once per PHOTO and repeated down the lot, so
 # both readings were lot-size-weighted.
 "B19": [("scale_first share", share(M["ni_scale_path"], "scale_first")),
         ("Mean scale confidence (per image)",
          f'=IFERROR(AVERAGEIFS({m}!{M["ni_scale_conf"]}2:'
          f'{M["ni_scale_conf"]},{CROP1}),"—")'),
         ("Lots with zero scale confidence (per image)",
          per_image(f'{m}!{M["ni_scale_conf"]}2:{M["ni_scale_conf"]},0'))],
 # `det_white_recovered` is written once per PHOTO but repeated on every
 # crop row, so SUM counted an 80-button sheet's recovery 80 times.  SUMIFS
 # pins it to crop 1 and ignores the empty strings a paste leaves behind.
 "B20": [("Buttons recovered by the rim pass (per image)",
          f'=IFERROR(SUMIFS({m}!{M["det_white_recovered"]}2:'
          f'{M["det_white_recovered"]},{CROP1}),"—")')],
 # Both halves were crop-weighted, so the ratio was a lot-size-weighted
 # average rather than the per-lot accuracy the front is about.
 # Both halves were crop-weighted, so the ratio was a lot-size-weighted
 # average rather than the per-lot accuracy the front is about.
 "B21": [("DT peaks within ±1 of Gemini (per image)",
          f'=IFERROR(SUMPRODUCT(({_crop1c})*{gnum}*'
          f'(ABS({_dt}-{_gcount})<=1))/'
          + numeric(_gcount) + ',"—")')],
 "B22": [("Lots with an unbacked Hough circle",
          f'=IFERROR({_gem_lots}/{_gem_scored},"—")'),
         # COUNTBLANK over an open range counted every empty row in the grid,
         # not just the data — it read 22698 against a 4170-row corpus.
         ("Lots where the match could not run (blank ≠ zero)",
          f'={_images}-{_gem_scored}')],
 # Denominator was every confirm_log row, but 458 of 2,468 are `gemini_count`
 # bookkeeping, not confirmations a person could have tapped.  The taps are
 # rates against REAL confirmations: 0.65%/1.30% pooled read against
 # 0.80%/1.59%.
 "B23": [("not_a_button rate (of real confirmations)",
          f'=IFERROR(COUNTIF({c}!{C["source"]}2:{C["source"]},"not_a_button")'
          f'/({typed(c, C["source"])}-{bookkeeping}),"—")'),
         ("missed_button rate (of real confirmations)",
          f'=IFERROR(COUNTIF({c}!{C["source"]}2:{C["source"]},"missed_button")'
          f'/({typed(c, C["source"])}-{bookkeeping}),"—")')],
 # The whole front is "where is the boundary between the two lot shapes",
 # so counting crops put its thumb on exactly the scale it measures: 7+ read
 # 4227 and 1-6 read 82, a 52:1 split that is mostly just lot size.
 # The whole front is "where is the boundary between the two lot shapes",
 # so counting crops put its thumb on exactly the scale it measures: 7+ read
 # 4227 and 1-6 read 82, a 52:1 split that is mostly just lot size.  The 7+
 # cell also needs {gnum} — it is the one comparison here with nothing above
 # it, so an un-scored row's empty string satisfied ">= 7".  The 1-6 cell is
 # already safe: text fails its "< 7" half.
 "B25": [("Fused lots by size — 7+ buttons (per image)",
          f'=SUMPRODUCT(({_crop1c})*{gnum}*({_gcount}>=7)*'
          f'({_comp}<{_gcount}))'),
         ("Fused lots — 1-6 buttons (per image)",
          f'=SUMPRODUCT(({_crop1c})*({_gcount}>0)*({_gcount}<7)*'
          f'({_comp}<{_gcount}))')],
 "B26": [("scale_first share of the feed", share(M["ni_scale_path"], "scale_first")),
         ("Rows", rows)],
 "B27": [("Grid fallback rate (per image)", share(M["det_detector_used"], "grid")),
         # Was a per-crop count against a per-image denominator, so it could
         # report more flooded grid lots than there were grid lots (665 vs 144).
         ("Of grid lots, how many were flooded",
          per_image(f'{m}!{M["det_detector_used"]}2:{M["det_detector_used"]},"grid"',
                    f'{m}!{M["det_mask_coverage"]}2:{M["det_mask_coverage"]},">0.6"'))],
 # The worst of the per-crop readings: an 80-button sheet counted its one
 # mask path 80 times, so "lots taking the whitepass rescue" read 983 against
 # 54 and the saturation fallback 1184 against 83 — 18x and 14x.
 "B28": [("Lots taking the whitepass rescue (per image)",
          per_image(f'{m}!{M["det_mask_path"]}2:{M["det_mask_path"]},"*whitepass*"')),
         ("Lots taking a saturation fallback (per image)",
          per_image(f'{m}!{M["det_mask_path"]}2:{M["det_mask_path"]},"*satfallback*"'))],
 # The dangerous one: this QUERY FIT, so it spilled instead of erroring and
 # wrote `clahe_lab 471` / `hsv 7347` down column C — on top of the "Pooled
 # over…" note and the PROGRESS LOG header, in the log's own `n` column.  A
 # third variant would have pushed it onto the first log row.
 "B29": [("Preprocessing variant distribution (per image)",
          dist(m, M["ni_variant"], limit=10, per_image=True))],
 # "Lots where Hough engaged" read 7289 against a corpus of ~900 images.
 # "Lots where Hough engaged" read 7289 against a corpus of ~900 images.
 "B30": [("Lots where Hough engaged (per image)",
          per_image(f'{_hough},">0"')),
         ("Small lots (1-3) where it engaged (per image)",
          f'=SUMPRODUCT(({_crop1c})*({_gcount}>0)*({_gcount}<=3)*'
          f'({_hough}>0))')],
 # A grouped QUERY returns one row per sport and the tab budgets ONE row, so
 # the spill hit the "Pooled over…" note below it and the cell rendered #REF!
 # for the whole life of the workbook.  The front's question is accrual on the
 # winter-sports shelf, and that is a scalar: how many confirms carry a sport
 # that is not Football.
 "C4": [("Non-football typed confirms  ← the shelf's accrual",
         f'={typed(c, C["chosen_type"])}'
         f'-COUNTIF({c}!{C["chosen_type"]}2:{C["chosen_type"]},"Football")')],
 "D2": [("Rows carrying a rerank read",
         f'=COUNTIF({c}!{C["rank_rerank"]}2:{C["rank_rerank"]},"?*")'),
        ("Rerank better than live",
         f'=SUMPRODUCT(({c}!{C["rank_rerank"]}2:{C["rank_rerank"]}<>"")*'
         f'({c}!{C["rank_rerank"]}2:{C["rank_rerank"]}<'
         f'{c}!{C["rank_restricted"]}2:{C["rank_restricted"]}))')],
 # Every count here was per-CROP, so an 80-button sheet counted 80 gated
 # "lots": it read 1349 against 215 images.  _gated/_agree/_disagree are
 # pinned to crop_num = 1.
 # Every count here was per-CROP, so an 80-button sheet counted 80 gated
 # "lots": it read 1349 against 215 images.  _gated/_agree/_disagree are
 # pinned to crop_num = 1.
 #
 # Rows 4-7 split the same gate by lot shape.  The pooled number alone
 # cannot say whether Stage B is blocked everywhere or only on dense lots,
 # and those are very different conclusions: the first is a research
 # programme (fusion — B21 refuted, B3 has no instrument for its real
 # question, B19 is the hard track), the second is a rollout that can ship
 # for small lots now with dense lots staying on Gemini.
 "E2": [("Gated lots (auto + scale_first, per image)", f'={_gated}'),
        ("Of those, unguided count == Gemini  ← the ≥98% gate",
         f'=IFERROR({_agree}/{_gated_scored},"—")'),
        ("Within ±1 of Gemini  ← the cheaper question, same columns",
         f'=IFERROR({_agree1}/{_gated_scored},"—")'),
        ("\u21b3 small lots (1-6): gated n", _strat(_small)),
        ("\u21b3 small lots: count == Gemini  ← the gate, this stratum",
         _strat(_small, agree=True)),
        ("\u21b3 dense lots (7+): gated n", _strat(_dense)),
        ("\u21b3 dense lots: count == Gemini  ← the gate, this stratum",
         _strat(_dense, agree=True)),
        # The rollback tripwire, which this front said it did not have.  It
        # did: ✏️ Fix count has been on every gate=auto post since 2026-07-19
        # and writes count_source=auto_overridden, downgraded back to `auto`
        # when the operator opens the modal and keeps the number.  Nothing
        # READ it, which is a different problem and is what these cells fix.
        #
        # It is a FLOOR on disagreement, not a measurement of it: the one-tap
        # "Match these N" default lets an unchecked count through as `auto`,
        # so a wrong count nobody looked at is not counted.  That is the right
        # direction for a rollback rule — it can only fail to fire, never fire
        # falsely — and the wrong direction for proving a stratum is under 2%.
        # Do not close the gate with these cells; only trip it.
        #
        # Shape comes from ni_selected, the count the auto path actually put
        # in front of the operator, not from Gemini's count as the strata
        # above do: an override is a statement about what was shown.
        ("Auto-count lots put to the operator (auto + auto_overridden)",
         f'=COUNTIFS({CROP1},{_cs},"auto")'
         f'+COUNTIFS({CROP1},{_cs},"auto_overridden")'),
        ("\u21b3 small (1-6 detected): override rate  ← tripwire, a floor",
         _override(_ni_small)),
        ("\u21b3 dense (7+ detected): override rate  ← tripwire, a floor",
         _override(_ni_dense))],
 "E3": [("Lots below gate=auto (what Gemini would still be called on)",
         f'=IFERROR(1-{_gate_auto}/{_images},"—")')],
 # "Confirmations accrued" was COUNTA over every confirm_log row, and 529 of
 # them are `gemini_count` bookkeeping rather than a confirmation the auto
 # path or a person made.  It does not change the verdict — 16x the gate
 # instead of 17x — but it is the cell INDEX reads for E4's Evidence, and a
 # gate should count the thing it names.  A10's "Confirms total" is the same
 # population and moves with it, so the workbook cannot hold two different
 # answers to "how many confirmations are there".
 "E4": [("Confirmations accrued (no bookkeeping)  ← the ≥300 gate", real_confirms),
        ("Of those, auto-path",
         f'=COUNTIF({c}!{C["source"]}2:{C["source"]},"*auto*")'),
        ("Corrections logged (precision denominator)",
          f'=COUNTIF({c}!{C["source"]}2:{C["source"]},"correction")')],
    }


LIVE = _live()

# front id -> index into LIVE[id] whose value IS the gate's accrual count.
#
# A13 was listed here and must not be: its gate counts "50 slogans whose
# shelves are filled to the 4-cap" and its LIVE row counts CONFIRMATIONS won
# with a low gap — different units entirely, so INDEX read 2237/50 = 4474%
# for a front on which nothing has been filled.  Shelf depth is not in the
# Logger at all; it is an offline count, so A13 falls to the typed-log path
# like every other front whose gate a cell cannot see.  A number in the wrong
# unit is worse than a blank: the blank asks to be filled.
VOLUME_LIVE = {"A10": 0, "E4": 0, "B3": 0, "B4": 0}


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

# (row, register_fields key, the label in column A) — the THE FRONT block.
FRONT_ROWS = [(5, "B5", "Track"), (6, "B6", "Status"), (7, "B7", "Stage"),
              (8, "B8", "Volume needed"), (9, "B9", "Instrument"),
              (10, "B10", "Gate"), (11, "B11", "Standing"),
              (12, "B12", "Source"), (13, None, "Owner"),
              (14, None, "Next action")]
FRONT_ROW_HEIGHT = {9: 46, 10: 60, 11: 74, 12: 30}
STAGE_BAR = '=REPT("\u2588",B7)&REPT("\u2591",6-B7)'
BUMP_NOTE = ("Bump Stage when the evidence moves; log the reading that "
             "moved it below.")
LIVE_BAND = "LIVE — recomputed from the pasted Logger tabs"
LIVE_NONE_BAND = "LIVE — none; this front is graded offline"
LIVE_NONE_NOTE = ("No cell formula can grade this one — see Instrument "
                  "above. Record the reading from the offline analysis in "
                  "the log below.")
POOLED_NOTE = "Pooled over whatever is currently in match_log / confirm_log."
LOG_BAND = "PROGRESS LOG — one line per Logger export graded against the gate"
LOG_HEADER_ROW = 18
LOG_ROWS = 14


LIVE_FIRST_ROW = 18


def live_rows(front):
    return LIVE.get(front["id"], [])


def log_row_for(front):
    """Where the PROGRESS LOG header lands, from the LIVE block's height.

    Both the builder and the repair script need this and must agree: the
    builder to place the log, the repair script to know which rows it must
    never touch.
    """
    live = live_rows(front)
    return LIVE_FIRST_ROW + (len(live) + 2 if live else 2)


def register_fields(front):
    """The cells on a front tab that come from the REGISTER, as (cell, value).

    The single definition the builder and the repair script both write from.
    It exists because they disagreed: the workbook built 2026-09-12 carried
    A1 as `SHADOW`/stage 4 and A25 as `DECIDED-HOLD`/stage 6 long after the
    register had moved them to `SETTLED-REFUTED`/5 and `SHADOW`/3, and no
    repair could pull them back — `emit_apps_script` wrote LIVE formulas and
    nothing else, so every status, stage, gate and standing in the deployed
    workbook was frozen at build time. Nineteen of the register's fronts had
    drifted by 2026-09-20.

    Everything here is a plain value. The derived cells beside them — the
    stage bar in C7, INDEX's `Stage`, `Progress`, `Toward`, `Owner` and
    `Next action` — are formulas pointing at these, so they follow on their
    own once these are written.
    """
    vol = (f'{front["VolumeN"]} {front["VolumeOf"]}' if front["VolumeN"]
           else "— the gate names no n")
    return [
        ("A1", f'{front["id"]} — {front["title"]}'),
        ("A2", front["Question"]),
        ("B5", front["Track"]),
        ("B6", front["Status"]),
        ("B7", front["Stage"]),
        ("D7", LADDER[front["Stage"]]),
        ("B8", vol),
        ("B9", front["Instrument"]),
        ("B10", front["Gate"]),
        ("B11", front["Standing"]),
        ("B12", front["Source"]),
    ]


def volume_formula(front):
    """C8 — progress against the n the gate names, or "" where it names none.

    Two sources. A front whose accrual a cell can count reads it straight off
    its own LIVE block; the rest read the newest `n` a human typed into the
    PROGRESS LOG. A13 used to be in the first group with a LIVE row counting
    a different unit than its gate — see VOLUME_LIVE.
    """
    if not front["VolumeN"]:
        return ""
    if front["id"] in VOLUME_LIVE and live_rows(front):
        src = f"B{LIVE_FIRST_ROW + VOLUME_LIVE[front['id']]}"
        return f'=IFERROR({src}/{front["VolumeN"]},"")'
    lo = log_row_for(front) + 1
    hi = log_row_for(front) + LOG_ROWS
    return (f'=IFERROR(LOOKUP(2,1/(C{lo}:C{hi}<>""),'
            f'C{lo}:C{hi})/{front["VolumeN"]},"")')


def write_front_tab(wb, front):
    ws = wb.create_sheet(tab_name(front))
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 104
    for L in "CDEFG":
        ws.column_dimensions[L].width = 18

    # Every register-sourced value comes from register_fields, so a tab the
    # builder writes and a tab the repair script refreshes carry the same
    # thing in the same cell.
    f = dict(register_fields(front))
    _band(ws, 1, f["A1"], 7, font=H1, fill=NAVY)
    ws.cell(row=2, column=1, value=f["A2"]).alignment = WRAP
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=7)
    ws.row_dimensions[2].height = 44

    _band(ws, 4, "THE FRONT", 7)
    for row, key, label in FRONT_ROWS:
        _kv(ws, row, label, f.get(key, ""), height=FRONT_ROW_HEIGHT.get(row))
    ws.cell(row=7, column=3, value=STAGE_BAR)
    ws.cell(row=7, column=4, value=f["D7"]).font = DIM
    vol_cell = ws.cell(row=8, column=3)
    _kv(ws, 13, "Owner", "")
    _kv(ws, 14, "Next action", "")
    ws.cell(row=15, column=2, value=BUMP_NOTE).font = DIM

    live = LIVE.get(front["id"])
    if live:
        _band(ws, 17, LIVE_BAND, 7)
        for k, (label, formula) in enumerate(live):
            r = LIVE_FIRST_ROW + k
            lab = ws.cell(row=r, column=1, value=label)
            lab.alignment = WRAP
            ws.cell(row=r, column=2, value=formula)
        ws.cell(row=LIVE_FIRST_ROW + len(live), column=1,
                value=POOLED_NOTE).font = DIM
    else:
        _band(ws, 17, LIVE_NONE_BAND, 7)
        ws.cell(row=LIVE_FIRST_ROW, column=1, value=LIVE_NONE_NOTE).font = DIM

    log_row = log_row_for(front)
    _band(ws, log_row - 1, LOG_BAND, 7)
    for i, h in enumerate(LOG_COLS, start=1):
        c = ws.cell(row=log_row, column=i, value=h)
        c.font = BOLD
        c.fill = PatternFill("solid", fgColor=LIGHT)
        c.border = BOX
    for k in range(LOG_ROWS):
        ws.cell(row=log_row + 1 + k, column=1).number_format = "yyyy-mm-dd"
    front["_log_row"] = log_row
    if front["VolumeN"]:
        vol_cell.value = volume_formula(front)
        if front["id"] in VOLUME_LIVE and live:
            ws.cell(row=8, column=4, value="counts itself — no typing").font = DIM
        vol_cell.number_format = "0%"
    ws.freeze_panes = "A4"


IDX_COLS = ["Front", "Title", "Track", "Stage", "Progress", "Toward",
            "Evidence", "Status", "Latest reading", "As of", "Owner",
            "Next action", "Gate"]
IDX_WIDTHS = [8, 34, 26, 7, 10, 40, 10, 18, 22, 12, 12, 28, 70]
IDX_HEADER_ROW = 6


def index_row(front, r):
    """The 13 INDEX cells for one front, on sheet row `r`.

    Shared by the builder and the repair script for the same reason as
    register_fields: INDEX is the landing page, and a landing page that
    disagrees with the tabs it links to is worse than no landing page. The
    deployed workbook said 69 fronts and carried A1 as SHADOW long after the
    register had refuted it.

    Everything derived lives in a formula pointing at the front's own tab —
    Stage, the bar, the ladder text, Evidence, Owner, Next action — so those
    follow when the tab is synced. Only Title, Track, Status and Gate are
    values, and they are the four this returns from the register.
    """
    q = f"'{tab_name(front)}'"
    first, last = log_row_for(front) + 1, log_row_for(front) + LOG_ROWS
    # LOOKUP(2, 1/(range<>""), range) returns the LAST non-empty cell.
    def newest(letter):
        rng = f"{q}!{letter}{first}:{letter}{last}"
        return f'=IFERROR(LOOKUP(2,1/({rng}<>""),{rng}),"—")'
    return [
        front["id"],
        front["title"],
        front["Track"],
        # Stage lives on the front tab so bumping it there moves the landing
        # tab; the bar and the ladder text are derived, never typed.
        f"={q}!B7",
        f'=REPT("\u2588",D{r})&REPT("\u2591",6-D{r})',
        f'=IFERROR(VLOOKUP(D{r},ROLLUP!$E$3:$F$9,2,FALSE),"")',
        # Evidence: latest logged n against the volume the gate names.
        (f"={q}!C8" if front["VolumeN"] else "—"),
        front["Status"],
        newest("D"),
        newest("A"),
        f"={q}!B13",
        f"={q}!B14",
        front["Gate"],
    ]


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
    for i, w in enumerate(IDX_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for k, f in enumerate(fronts):
        r = hr + 1 + k
        for col, value in enumerate(index_row(f, r), start=1):
            if col == 1:
                # The one cell the repair script cannot reproduce as-is: a
                # real .xlsx hyperlink here, a HYPERLINK() formula there,
                # because only Apps Script can resolve a tab's gid.
                c = ws.cell(row=r, column=col, value=f["id"])
                c.hyperlink = Hyperlink(
                    ref=c.coordinate, location=f"'{tab_name(f)}'!A1")
                c.font = LINK
                continue
            ws.cell(row=r, column=col, value=value)
        ws.cell(row=r, column=6).alignment = TOP
        ws.cell(row=r, column=7).number_format = (
            "0%" if f["VolumeN"] else "General")
        ws.cell(row=r, column=10).number_format = "yyyy-mm-dd"
        ws.cell(row=r, column=13).alignment = WRAP
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
        # NO wrap on these: a wrapped 400-character cell makes Sheets stretch
        # row 1 to fit it, which swamps the header.  Unwrapped, it simply
        # overflows across the empty columns to its right.
        far = len(header) + 2
        for k, line in enumerate((
            f"PASTE the Logger's {tab} rows here, starting at A2, under the "
            "header to the left.",
            "Easiest route: in the Logger, right-click the tab → Copy to → "
            f"Existing spreadsheet → this file. Then delete THIS tab and "
            f"rename the copy to exactly '{tab}'.",
            "To pool across exports instead, paste values and append each new "
            "export below the last. Formulas read whole columns, so either "
            "works at any size.",
        )):
            c = ws.cell(row=1 + k, column=far, value=line)
            c.font = DIM
            c.alignment = TOP
        ws.cell(row=5, column=far, value='=COUNTA(A2:A)&" rows"').font = BOLD

    ws = wb.create_sheet(DER)
    ws.cell(row=1, column=1, value=(
        "Derived from confirm_log — the leaderboard JSON unpacked once so "
        "every front can read it. ARRAYFORMULA, so it extends itself as rows "
        "are pasted. Do not type here.")).font = DIM
    for i, (name, _expr) in enumerate(DERIVED, start=1):
        ws.column_dimensions[get_column_letter(i)].width = 22
        c = ws.cell(row=2, column=i, value=name)
        c.font = WHITE_F
        c.fill = PatternFill("solid", fgColor=NAVY)
    ws.cell(row=3, column=1, value=derived_formula())
    # derived is the one tab that needs headroom: its ARRAYFORMULAs spill.
    ws.cell(row=DERIVED_ROWS, column=len(DERIVED), value="")
    ws.freeze_panes = "A3"


GOOGLE_ONLY = ("ARRAYFORMULA", "REGEXEXTRACT", "REGEXREPLACE", "QUERY",
               "FILTER", "UNIQUE")


def write_repair(wb):
    """Google Sheets converts an uploaded .xlsx by parsing formulas as EXCEL
    formulas, and these functions have no Excel equivalent — they arrive as
    #NAME?.  Every affected cell is listed here as plain text so it can be
    copied back in without leaving the sheet."""
    ws = wb.create_sheet("REPAIR")
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 8
    ws.column_dimensions["C"].width = 130
    _band(ws, 1, "REPAIR — formulas the .xlsx import cannot carry", 3,
          font=H1, fill=NAVY)
    for i, line in enumerate((
        "Sheets parses an uploaded .xlsx as Excel. ARRAYFORMULA, REGEXEXTRACT, "
        "REGEXREPLACE, QUERY, FILTER and UNIQUE are Google-only, so those "
        "cells land as #NAME?. Everything else converts fine.",
        "To fix: copy the formula text from column C, go to the cell named in "
        "columns A/B, and paste it in. Do the derived row first — most of the "
        "workbook depends on it.",
        "Only needed once, on a fresh import.",
    )):
        ws.cell(row=2 + i, column=1, value=line).font = DIM

    r = 6
    for name in ("derived", *[w.title for w in wb.worksheets
                              if w.title not in ("derived", "REPAIR")]):
        if name not in wb.sheetnames:
            continue
        for row in wb[name].iter_rows():
            for c in row:
                v = c.value
                if (isinstance(v, str) and v.startswith("=")
                        and any(g in v for g in GOOGLE_ONLY)):
                    ws.cell(row=r, column=1, value=name)
                    ws.cell(row=r, column=2, value=c.coordinate)
                    # Leading apostrophe keeps it text, not a formula.
                    ws.cell(row=r, column=3, value="'" + v)
                    r += 1
    ws.cell(row=4, column=3, value=f"{r - 6} cells").font = BOLD
    ws.freeze_panes = "A6"


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
        ("FIRST — check the REPAIR tab",
         "Sheets parses an uploaded .xlsx as Excel, so Google-only functions "
         "(ARRAYFORMULA, REGEXEXTRACT, QUERY...) arrive as #NAME?. REPAIR "
         "lists every affected cell with its formula as text; paste them back "
         "in, derived first. One-time, on a fresh import."),
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
         f"{sum(1 for f in fronts if LIVE.get(f['id']))} of the "
         f"{len(fronts)} fronts have a LIVE block that recomputes on "
         f"every paste. The other "
         f"{sum(1 for f in fronts if not LIVE.get(f['id']))} have no "
         "cell formula that can grade them — the "
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


# The LIVE row count each front was BUILT with.  The deployed workbook's
# layout is fixed: LIVE rows start at 18 and the "Pooled over…" note, the
# PROGRESS LOG header and the operator's typed readings follow immediately.
# The Apps Script repair writes in place, so growing a block by even one row
# silently overwrites the log.  Read off the built workbook 2026-09-09.
LIVE_ROW_BUDGET = {
    "A1": 2, "A2": 2, "A3": 7, "A4": 7, "A7": 3, "A8": 2, "A10": 4, "A11": 2,
    "A12": 2, "A13": 1, "A16": 2, "A23": 2, "A24": 1, "A25": 2,
    "B2": 2, "B3": 3, "B4": 2, "B5": 3, "B7": 3, "B9": 2, "B11": 2, "B14": 2,
    "B19": 3, "B20": 1, "B21": 1, "B22": 2, "B23": 2, "B25": 2, "B26": 2,
    "B27": 2, "B28": 2, "B29": 1, "B30": 2, "C4": 1, "D2": 2, "E2": 10,
    "E3": 1, "E4": 3,
}


def check_live_row_budget():
    """Raise if a front's LIVE block height changed without this map saying so.

    This used to mean "never change a block", because the repair script wrote
    formulas in place and a taller block would have written straight over the
    PROGRESS LOG.  It no longer does: the repair relays out a tab whose block
    changed height, and refuses if that would strand a row the operator
    typed.  So the map is now a DECLARATION of the shape the workbook should
    have, not a freeze — change a block and change its entry in the same
    commit, and the next repair migrates the deployed tab.

    It is still a guard worth having.  The heights here are what the repair
    script writes against, and a block that grew without the map growing
    would have the script write a formula onto the "Pooled over…" note.
    """
    bad = []
    for fid, rows in LIVE.items():
        want = LIVE_ROW_BUDGET.get(fid)
        if want is not None and len(rows) != want:
            bad.append(f"{fid}: {len(rows)} rows, workbook has {want}")
    if bad:
        raise SystemExit(
            "LIVE block no longer matches the built workbook — the repair "
            "script would overwrite the PROGRESS LOG:\n  " + "\n  ".join(bad))


def deploy_tables(fronts):
    """Everything the repair script writes: (formulas, values).

    One definition, because `build_fingerprint` has to hash exactly what
    `emit_apps_script` emits — a fingerprint computed over anything else
    would be a second source of truth and could agree while the scripts
    differed, which is the failure it exists to catch.
    """
    # Formulas, written with setFormula.  C7 and C8 sit in the THE FRONT
    # block but are derived from it, so they ride along here.
    cells = [(DER, "A3", derived_formula(), None)]
    for f in fronts:
        tab = tab_name(f)
        for k, (label, formula) in enumerate(LIVE.get(f["id"], [])):
            cells.append((tab, f"B{LIVE_FIRST_ROW + k}", formula, label))
        cells.append((tab, "C7", STAGE_BAR, None))
        # "" clears C8 on a front whose gate has stopped naming an n.
        cells.append((tab, "C8", volume_formula(f), None))

    # Values from the register, written with setValue.
    values = []
    for f in fronts:
        tab = tab_name(f)
        values += [(tab, cell, v) for cell, v in register_fields(f)]
        values += [(tab, f"A{row}", label) for row, _k, label in FRONT_ROWS]
    return cells, values


def build_fingerprint(fronts):
    """Eight hex characters identifying what a build deploys."""
    cells, values = deploy_tables(fronts)
    return hashlib.sha256(
        repr((values, cells)).encode("utf-8")).hexdigest()[:8]


def emit_apps_script(fronts, path):
    """A full resync of a workbook that is already in Sheets.

    Pasting formulas by hand goes wrong in ways that have nothing to do with
    the formulas: Sheets splits pasted text on commas, an array formula will
    not overwrite a non-empty neighbour, and the .xlsx import drops every
    Google-only function.  setFormula() has none of those problems -- it
    writes the string straight into the cell.  Idempotent: re-run it any time.

    It used to write LIVE formulas and nothing else, and that was the hole the
    workbook fell through.  A front's Status, Stage, Gate and Standing are
    written once at build time, the register keeps moving, and there was no
    way to pull them back: by 2026-09-20 nineteen of the register's fronts
    disagreed with the deployed workbook -- A1 shown as an open SHADOW three
    stages after it was refuted, A25 shown closed after it had reopened --
    and three fronts added since the build (B32, B33, C7) had no tab at all.
    The only repair anyone had was a rebuild, which costs the pasted Logger
    corpus and every typed PROGRESS LOG line.

    So it now writes everything the register owns, and only what the register
    owns:

      * the THE FRONT block on every tab (register_fields),
      * the LIVE formulas and the two derived cells beside the stage,
      * INDEX whole -- the header count, the per-stage strip, and every row,
      * a scaffolded tab for any front the workbook has never seen.

    It never touches a PROGRESS LOG row, the raw Logger tabs, or Owner and
    Next action -- those are the operator's, and a resync that ate them would
    be a worse bug than the drift it fixes.

    Every front's LIVE block must keep the row count it was BUILT with.  The
    script writes in place from row 18, and the "Pooled over..." note, the
    PROGRESS LOG header and the operator's typed readings sit directly below
    -- one extra row per front would overwrite the log this workbook exists
    to keep.  ``check_live_row_budget`` enforces that.

    Writes the LABEL (column A) beside every LIVE formula.  A repair that
    corrects what a cell computes but leaves the old caption is worse than no
    repair: A3's bands are graded human-confirmed now, and a row still
    captioned "[0.82, 0.85)" would read as the pooled number it no longer is.
    """
    check_live_row_budget()
    cells, values = deploy_tables(fronts)

    # [tab, live rows, log header row] -- enough to raise a front tab from
    # nothing.  Only used when the tab is absent; an existing tab is never
    # re-scaffolded, because its log rows are below the scaffold.
    scaffold = [[tab_name(f), len(LIVE.get(f["id"], [])), log_row_for(f)]
                for f in fronts]

    index = [index_row(f, IDX_HEADER_ROW + 1 + k)
             for k, f in enumerate(fronts)]
    strip = [f'{st}: {sum(1 for f in fronts if f["Stage"] == st)}'
             for st in range(7)]
    below = (f'{sum(1 for f in fronts if f["Stage"] <= 2)} fronts are below '
             f'stage 3 — not yet graded at volume.')

    # A fingerprint of everything this script deploys.  Two builds with the
    # same one write the same workbook; a different one means the register or
    # a formula moved.
    #
    # It exists because a stale paste is otherwise invisible.  On 2026-09-20
    # a run reported "1747 of 1747 cells written across 72 fronts" and had
    # deployed the PREVIOUS script: the file has a fixed name, the cell count
    # does not change when a register VALUE changes, and the status line said
    # nothing that could tell the two apart.  The register edits looked
    # applied and were not.  Now the line carries the build, so re-running a
    # stale paste shows a fingerprint that does not match the one the build
    # printed.
    fingerprint = build_fingerprint(fronts)

    bands = {"front": "THE FRONT", "bump": BUMP_NOTE, "live": LIVE_BAND,
             "liveNone": LIVE_NONE_BAND, "liveNoneNote": LIVE_NONE_NOTE,
             "pooled": POOLED_NOTE, "log": LOG_BAND}

    def js(x):
        return json.dumps(x, ensure_ascii=False)

    lines = [
        "/**",
        " * Resyncs the Progress Trackers workbook with LOGGER_FRONTS.md.",
        " *",
        " * Run after importing the .xlsx, and again whenever the register",
        " * moves: Extensions > Apps Script, paste this in, Run, approve the",
        " * permission prompt. Safe to re-run at any time.",
        " *",
        " * It writes the register's cells and the computed ones. It does NOT",
        " * touch the PROGRESS LOG, the pasted Logger tabs, or Owner/Next",
        " * action.",
        " *",
        f" * {len(cells)} formulas and {len(values)} values across "
        f"{len(fronts)} fronts.",
        f" * Build {fingerprint}. The status line repeats it — if the REPAIR",
        " * tab shows a different one after a run, the editor still holds an",
        " * older paste and the newest changes were NOT deployed.",
        " * Generated by ebayscout/tools/build_goal_trackers.py -- do not edit.",
        " */",
        "function repairFormulas() {",
        "  var ss = SpreadsheetApp.getActiveSpreadsheet();",
        f"  var LIVE_FIRST_ROW = {LIVE_FIRST_ROW}, LOG_ROWS = {LOG_ROWS};",
        f"  var IDX_HEADER_ROW = {IDX_HEADER_ROW};",
        f"  var LOG_COLS = {js(LOG_COLS)};",
        f"  var BANDS = {js(bands)};",
        f"  var SCAFFOLD = {js(scaffold)};",
        f"  var STRIP = {js(strip)};",
        f"  var BELOW = {js(below)};",
        f"  var IDX_WIDTHS = {js(IDX_WIDTHS)};",
        "  var INDEX = [",
    ]
    for row in index:
        lines.append(f"    {js(row)},")
    lines.append("  ];")
    lines.append("  var VALUES = [")
    for tab, cell, value in values:
        lines.append(f"    [{js(tab)}, {js(cell)}, {js(value)}],")
    lines.append("  ];")
    lines.append("  var CELLS = [")
    for tab, cell, formula, label in cells:
        lines.append(f"    [{js(tab)}, {js(cell)}, {js(formula)}, "
                     f"{js(label)}],")
    lines += [
        "  ];",
        "",
        "  // Tab names are derived from a front's TITLE, and titles change:",
        "  // A25 was renamed 'Edition-twin wrong-year picks' ->",
        "  // 'Edition-twin resolution' on 2026-09-09, so the generated name",
        "  // stopped matching the deployed tab and its two LIVE cells would",
        "  // have gone quietly stale.  A front's ID never changes, so match",
        "  // on the 'A25 ' prefix and RENAME the tab to the canonical name:",
        "  // every formula below refers to tabs by that name, and Sheets",
        "  // rewrites existing references when a sheet is renamed.",
        "  var byId = {};",
        "  var all = ss.getSheets();",
        "  for (var s = 0; s < all.length; s++) {",
        "    var m = all[s].getName().match(/^([A-E]\\d+) /);",
        "    if (m) { byId[m[1]] = all[s]; }",
        "  }",
        "  var created = [], renamed = [];",
        "  for (var i = 0; i < SCAFFOLD.length; i++) {",
        "    var want = SCAFFOLD[i][0];",
        "    var sh = ss.getSheetByName(want);",
        "    if (!sh) {",
        "      var idm = want.match(/^([A-E]\\d+) /);",
        "      if (idm && byId[idm[1]]) {",
        "        sh = byId[idm[1]];",
        "        renamed.push(sh.getName() + '  ->  ' + want);",
        "        sh.setName(want);",
        "      }",
        "    }",
        "    if (!sh) {",
        "      sh = ss.insertSheet(want);",
        "      scaffoldFront(sh, SCAFFOLD[i][1], SCAFFOLD[i][2]);",
        "      created.push(want);",
        "    }",
        "    byId[want.match(/^([A-E]\\d+) /)[1]] = sh;",
        "  }",
        "",
        "  // A LIVE block that changed height moves the PROGRESS LOG header,",
        "  // so an existing tab has to be re-laid-out before anything is",
        "  // written into it. This is what used to make the row budget a",
        "  // freeze rather than a declaration. It refuses rather than",
        "  // stranding a typed row above the new header.",
        "  var relaid = [], stale = [];",
        "  for (var i = 0; i < SCAFFOLD.length; i++) {",
        "    var sh = ss.getSheetByName(SCAFFOLD[i][0]);",
        "    if (!sh || created.indexOf(SCAFFOLD[i][0]) >= 0) { continue; }",
        "    var r = relayout(sh, SCAFFOLD[i][1], SCAFFOLD[i][2]);",
        "    if (r === 'moved') { relaid.push(SCAFFOLD[i][0]); }",
        "    if (r === 'occupied') { stale.push(SCAFFOLD[i][0]); }",
        "  }",
        "",
        "  var missing = [], wrote = 0;",
        "  for (var i = 0; i < VALUES.length; i++) {",
        "    var sh = ss.getSheetByName(VALUES[i][0]);",
        "    if (!sh) { missing.push(VALUES[i][0]); continue; }",
        "    sh.getRange(VALUES[i][1]).setValue(VALUES[i][2]);",
        "    wrote++;",
        "  }",
        "  for (var i = 0; i < CELLS.length; i++) {",
        "    var sh = ss.getSheetByName(CELLS[i][0]);",
        "    if (!sh) { missing.push(CELLS[i][0]); continue; }",
        "    // setFormula('') clears the cell, which is what a front whose",
        "    // gate no longer names an n needs to happen to its C8.",
        "    sh.getRange(CELLS[i][1]).setFormula(CELLS[i][2]);",
        "    if (CELLS[i][3] !== null) {",
        "      // Keep the caption truthful about what the cell now computes.",
        "      sh.getRange('A' + CELLS[i][1].substring(1))"
        ".setValue(CELLS[i][3]);",
        "    }",
        "    wrote++;",
        "  }",
        "",
        "  // INDEX is rebuilt whole: it said '69 fronts' with three tabs",
        "  // missing, and its A4 label had been overwritten by an earlier",
        "  // version of this script's own status line.",
        "  var idx = ss.getSheetByName('INDEX');",
        "  if (idx) {",
        "    idx.getRange('A1').setValue('INDEX — ' + INDEX.length "
        "+ ' fronts');",
        "    idx.getRange('A4').setValue('Fronts per stage');",
        "    for (var s = 0; s < STRIP.length; s++) {",
        "      idx.getRange(4, 2 + s).setValue(STRIP[s]);",
        "    }",
        "    idx.getRange(4, 10).setValue(BELOW);",
        "    idx.getRange(IDX_HEADER_ROW + 1, 1, INDEX.length, "
        "INDEX[0].length).setValues(INDEX);",
        "    for (var i = 0; i < INDEX.length; i++) {",
        "      // Only Apps Script can resolve a tab's gid, so column A is a",
        "      // HYPERLINK() here where the .xlsx carries a real link.",
        "      var t = ss.getSheetByName(SCAFFOLD[i][0]);",
        "      if (!t) { continue; }",
        "      idx.getRange(IDX_HEADER_ROW + 1 + i, 1).setFormula(",
        "        '=HYPERLINK(\"#gid=' + t.getSheetId() + '\",\"'",
        "        + INDEX[i][0] + '\")');",
        "    }",
        "    // A register that lost a front would otherwise leave its row.",
        "    var extra = idx.getLastRow() - IDX_HEADER_ROW - INDEX.length;",
        "    if (extra > 0) {",
        "      idx.getRange(IDX_HEADER_ROW + INDEX.length + 1, 1, extra,",
        "                   INDEX[0].length).clearContent();",
        "    }",
        "  } else { missing.push('INDEX'); }",
        "",
        "  SpreadsheetApp.flush();",
        "  var msg = new Date().toISOString() + ' — ' + wrote + ' of '",
        "          + (VALUES.length + CELLS.length) + ' cells written across '",
        f"          + INDEX.length + ' fronts. [build {fingerprint}]';",
        "  if (created.length) { msg += '  TABS CREATED: ' "
        "+ created.join(' | '); }",
        "  if (relaid.length) { msg += '  RELAID OUT: ' "
        "+ relaid.join(' | '); }",
        "  if (stale.length) {",
        "    msg += '  LAYOUT STALE, NOT MOVED (typed log rows would be "
        "stranded — move them by hand, then re-run): ' + stale.join(' | ');",
        "  }",
        "  if (renamed.length) { msg += '  TABS RENAMED: ' "
        "+ renamed.join(' | '); }",
        "  if (missing.length) {",
        "    var uniq = missing.filter(function (v, k, a) "
        "{ return a.indexOf(v) === k; });",
        "    msg += '  MISSING TABS: ' + uniq.join(' | ');",
        "  }",
        "  Logger.log(msg);",
        "  // Status goes to a tab of its own, CREATED if absent.  It used to",
        "  // fall back to ss.getSheets()[0] -- which is INDEX -- and wrote the",
        "  // message over INDEX!A4, the 'Fronts per stage' row label",
        "  // (2026-09-12: it did exactly that).  Never write a status line",
        "  // into a tab that carries content.",
        "  var out = ss.getSheetByName('REPAIR');",
        "  if (!out) { out = ss.insertSheet('REPAIR'); }",
        "  out.getRange('A1').setValue("
        "'Repair log — written by repairFormulas().');",
        "  out.getRange('A2').setValue(msg);",
        "  SpreadsheetApp.flush();",
        "  // NO getUi().alert() here.  When the script IS container-bound it",
        "  // does not throw -- it opens a modal and BLOCKS until somebody",
        "  // clicks, so a run nobody is watching burns the full 6-minute quota",
        "  // and dies with 'Exceeded maximum execution time' AFTER the writes",
        "  // have already succeeded (2026-09-12: 87/87 written in 4s, killed at",
        "  // 5m57s).  The unflushed status write was lost with it.  The log line",
        "  // above and the REPAIR tab are the report.",
        "  return msg;",
        "",
        "  function relayout(sh, liveRows, logRow) {",
        "    // Rows 1..logRow belong to the generator; logRow+1 down belong",
        "    // to the operator. Nothing to do if the header is already where",
        "    // this build wants it -- the normal case, so this costs one",
        "    // read per tab.",
        "    if (sh.getRange(logRow, 1).getValue() === LOG_COLS[0]) {",
        "      return 'ok';",
        "    }",
        "    var depth = Math.max(sh.getLastRow(), logRow + LOG_ROWS) ;",
        "    var colA = sh.getRange(1, 1, depth, 1).getValues();",
        "    var oldLog = -1;",
        "    for (var r = 0; r < colA.length; r++) {",
        "      if (colA[r][0] === BANDS.log) { oldLog = r + 2; break; }",
        "    }",
        "    if (oldLog < 0) { return 'ok'; }   // nothing recognisable to move",
        "    // Refuse if the operator has typed anything into the old log.",
        "    var typed = sh.getRange(oldLog + 1, 1, LOG_ROWS, LOG_COLS.length)",
        "                  .getValues();",
        "    for (var r = 0; r < typed.length; r++) {",
        "      for (var k = 0; k < typed[r].length; k++) {",
        "        if (typed[r][k] !== '' && typed[r][k] !== null) {",
        "          return 'occupied';",
        "        }",
        "      }",
        "    }",
        "    // Clear the generator's old rows from the LIVE band down through",
        "    // the empty log, then lay it out at the new height.",
        "    var from = 17;",
        "    var to = Math.max(oldLog + LOG_ROWS, logRow + LOG_ROWS);",
        "    sh.getRange(from, 1, to - from + 1, LOG_COLS.length)",
        "      .clearContent();",
        "    scaffoldFront(sh, liveRows, logRow);",
        "    return 'moved';",
        "  }",
        "",
        "  function scaffoldFront(sh, liveRows, logRow) {",
        "    // Only the parts that are the same on every front tab; the",
        "    // register's own cells are written by the VALUES pass above.",
        "    sh.getRange('A4').setValue(BANDS.front);",
        "    sh.getRange('B15').setValue(BANDS.bump);",
        "    if (liveRows > 0) {",
        "      sh.getRange('A17').setValue(BANDS.live);",
        "      sh.getRange('A' + (LIVE_FIRST_ROW + liveRows))"
        ".setValue(BANDS.pooled);",
        "    } else {",
        "      sh.getRange('A17').setValue(BANDS.liveNone);",
        "      sh.getRange('A' + LIVE_FIRST_ROW).setValue(BANDS.liveNoneNote);",
        "    }",
        "    sh.getRange('A' + (logRow - 1)).setValue(BANDS.log);",
        "    sh.getRange(logRow, 1, 1, LOG_COLS.length).setValues([LOG_COLS]);",
        "    sh.getRange(logRow + 1, 1, LOG_ROWS, 1)"
        ".setNumberFormat('yyyy-mm-dd');",
        "    sh.setColumnWidth(1, 120);",
        "    sh.setColumnWidth(2, 700);",
        "    sh.setFrozenRows(3);",
        "  }",
        "}",
    ]
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return len(cells) + len(values)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", default="LOGGER_FRONTS.md")
    ap.add_argument("--csv", help="also write a flat one-row-per-front CSV")
    ap.add_argument("--apps-script", help="also write the Sheets repair script")
    ap.add_argument("-o", "--out", default="Logger - Progress Trackers.xlsx")
    args = ap.parse_args()

    fronts = parse_register(args.register)
    seen = {}
    for f in fronts:
        n = tab_name(f)
        if n in seen:
            raise SystemExit(f"tab name collision: {f['id']} and {seen[n]} → {n}")
        seen[n] = f["id"]

    if args.apps_script:
        n = emit_apps_script(fronts, args.apps_script)
        print(f"{n} cells → {args.apps_script}")
        print(f"  build {build_fingerprint(fronts)} — the REPAIR tab must "
              f"show this after the run, or the paste is stale")

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
    write_repair(wb)   # after the front tabs, so it can scan them
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
