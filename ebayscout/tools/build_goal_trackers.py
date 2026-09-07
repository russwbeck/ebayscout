"""build_goal_trackers — generate the sister workbook of per-front progress trackers.

WHY THIS EXISTS
---------------
The Logger workbook (`match_log` + `confirm_log`) is an append-only firehose: one
row per crop, 110 instrumented columns.  It records where every front stands but
says nothing about where each front is *going* — there is no per-signal target,
no trend, no "are we closer than last month".

This script emits a sister workbook with one tab per instrumented front, each
carrying the front's definition, its goal + target, a live read of the current
value pulled straight out of the Logger via IMPORTRANGE, and a dated progress
log the operator appends to after each measured batch.  An INDEX tab rolls all
of them up.

The Logger is the source of truth; this workbook never writes back to it.

USAGE
-----
    python -m ebayscout.tools.build_goal_trackers  [-o goal_trackers.xlsx]

The .xlsx is then uploaded to Drive and converted to a Google Sheet.  Re-run it
whenever MATCH_HEADER / CONFIRM_HEADER change: the front list is derived from
`match_logging`, so a new column becomes a new tracker tab (and an unknown
column fails the build loudly rather than being silently dropped).
"""

from __future__ import annotations

import argparse
import os
import sys

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.hyperlink import Hyperlink

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from ebayscout import match_logging as ml  # noqa: E402


# The Logger workbook this sister sheet reads from.
LOGGER_KEY = "11BJAJv4tkPKtkrlPVyZqaMpKkpwfuv9t_tnk5WdjPzI"
LOGGER_URL = f"https://docs.google.com/spreadsheets/d/{LOGGER_KEY}"

# Columns that identify a row rather than measure anything.  Listed on INDEX as
# context, given no tracker tab.
CONTEXT_COLS = {
    "ts", "service", "command", "mode", "job_id", "thread_ts", "channel_id",
    "user_id", "crop_num", "check_id",
}

# --- The front spec ---------------------------------------------------------
# (kind, direction, target, definition, goal)
#   kind      count | px | pct | ratio | score | cat | bool | json | text
#   direction up | down | zero | track | eq
#   target    a string shown verbatim; "" means "no target set yet"
#
# Targets and baselines are quoted from AUTOMATION_ROADMAP.md,
# AUTOMATION_VISION.md, MANAGEMENT_BRIEF.md and tested_hypothesis.md.  Where
# those docs set no number the target is left blank ON PURPOSE — an invented
# target is worse than an empty cell.

M = {
 "det_h": ("px", "track", "≥ 800 px on the long edge",
   "Height in px of the frame the detector actually saw (after resize).",
   "Detection is resolution-fragile: the white-on-white miss (Part VIII) was a "
   "resampling artefact. Keep frames at or above the 800px working size."),
 "det_w": ("px", "track", "≥ 800 px on the long edge",
   "Width in px of the frame the detector actually saw (after resize).",
   "Same as det_h — watch the share of lots arriving below the working size."),
 "det_bg_brightness": ("score", "track", "",
   "Mean V of the sampled border region, 0–255.",
   "Segments the pool for every other front: detection and classification both "
   "behave differently by background (DASH_BACKGROUND_ANALYSIS)."),
 "det_bg_saturation": ("score", "track", "",
   "Mean S of the sampled border region, 0–255.",
   "The other half of the background segmentation."),
 "det_bg_is_white": ("bool", "track", "",
   "Border sampler's verdict that the background is white.",
   "The cream quilted blanket that caused defect C read FALSE here and so took "
   "the blue_or_white path into an 89%-coverage mask. Watch for lots where the "
   "verdict is wrong, not just its rate."),
 "det_mask_path": ("cat", "track", "blue_or_white share ↑, rescue-path share ↓",
   "Which mask route ran: blue_or_white, +whitepass, +bgdiff, bright variant.",
   "Every rescue suffix is a lot the primary mask failed on. Shrinking the "
   "rescue share is the mask-blindness cure (AUTOMATION_VISION §2)."),
 "det_hough_pass1": ("count", "track", "",
   "Circles found by guided Hough on the first pass (Gemini-radius seeded).",
   "The old '~80% on singles' number. Guided, so it flatters us — hold it as "
   "the ceiling that unguided (ni_selected) has to climb toward."),
 "det_hough_retry": ("count", "zero", "0",
   "Circles found on the retry pass, when pass 1 was rejected.",
   "A retry means pass 1 was wrong. Drive the retry rate to zero."),
 "det_count_user": ("count", "zero", "0 lots requiring human count",
   "The count the human typed in.",
   "THE headline front. Full automation is exactly 'this column is never "
   "needed' (AUTOMATION_VISION §1). Track the share of lots that still need it."),
 "det_count_noinput": ("count", "up", "exact-match rate → ≥ 96% (today's auto stratum)",
   "Unguided count — the multi-scale sweep with no human input, no Gemini seed.",
   "The number that has to become trustworthy before the guided count can be "
   "dropped. Baseline: 34% exact overall on Logger_4 (300 lots, 2026-07-02)."),
 "det_user_count": ("count", "track", "",
   "The count actually used by the pipeline for this row.",
   "The join key for grading every count front against truth."),
 "det_detector_used": ("cat", "track", "grid-fallback share → 0",
   "Which detector produced the crops: hough | grid | whole_image.",
   "grid means detection gave up and cut a rectangle. 62.7% grid-fallback "
   "inside the saturated pool was the defect-C signature; 2a took a real "
   "35-lot batch to 35/35 guided."),
 "det_n_crops": ("count", "track", "",
   "How many crops this image produced.",
   "Volume denominator for the per-crop fronts."),
 "det_raw_hough": ("count", "track", "",
   "Circles Hough returned before any filtering.",
   "The top of the filtering funnel — read together with the four *_removed "
   "fronts to see where localisation loses buttons."),
 "det_circles_rejected": ("count", "down", "",
   "Raw circles thrown away by the filter stack.",
   "High rejection with a low final count means the filters, not Hough, are "
   "the bottleneck."),
 "det_rejection_rate": ("pct", "down", "",
   "det_circles_rejected / det_raw_hough.",
   "Normalised view of the filtering loss, comparable across lot sizes."),
 "det_border_removed": ("count", "down", "",
   "Circles dropped for touching the frame border.",
   "Per-stage filter breakdown — isolates which rule is over-eager."),
 "det_fill_removed": ("count", "down", "",
   "Circles dropped by the fill-ratio test.",
   "Per-stage filter breakdown."),
 "det_overlap_removed": ("count", "down", "",
   "Circles dropped as overlapping/concentric duplicates.",
   "Defect B's fix lives here: the +1 concentric glare rim was the largest "
   "single overcount cluster (69 of 216 singles)."),
 "det_radius_min": ("px", "track", "",
   "Smallest accepted circle radius, px.",
   "Radius spread is the scale-trust signal; extremes expose bad r_est."),
 "det_radius_max": ("px", "track", "",
   "Largest accepted circle radius, px.",
   "Radius spread is the scale-trust signal; extremes expose bad r_est."),
 "det_radius_mean": ("px", "track", "",
   "Mean accepted circle radius, px.",
   "Compare against det_expected_radius — divergence is a scale failure."),
 "det_radius_std": ("px", "down", "",
   "Std-dev of accepted radii, px.",
   "Buttons in one lot are the same size. A wide spread means the detection "
   "mixed real buttons with phantoms."),
 "det_buttons_per_megapixel": ("ratio", "track", "",
   "Accepted circles per megapixel of frame.",
   "Density prior — implausible density is a cheap phantom detector."),
 "det_expected_radius": ("px", "track", "",
   "Radius the scale estimator expected before Hough ran.",
   "The seed that guided Hough gets and unguided doesn't. Closing the gap "
   "between this and a no-input estimate is Phase 2b's whole job."),
 "det_mask_components": ("count", "track", "",
   "Connected components in the binary mask.",
   "components < gemini_button_count is the fusion signature (defect A)."),
 "det_edge_density": ("ratio", "track", "",
   "Edge pixels / total pixels over the whole frame.",
   "Whole-image quality signal — separates busy backgrounds from clean ones."),
 "det_brightness_std": ("score", "track", "",
   "Std-dev of frame brightness.",
   "Whole-image quality signal — flags uneven lighting."),
 "ni_conservative": ("count", "track", "",
   "Unguided count from the conservative Hough pass.",
   "One of three candidate passes the unguided selector chooses between."),
 "ni_standard": ("count", "track", "",
   "Unguided count from the standard Hough pass.",
   "One of three candidate passes the unguided selector chooses between."),
 "ni_aggressive": ("count", "track", "",
   "Unguided count from the aggressive Hough pass.",
   "One of three candidate passes the unguided selector chooses between."),
 "ni_selected": ("count", "up", "exact ≥ 96% / ±1 ≥ 100% (match the auto stratum)",
   "The unguided count the selector settled on.",
   "The automation candidate. Baseline 34% exact overall (singles 31.5%, 36% "
   "overcounting by 2+) on Logger_4. Grade it against det_user_count."),
 "ni_confidence": ("score", "up", "",
   "Selector's confidence in ni_selected, 0–1.",
   "Feeds ni_gate. Capped at 0.4 when mask coverage > 0.75."),
 "ni_layout_conf": ("score", "up", "",
   "Confidence that the detected circles form a plausible grid layout.",
   "Grid geometry is resolution-independent (Part VIII) — a strong trust "
   "signal even when detection is fragile."),
 "ni_outliers": ("count", "down", "0",
   "Circles the layout fit called outliers.",
   "Outliers are phantoms or misses; a clean lot has none."),
 "ni_pass_winner": ("cat", "track", "",
   "Which unguided pass won: conservative | standard | aggressive.",
   "Tells you which sweep is carrying the automation, and where to tune."),
 "ni_contour_count": ("count", "track", "",
   "Buttons found by the contour fallback.",
   "Phase 2 fallback for when Hough finds nothing on a saturated mask."),
 "ni_merged_count": ("count", "track", "",
   "Count after merging Hough and contour candidates.",
   "The union pass — Layer 2's colour-blind recovery."),
 "ni_source": ("cat", "track", "",
   "Which unguided source produced the final number: hough | contour | merged.",
   "A rising contour/merged share means Hough alone is failing more often."),
 "ni_variant": ("cat", "track", "",
   "Preprocessing variant used: none | CLAHE | LAB.",
   "Phase 3 preprocessing A/B — which variant earns its CPU."),
 "bank": ("cat", "track", "",
   "Which reference bank the match was scored against (era + sport).",
   "The restriction the shadow pass is trying to prove unnecessary."),
 "restricted_top_json": ("json", "track", "100% populated",
   "Top-10 leaderboard the user actually saw (bank-restricted).",
   "The LIVE result. Every shadow front is graded as a paired comparison "
   "against this same-row column — coverage here gates all of them."),
 "shadow_enabled": ("bool", "up", "TRUE on 100% of rows",
   "Whether the unrestricted counterfactual pass ran on this crop.",
   "Kill switch is BUTTONMATCHER_SHADOW_PASS=0. If this goes FALSE at scale, "
   "every shadow measurement silently stops."),
 "shadow_top_json": ("json", "track", "",
   "Top-10 leaderboard over the unrestricted universe (all years, all sports).",
   "If the truth's rank here is consistently 1, the bank restriction is no "
   "longer earning its keep and can be automated away."),
 "ni_bgdiff": ("bool", "track", "",
   "Whether the background-difference mask path was used.",
   "The bg-diff rescue called a woven mat foreground once (coverage 100%) — "
   "watch it alongside det_mask_coverage."),
 "ni_r_est": ("px", "track", "",
   "Unguided radius estimate, px.",
   "Layer 1 is the real bottleneck: radius/scale, not Hough. This is the "
   "number that has to become trustworthy."),
 "ni_scale_conf": ("score", "up", "",
   "Confidence in ni_r_est, 0–1.",
   "ni_scale_conf = 0 was a telltale on five of six wrong auto lots; Phase 3.5 "
   "tightened the gate on exactly that."),
 "ni_scale_path": ("cat", "track", "scale_first share ↑",
   "How the scale was derived: scale_first | (other paths).",
   "scale_path — not scale_conf — is THE trust signal (§3.1). scale_first is "
   "the trustworthy stratum: 96% exact / 100% ±1 at n=329."),
 "ni_est_rows": ("count", "track", "",
   "Estimated grid rows.",
   "Grid geometry is resolution-independent — the force-fill signal."),
 "ni_est_cols": ("count", "track", "",
   "Estimated grid columns.",
   "Grid geometry is resolution-independent — the force-fill signal."),
 "ni_gate": ("cat", "up", "auto share ≥ 50% of lots at ≥ 98% exact",
   "Confidence gate verdict: auto | suggest | manual.",
   "The rollout is gate-scoped, not bucket-scoped (Phase 5). auto was 23.4% of "
   "lots at 90.3% exact on Logger_4; after 3.5, 96%/100%±1 at n=329. Growing "
   "the auto share at that accuracy IS the staircase to full automation."),
 "count_source": ("cat", "track", "user share → 0",
   "Where the count/grid came from: user | auto | auto_overridden | suggest | gemini.",
   "The end state is that no row ever reads 'user'. auto_overridden is the "
   "cost signal: how often the human had to undo us."),
 "rerank_json": ("json", "track", "",
   "Two-level reference re-rank scores for the offered candidates.",
   "The reference flywheel is the moat (§5) — rerank is where reference photos "
   "turn into ranking lift."),
 "gemini_button_count": ("count", "track", "",
   "Gemini's button count for the lot.",
   "Today's ground truth on the pipeline. Every unguided front is graded "
   "against it until human truth is available."),
 "n_recovered": ("count", "track", "",
   "Buttons recovered by reconciliation that detection alone missed.",
   "Direct measure of what the Gemini→Hough reconcile is buying."),
 "reconcile_misses_json": ("json", "track", "",
   "Gemini points reconciliation could not back with a detection.",
   "The labelled miss pool — the training set for the next detection fix."),
 "det_hough_dp": ("score", "track", "config — no target",
   "Hough dp parameter in force for this row.",
   "Tuning knob, logged so a behaviour change can be attributed to a param "
   "change rather than to the data."),
 "det_hough_mindist": ("px", "track", "config — no target",
   "Hough minDist parameter in force for this row.",
   "Tuning knob — attribution, not a goal."),
 "det_hough_param1": ("score", "track", "config — no target",
   "Hough param1 (Canny high threshold) in force for this row.",
   "Tuning knob — attribution, not a goal."),
 "det_hough_param2": ("score", "track", "config — no target",
   "Hough param2 (accumulator threshold) in force for this row.",
   "Tuning knob — attribution, not a goal."),
 "det_hough_minradius": ("px", "track", "config — no target",
   "Hough minRadius in force for this row.",
   "Tuning knob — attribution, not a goal."),
 "det_hough_maxradius": ("px", "track", "config — no target",
   "Hough maxRadius in force for this row.",
   "Tuning knob — attribution, not a goal."),
 "det_rej_radius_min": ("px", "track", "",
   "Smallest radius among rejected circles, px.",
   "If rejected radii cluster on the accepted band, the filters are eating "
   "real buttons."),
 "det_rej_radius_median": ("px", "track", "",
   "Median radius among rejected circles, px.",
   "Compare against det_radius_mean — overlap means over-filtering."),
 "det_rej_radius_max": ("px", "track", "",
   "Largest radius among rejected circles, px.",
   "Compare against det_radius_mean — overlap means over-filtering."),
 "det_mask_blobs_raw": ("count", "track", "",
   "Raw mask blob count before any splitting.",
   "Gap-5 over-merge signal: blobs far below the true count means fusion."),
 "det_dt_peaks_total": ("count", "track", "",
   "Summed distance-transform peaks per blob (0.55× each blob's own DT max).",
   "NOT a counter in the wild — 12% exact on 25 fused lots. It is the "
   "RADIUS/fusion signal; Phase 2b re-runs Hough at the DT-corrected radius."),
 "det_mask_coverage": ("pct", "down", "saturated (>0.75) share ↓ from 19.2% baseline",
   "Foreground fraction of the mask, 0–1.",
   "The defect-C saturation trigger. >0.75 fuses buttons and background into "
   "one sheet: guided exact fell to 19.6% vs 64.5% on normal masks. Note the "
   "flood floor has overfit its calibration set twice (§4.9, §4.11)."),
 "det_white_recovered": ("count", "track", "",
   "Buttons recovered by the white-rescue rim pass.",
   "Layer-2 gap metric — what the colour-blind recovery is buying."),
 "det_gem_unmatched": ("count", "down", "",
   "Hough circles with no Gemini point behind them.",
   "Placement / non-button blind spot. Blank means the match could not run "
   "(unknown), which is different from zero — check coverage as well as mean."),
 "det_gem_unmatched_json": ("json", "track", "",
   "The unbacked circles themselves.",
   "The labelled phantom pool for the next placement fix."),
 "det_n_swapped": ("count", "track", "",
   "Hough phantoms dropped in favour of a real Gemini miss.",
   "The two-signal reconcile swap (§4.6) — a phantom was SUPPRESSING a real "
   "button before this landed."),
 "det_reconcile_swaps_json": ("json", "track", "",
   "The swaps themselves — a labelled phantom pool.",
   "Training data for the phantom classifier."),
 "det_gemini_anchored_json": ("json", "track", "",
   "Gemini-anchored A/B shadow: agreement, snap distance, gemini-only/hough-only.",
   "§4.8's hypothesis under test — anchor crops on Gemini x/y, refine with "
   "Hough. Physical anchoring is what separates right from wrong associations "
   "(Part VII)."),
 "fullres_top_json": ("json", "track", "",
   "Top-10 leaderboard when matched at ≤2200px instead of the 800px frame.",
   "Logger_19 A/B. The same-photo A/B REFUTED full-res as a win; the shadow "
   "stays deployed for the 100-lot read. Measurement only — 800px is live."),
 "variant_top_json": ("json", "track", "",
   "Top-10 leaderboard with punctuation-normalised slogan variants unioned in.",
   "Does a truth that is off the restricted board come ON with variants? "
   "Targets the hyphen/apostrophe pun family."),
 "within_year_json": ("json", "track", "",
   "The hidden within-year competition for #1's year: runner_up_margin, top[5].",
   "Every other leaderboard is folded to one row per year, so a wrong "
   "within-year pick is invisible to the gap rules and the visual veto — the "
   "2026-09-03 wrong auto. Grade runner_up_margin against truth to replace the "
   "curated confusable list with a general margin."),
}

C = {
 "chosen_year": ("cat", "track", "",
   "The year the human confirmed.",
   "Human truth. The grading key for every ranking front."),
 "chosen_phrase": ("text", "track", "",
   "The slogan the human confirmed.",
   "Human truth. The grading key for every ranking front."),
 "chosen_type": ("cat", "track", "",
   "Sport/type of the confirmed button: Football | WBB | MBB | Wrest.",
   "Type drift is a known classification failure mode (DASH_CLASSIFICATION)."),
 "typed_slogan": ("text", "track", "",
   "Slogan the user typed instead of picking from the board.",
   "A typed entry means the board failed. Its rate is a direct board-quality "
   "measure — and note rank_restricted is YEAR-based, so it misleads here."),
 "source": ("cat", "track", "auto/gemini_auto share ↑ at ≥ 98% precision",
   "How the confirmation happened: gemini_auto | edition_pick | typed | manual | …",
   "The automation share, from the human's side of the glass."),
 "rank_restricted": ("count", "down", "rank 1 on ≥ 98% of confirmations",
   "Rank of the confirmed answer in the restricted board the user saw.",
   "Top-1 accuracy of the live product. KNOWN BUG: this is YEAR-based, so it "
   "misleads on typed entries (§ 'BUG found while grading', Logger_19)."),
 "rank_shadow": ("count", "down", "converge on rank_restricted",
   "Rank of the confirmed answer in the unrestricted shadow board.",
   "If this equals rank_restricted, the bank restriction earns nothing and can "
   "be dropped — that is the whole point of the shadow."),
 "shadow_leaderboard_size": ("count", "track", "",
   "How many candidates the shadow board scored.",
   "The denominator that makes rank_shadow interpretable."),
 "restricted_top_json": ("json", "track", "",
   "The restricted board at confirmation time.",
   "Paired-comparison anchor for every shadow front on this row."),
 "shadow_top_json": ("json", "track", "",
   "The unrestricted board at confirmation time.",
   "Paired-comparison partner for rank_shadow."),
 "typed_top_json": ("json", "track", "",
   "The board scored against what the user typed.",
   "Shows whether the typed answer was reachable and simply ranked too low."),
 "rank_image_only": ("count", "down", "",
   "Rank of the truth using image similarity alone, no text signal.",
   "Isolates how much CLIP-on-pixels is contributing vs the slogan text."),
 "rank_rerank": ("count", "down", "beat rank_restricted",
   "Rank of the truth in the unrestricted + reference-rerank board.",
   "The reference flywheel's payoff metric (§5)."),
 "edition_shadow_json": ("json", "track", "",
   "Reference-photo arbitration for twin-family confirmations. {} for non-twins.",
   "The edition_twins guard — the variants the matcher provably cannot tell "
   "apart (see the Inventory notes on greenback vs metalback)."),
 "rank_centered": ("count", "down", "beat rank_restricted",
   "Rank of the truth when each candidate's text sim is centred on that "
   "slogan's own background baseline.",
   "Logger_14 layer 3: text_scores range 0.34–0.80 across 515 phrases, so a "
   "'hot' embedding starts with up to a +0.46 head start before the crop is "
   "even seen. Centring scores the evidence, not the temperature."),
}


# --- styling ---------------------------------------------------------------

NAVY = "FF041E42"          # Penn State navy
LIGHT = "FFEFF3F8"
RULE = "FFC8D2E0"
WHITE_F = Font(color="FFFFFFFF", bold=True)
H1 = Font(size=14, bold=True, color="FFFFFFFF")
BOLD = Font(bold=True)
DIM = Font(color="FF5A6B80")
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")
thin = Side(style="thin", color=RULE)
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)


def _title_row(ws, row, text, span):
    ws.cell(row=row, column=1, value=text).font = H1
    for c in range(1, span + 1):
        ws.cell(row=row, column=c).fill = PatternFill("solid", fgColor=NAVY)
    ws.row_dimensions[row].height = 24


def _section(ws, row, text, span):
    ws.cell(row=row, column=1, value=text).font = BOLD
    for c in range(1, span + 1):
        ws.cell(row=row, column=c).fill = PatternFill("solid", fgColor=LIGHT)


def _kv(ws, row, key, value, wrap=False):
    k = ws.cell(row=row, column=1, value=key)
    k.font = BOLD
    k.alignment = TOP
    v = ws.cell(row=row, column=2, value=value)
    v.alignment = WRAP if wrap else TOP
    return v


def tab_name(src, col):
    """Two columns appear in both logs, so the source has to be part of the tab
    name.  Kept ASCII and under Excel's 31-char sheet-name limit so the .xlsx
    survives the round-trip into Sheets unrenamed."""
    name = f"{'M' if src == 'match' else 'C'}_{col}"
    if len(name) > 31:
        raise SystemExit(f"tab name too long for xlsx: {name}")
    return name


def build_fronts():
    """Return [(order, source, column, col_letter, kind, direction, target,
    definition, goal)] for every instrumented column, in header order."""
    out = []
    for src, header, spec in (("match", ml.MATCH_HEADER, M),
                              ("confirm", ml.CONFIRM_HEADER, C)):
        for i, col in enumerate(header, start=1):
            if col in CONTEXT_COLS:
                continue
            if col not in spec:
                raise SystemExit(
                    f"UNSPECIFIED FRONT: {src}.{col} — add it to the spec "
                    "rather than shipping a tracker with no goal.")
            kind, direction, target, definition, goal = spec[col]
            out.append((len(out) + 1, src, col, get_column_letter(i),
                        kind, direction, target, definition, goal))
    return out


def stat_block(ws, row, src, letter, kind, target):
    """Live read of the front, straight out of the Logger. Which statistics are
    meaningful depends on the front's kind — a count is not a percentage and a
    JSON blob is neither."""
    data = "DATA_match" if src == "match" else "DATA_confirm"
    rng = f"{data}!{letter}2:{letter}"
    _section(ws, row, "LIVE READ  (from the Logger, via DATA_ tab)", 4)
    r = row + 1
    ws.cell(row=r, column=4, value="⟵ recomputes whenever the Logger grows").font = DIM

    def put(label, formula, fmt=None):
        nonlocal r
        _kv(ws, r, label, None)
        c = ws.cell(row=r, column=2, value=formula)
        if fmt:
            c.number_format = fmt
        r += 1

    put("Rows logged", f"=COUNTA({rng})")

    if kind in ("count", "px", "score", "ratio"):
        put("Mean", f"=IFERROR(AVERAGE({rng}),\"—\")", "0.00")
        put("Median", f"=IFERROR(MEDIAN({rng}),\"—\")", "0.00")
        put("Min", f"=IFERROR(MIN({rng}),\"—\")", "0.00")
        put("Max", f"=IFERROR(MAX({rng}),\"—\")", "0.00")
        put("Rows reading zero", f"=COUNTIF({rng},0)")
    elif kind == "pct":
        put("Mean", f"=IFERROR(AVERAGE({rng}),\"—\")", "0.0%")
        put("Median", f"=IFERROR(MEDIAN({rng}),\"—\")", "0.0%")
        put("Share above 0.75 (saturation band)",
            f"=IFERROR(COUNTIF({rng},\">0.75\")/COUNT({rng}),\"—\")", "0.0%")
        put("Min / Max",
            f"=IFERROR(MIN({rng})&\" / \"&MAX({rng}),\"—\")")
    elif kind == "bool":
        put("Share TRUE",
            f"=IFERROR(COUNTIF({rng},TRUE)/COUNTA({rng}),\"—\")", "0.0%")
        put("Share FALSE",
            f"=IFERROR(COUNTIF({rng},FALSE)/COUNTA({rng}),\"—\")", "0.0%")
    elif kind in ("cat", "text"):
        put("Distinct values", f"=IFERROR(COUNTA(UNIQUE({rng})),\"—\")")
        ws.cell(row=r + 1, column=1, value="Distribution").font = BOLD
        ws.cell(row=r + 2, column=1, value=(
            f'=IFERROR(QUERY({data}!{letter}1:{letter}, '
            f'"select {letter}, count({letter}) where {letter} is not null '
            f'group by {letter} order by count({letter}) desc limit 15 '
            f'label count({letter}) \'rows\'", 1), "—")'))
        # The QUERY spills downward; leave it room before the progress log.
        r += 20
    elif kind == "json":
        put("Populated rows", f"=COUNTIF({rng},\"?*\")")
        put("Coverage",
            f"=IFERROR(COUNTIF({rng},\"?*\")/COUNTA({data}!A2:A),\"—\")", "0.0%")
        put("Empty-JSON rows",
            f"=COUNTIF({rng},\"[]\")+COUNTIF({rng},\"{{}}\")")

    return r + 1


LOG_HEADERS = ["Date", "n rows", "Value", "Δ vs prev", "vs target",
               "Run / batch", "Note"]


def progress_log(ws, row, rows=14):
    _section(ws, row, "PROGRESS LOG  (append one line per measured batch)", 7)
    hr = row + 1
    for i, h in enumerate(LOG_HEADERS, start=1):
        c = ws.cell(row=hr, column=i, value=h)
        c.font = BOLD
        c.border = BOX
        c.fill = PatternFill("solid", fgColor=LIGHT)
    for k in range(rows):
        r = hr + 1 + k
        ws.cell(row=r, column=1).number_format = "yyyy-mm-dd"
        # Δ only fills once the row above and this row both have values.
        ws.cell(row=r, column=4, value=(
            f'=IF(OR(C{r}="",C{r-1}=""),"",C{r}-C{r-1})'))
    return hr + rows + 2


def write_front_tab(wb, front):
    order, src, col, letter, kind, direction, target, definition, goal = front
    ws = wb.create_sheet(tab_name(src, col))
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 62
    for L in "CDEFG":
        ws.column_dimensions[L].width = 16

    _title_row(ws, 1, f"{col}", 7)
    ws.cell(row=2, column=1, value=definition).alignment = WRAP
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=7)
    ws.row_dimensions[2].height = 30

    r = 4
    _section(ws, r, "THE FRONT", 4); r += 1
    _kv(ws, r, "Front #", order); r += 1
    _kv(ws, r, "Source", f"Logger › {src}_log › column {letter}"); r += 1
    _kv(ws, r, "Kind", kind); r += 1
    _kv(ws, r, "Direction", {
        "up": "higher is better", "down": "lower is better",
        "zero": "drive to zero", "eq": "hit the target exactly",
        "track": "no direction — watch it, segment by it",
    }[direction]); r += 1
    _kv(ws, r, "Goal", goal, wrap=True)
    ws.row_dimensions[r].height = 46; r += 1
    _kv(ws, r, "Target", target or "— not set; pick one before grading this front",
        wrap=True); r += 1
    _kv(ws, r, "Owner", ""); r += 1
    _kv(ws, r, "Status", "").alignment = TOP; r += 2

    r = stat_block(ws, r, src, letter, kind, target)
    progress_log(ws, r)
    ws.freeze_panes = "A4"
    return ws


def write_data_tabs(wb):
    for name, tab in (("DATA_match", "match_log"), ("DATA_confirm", "confirm_log")):
        ws = wb.create_sheet(name)
        formula = f'=IMPORTRANGE("{LOGGER_KEY}","{tab}!A:CZ")'
        ws["A1"] = formula
        ws.column_dimensions["A"].width = 40
        # Belt and braces: IMPORTRANGE is a Sheets-only function, so if the
        # .xlsx import ever drops it, the text below is the cell to paste back.
        ws["A3"] = "If A1 is blank or #NAME?, paste this into A1:"
        ws["A3"].font = DIM
        ws["A4"] = "\u0027" + formula


def write_readme(wb, fronts):
    ws = wb.create_sheet("README", 0)
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 96
    _title_row(ws, 1, "Logger — Progress Trackers", 2)
    rows = [
        ("What this is",
         "A sister workbook to the Logger. The Logger records where every "
         "instrumented front stands; this workbook records where each one is "
         "GOING — its goal, its target, and its trend."),
        ("Source of truth",
         f"The Logger workbook, {LOGGER_URL} — tabs match_log and confirm_log. "
         "Nothing here ever writes back to it."),
        ("Fronts", f"{len(fronts)} — one tab each, in Logger column order. "
         "Tabs are prefixed M· (match_log) or C· (confirm_log) because three "
         "column names appear in both logs."),
        ("FIRST RUN — do this",
         "Open DATA_match. The IMPORTRANGE cell in A1 will show #REF! with "
         "'You need to connect these sheets'. Click Allow access. Do the same "
         "on DATA_confirm. Every tracker fills in from there. If A1 is empty "
         "or #NAME? instead, the import dropped the formula — A4 on that tab "
         "holds the text to paste back into A1."),
        ("Sheets-only formulas",
         "IMPORTRANGE, QUERY and UNIQUE are Google Sheets functions with no "
         "Excel equivalent. They are written into the .xlsx verbatim and "
         "normally survive the import; they will NOT work if you open the "
         "file in Excel instead."),
        ("Each tracker tab",
         "THE FRONT (definition, goal, target, direction) · LIVE READ "
         "(recomputed from the Logger every time it grows) · PROGRESS LOG "
         "(you append one dated line per measured batch)."),
        ("Blank targets",
         "A blank target means no number is set in the roadmap or the tested-"
         "hypothesis record. It is left blank on purpose — an invented target "
         "is worse than an empty cell. Fill it in when you set one."),
        ("Kinds",
         "count · px · pct (0–1 fraction) · ratio · score · cat (categorical) · "
         "bool · json (measured as coverage) · text."),
        ("Regenerating",
         "ebayscout/tools/build_goal_trackers.py. The front list is derived "
         "from match_logging.MATCH_HEADER / CONFIRM_HEADER, so a new logged "
         "column becomes a new tab — and an unspecified one fails the build."),
    ]
    r = 3
    for k, v in rows:
        _kv(ws, r, k, v, wrap=True)
        ws.row_dimensions[r].height = 46
        r += 1


IDX_HEADERS = ["#", "Front", "Source", "Col", "Kind", "Direction", "Target",
               "Rows logged", "Current", "Status", "Owner", "Goal"]


def write_index(wb, fronts):
    ws = wb.create_sheet("INDEX", 1)
    _title_row(ws, 1, "INDEX — every front the Logger tracks", len(IDX_HEADERS))
    ws.cell(row=2, column=1, value=(
        "Current pulls the headline statistic off each front's own tab. "
        "Click a front name to jump to its tracker.")).font = DIM

    hr = 4
    for i, h in enumerate(IDX_HEADERS, start=1):
        c = ws.cell(row=hr, column=i, value=h)
        c.font = WHITE_F
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.border = BOX
    widths = [5, 30, 10, 6, 9, 18, 40, 12, 12, 14, 12, 80]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for k, f in enumerate(fronts):
        order, src, col, letter, kind, direction, target, definition, goal = f
        r = hr + 1 + k
        tn = tab_name(src, col)
        q = f"'{tn}'"
        ws.cell(row=r, column=1, value=order)
        c = ws.cell(row=r, column=2, value=col)
        c.hyperlink = Hyperlink(ref=c.coordinate, location=f"{q}!A1")
        c.font = Font(color="FF1155CC", underline="single")
        ws.cell(row=r, column=3, value=f"{src}_log")
        ws.cell(row=r, column=4, value=letter)
        ws.cell(row=r, column=5, value=kind)
        ws.cell(row=r, column=6, value=direction)
        ws.cell(row=r, column=7, value=target).alignment = WRAP
        # B6 is "Rows logged"; B7 the headline statistic on every tab layout.
        # Every tracker tab shares one layout: B15 rows logged, B16 the
        # headline statistic for that kind, B12 status, B11 owner.
        ws.cell(row=r, column=8, value=f"={q}!B15")
        ws.cell(row=r, column=9, value=f"={q}!B16")
        ws.cell(row=r, column=10, value=f"={q}!B12")
        ws.cell(row=r, column=11, value=f"={q}!B11")
        ws.cell(row=r, column=12, value=goal).alignment = WRAP
    ws.freeze_panes = f"A{hr + 1}"
    ws.auto_filter.ref = f"A{hr}:{get_column_letter(len(IDX_HEADERS))}{hr + len(fronts)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="goal_trackers.xlsx")
    args = ap.parse_args()

    fronts = build_fronts()
    wb = Workbook()
    wb.remove(wb.active)
    write_readme(wb, fronts)
    write_index(wb, fronts)
    write_data_tabs(wb)
    for f in fronts:
        write_front_tab(wb, f)
    wb.save(args.out)
    print(f"{len(fronts)} fronts → {len(wb.sheetnames)} tabs → {args.out}")


if __name__ == "__main__":
    main()
