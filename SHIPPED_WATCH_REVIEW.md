# Shipped-watch review — what closes, what stops, what still needs eyes

**Date:** 2026-09-12 · **Export graded:** the `Logger - Progress Trackers`
workbook — `match_log` 4,170 rows over **637 images** (2026-07-19 →
2026-09-07), `confirm_log` 2,468 rows of which 458 are `gemini_count`
bookkeeping and **2,010 are real confirmations** (390 human-sourced, 1,620
machine). 1,435 confirmations carry a usable leaderboard.

**Why this exists.** `SHADOW_PROMOTION_REVIEW.md` swept the fronts that were
still *paying* for an ungraded instrument — the shadows. This is the other
half: the **17 fronts sitting at `SHIPPED-WATCH`**, which cost nothing to run
and everything to leave unread. A front parked there is a claim that something
shipped and is holding. Nobody had checked, and the pool is now a clean §4.2
batch: every one of these fixes shipped on or before 2026-07-19, so all 637
images are data none of them was tuned on.

The verdicts are **CLOSE** (the gate held on fresh data — write it into
`tested_hypothesis.md` and stop watching), **STOP** (the front is dormant or
answered; stop looking, keep the column), and **KEEP** (still live, with the
next reading named).

> **This review changed no thresholds and no detection code.** It reads the
> log, fixes the tracker formulas that misreport it, and records verdicts.

---

## 1. Six more LIVE readings computed the wrong number

The 2026-09-07 patch fixed eight tabs and named the general defect:
**detection facts are per-image, but `match_log` has one row per crop.** It
audited the shadow fronts. It did not reach the shipped ones, and every
detection front in this review was reading per-crop — so an 80-button sheet
counted its one mask path 80 times.

Fixed in `ebayscout/tools/build_goal_trackers.py`, same three helpers
(`per_image`, plus a new `typed`), each formula re-implemented in Python and
run over the raw tabs before being written:

| Tab | Cell showed | Actually | Cause |
|---|---|---|---|
| A11 | "Non-football confirms" **483** | **5** | `COUNTA` counts the blank `chosen_type` on the 478 confirmations that resolved no type |
| A11 | "Football share" **80.4%** | **99.75%** | same denominator |
| B2 | "Grid fallback on saturated lots" **160** | **37** | per-crop |
| B4 | "Mean concentric circles removed" **0** | *not measurable* | wrong column — see §4 |
| B11 | "Coverage > 0.75" **162** | **39** | per-crop |
| B14 | "Lots where the swap fired" **51** / population **698** | **5** / **65** | per-crop — 10x on the one front whose whole question is "does this ever fire?" |
| B23 | tap rates **0.65% / 1.30%** | **0.80% / 1.59%** | denominator included 458 `gemini_count` bookkeeping rows |
| B28 | "Lots taking the whitepass rescue" **983** | **54** | per-crop — **18x**, the worst in the workbook |
| C4 | **`#REF!`** since the workbook was built | **5** | a grouped `QUERY` returns one row per sport into a 1-row budget; the spill collided with the note below it |

Two guards now pin this: `test_detection_readings_count_images_not_crops`
covers B2/B4/B11/B14/B28 alongside the three it already had, and
`test_confirm_type_counts_ignore_the_untyped_blank` covers A11/C4.

### 1b. And a third defect class the first repair exposed

The repair ran on 2026-09-12 (87 of 87 cells). Fourteen of the fifteen cells
above landed on their predicted value; **B11's mean coverage came back
0.42270 against a predicted 0.42336**, which turned out to be a defect of its
own — one I had just introduced, and one that was already in the workbook
twice over.

`COUNTIFS(range, "<>")` does **not** mean "has a value" on a pasted tab. An
export writes a zero-length **string** into an empty field, and a zero-length
string is not blank, so the criterion counts it. B11's mean was dividing by
637 images instead of the 636 that carry a number.

The same idiom was load-bearing in two cells the 2026-09-07 patch wrote:

| Tab | Cell showed | Actually | Why it matters |
|---|---|---|---|
| B22 | "Lots where the match could not run (blank ≠ zero)" **0** | **157** | the denominator counted all 637 as present, so the front's whole question answered itself — on the one cell whose caption is *about* blanks |
| B22 | "Lots with an unbacked Hough circle" **10.2%** | **13.5%** | 65/637 instead of 65/**480** scored |
| E2 | "unguided count == Gemini ← the ≥98% gate" **74.4%** | **79.6%** | denominator 215 gated lots instead of the **201** carrying a Gemini count |
| E2 | "Within ±1 of Gemini" **89.8%** | **96.0%** | same denominator |
| B7 | population **698** / swap fired **51** | **65** / **5** | never de-cropped by either pass — B7 is B14's sibling and the two tabs disagreed about the size of one population |

`ISNUMBER` is the test that separates a written number from an empty paste,
and it is now used everywhere the bare criterion was. **E2's corrected
numbers reproduce `SHADOW_PROMOTION_REVIEW.md` §1.2 exactly** (201 scored,
79.6%, 96.0% within ±1) — that review's prose had the right figures; only its
formula was half-fixed. No conclusion moves: E2 was already nowhere near its
gate, and B7 and B22 are graded elsewhere. Two more guards:
`test_no_bare_not_blank_criterion_on_a_pasted_column` (every LIVE block) and
`test_b7_and_b14_report_the_same_two_facts_per_image`.

**This needs a second run of the repair script** — eight cells across five
tabs (A23, B7, B11, B22, E2), regenerated from the same generator; the other
79 are byte-identical to the run that already happened.

---

## 2. Verdicts

| Front | | Verdict | Reading that decides it |
|---|---|---|---|
| A11 | Football pre-filter split | **CLOSE** | 63 score-only autos, **0** fired against a different non-Football shadow #1 |
| B10 | Grid-hole force-fill | **CLOSE** | no regression in 637 images |
| B12 | Hole-inversion coverage floor | **CLOSE** | 8 lots inverted, 8/8 reached Hough, none fell to grid |
| B15 | Deficit-fill over-trust | **CLOSE** | doctrine adopted system-wide; path fires on 3 of 637 |
| B18 | Frame fit | **CLOSE** | no DUAL-class recurrence in 637 images |
| B23 | Per-button review taps | **CLOSE (rate half)** | Sept 0.63% / 1.10%, below the 1.5–2.5% standing; the L18 spike is gone |
| B14 | Two-signal reconcile swap | **STOP** | 5 of 637 lots (0.8%); third export in a row reading "dormant" |
| B31 | Fixture regression battery | **KEEP (one action)** | invariant confirmed on 215 production auto lots, but ebayscout has no copy — §5 |
| A18 | The signal ladder | **KEEP** | "0 wrong at every rung" does **not** reproduce — §4 |
| B2 | Mask saturation | **KEEP** | rescues 68% of saturated lots; **6.1% residual, all to grid** |
| B11 | Flood gate's calibration set | **KEEP** | mean coverage 0.423, 39 lots over the bar |
| B17 | Unanchored associations | **KEEP** | 95.3% of Gemini buttons anchored; 4.7% (148) are the incident class |
| B28 | Whitepass telemetry | **KEEP** | now gradeable per-image: 54 whitepass / 83 satfallback lots, 117 buttons |
| A26 | Bowl-year resolution | **KEEP** | 51 `gemini_printed_year` confirms in September — no longer zero-data |
| C4 | Winter-sports shelf | **KEEP (decide)** | **5** non-football confirms in seven weeks — §4 |
| C5 | Label harvester | **KEEP (verify)** | not checkable from a web session — §5 |
| B4 | Small-lot overcount | **KEEP (re-instrumented)** | was ungradeable as instrumented; the gate's counters are now columns — §4 |

Six close and one stops and closes with them; ten stay.
**`SHIPPED-WATCH` 17 → 10.** (B4 was reclassified `BLOCKED` on the reading in
§4, then returned to `SHIPPED-WATCH` the same day once its gate's counters were
appended to `match_log` — see §7 item 6.)

---

## 3. Close — the gate held on a batch it was not tuned on

### A11 — football pre-filter split

The gate: *score-only AUTO is blocked whenever the shadow #1 is a different,
non-Football candidate.* **63 score-only autos in the pool
(`auto_sort` 38, `auto_gap_only` 24, `auto_slogan_gap` 1) and zero
violations.**

The restricted and unfiltered boards now disagree on **12 of 1,435**
confirmations (0.9%); Logger_2 read 9%. Every disagreement was handled by a
human or a tap, never by an auto. And the unfiltered board is earning its
keep — the operator took the shadow candidate over a football #1 on lots like
Hockey `Blue & White Chill Thrill` 0.908 against Football `Southern Cal-amity`
0.660, and Men's Basketball `TTIP PITT` 0.794 against 0.626.

The entry's "unfiltered-suggestions half remains open" is **C4's question**,
not A11's. Hand it over and close this one.

### B12 — hole-inversion ate a good mask

`HOLE_INVERT_MIN_COVERAGE = 0.08`. Eight lots took `+holeinvert` and **all
eight reached Hough; none fell to the grid.** Worth recording: four of them sat
at coverage **0.085**, five thousandths above the floor. The floor is right but
it is not roomy — if inversion ever regresses, this is the number to look at.

### B15 — deficit-fill over-trust

The front's real product was the doctrine — *gate the new path, verify it is
on-target, fall back* — and B2, B11 and B17 all shipped under it. The path
itself fires on **3 of 637 lots**. The "underlying trust question" the entry
leaves open is B7's and B20's, not this one's. Close it as doctrine.

### B10, B18 — no instrument, no recurrence

Both are code-shape fixes with no live column: grid-gap force-fill (resolution
independence) and frame-fit-before-position. Neither incident class recurred
across 637 images and eight weeks. There is nothing a further watch would read.

### B23 — per-button review taps

| | not_a_button | missed_button |
|---|---|---|
| 2026-08 (n=86) | 4.65% | 13.95% |
| 2026-09 (n=1,916) | **0.63%** | **1.10%** |

The standing said `not_a_button` ~1.5–2.5% with the `missed_button` spike
traced to the 1979-front incident. Post-anchor-gate it is **below** that band
and the spike is gone. The rate question is answered. The front's other half —
each tap is a labeled training example — is accrual, and that is C5's gate.

---

## 4. The three readings that changed a conclusion

### A18 — "0 wrong at every rung" does not reproduce

The ladder was graded on Logger_14's 731 rows at **0 wrong at every rung**.
Regraded here, the population has to be split, because `gemini_auto` fires only
when CLIP already agreed with Gemini (the third standing directive):

**On human-picked confirmations — real truth, n=302:**

| Cumulative rung | Fires | Wrong | Precision |
|---|---|---|---|
| A `overall ≥ 0.85` | 46 (15.2%) | 1 | 97.8% |
| ∪ C `gap ≥ 0.15` | 83 (27.5%) | 1 | 98.8% |
| ∪ S `slogan_gap ≥ 0.12` | 109 (36.1%) | 1 | 99.1% |
| ∪ R `sg ≥ 0.05 & ref_sim ≥ 0.90` | **169 (56.0%)** | **1** | **99.4%** |

**On `gemini_auto` rows — agreement, not truth, n=1,015:** the same union
fires on 663 (65.3%) with **14** disagreements.

So the honest standing is **1 wrong in 169 on human truth**, not zero. That is
a good rule — but the gate says *zero*, and it is not met on a fresh batch.

The single miss is worth its own line. 2026-09-04, restricted #1 `1992 "Penn
State and Proud of it"` at overall 0.892, gap 0.162, `slogan_gap` 0.162 — a
rung-A **and** rung-C **and** rung-S fire, with margin to spare. The human
chose `1992 "'Eers to Penn State"`, and the source was **`confusable_pick`**.
The ladder's one failure on truth is a look-alike pair, which is exactly the
class `confusable_slogans.py` was added for on 2026-09-10. **The next move on
A18 is to gate the ladder behind the confusable check, not to loosen a
threshold.**

The held step-down to `slogan_gap` 0.10 stays held: on the machine pool it buys
+39 fires for **+3 wrong**.

### B4 — blocked on instrumentation, not on data

The gate names `det_overlap_removed`. **That column reads 0 on all 3,917 rows
that carry it, and would whatever the fix did** — it belongs to the *guided*
dedup in `_detect_buttons_once`. B4 shipped its radius-band and concentric
collapse in `_detect_unguided_once`, which announces itself on a
`>>> DETECT_UNGUIDED: radius/concentric dedup` stdout line and **writes no
column at all**. The gate's other half, "`ni_selected` vs truth", has no truth
column either: `det_user_count` is populated on **22 of 637** images.

The workbook shows this front at **492% of the volume its gate needs**. It is
counting a population the gate cannot read.

Meanwhile the defect is still there. Taking Gemini's count as the only truth
available at volume:

- small lots (truth 1–3): **140 of 289 overcount (48.4%)**
- singles: **138 of 263 (52.5%)**; excluding gross Gemini miscounts, 99 of 224 (44.2%)
- **the +1 cluster is still the mode: 83 of 140** — the concentric glare rim the fix targets

Against the original 68% of singles this is real improvement, but the gate
wants ≥80% of spurious extras removed and nothing here can show that. **No
further export moves this front.** It needs one of: the unguided dedup's
band/concentric counters promoted into `diag`, or the gate rewritten around a
truth signal that exists. Until then the second LIVE cell reports the defect's
signature (the +1 cluster) rather than a counter that is structurally zero.

### C4 — the accrual premise is not working

The winter-sports shelf fills from typed confirms on non-football lots. In
seven weeks:

| | count |
|---|---|
| Football | 1,985 |
| Hockey | 2 |
| Men's Basketball | 2 |
| Women's Basketball | 1 |
| (no type resolved) | 478 |

**Five.** 99.75% of typed confirms are Football. The gate — *references
accumulate → `ref_sim` starts arbitrating same-slogan twins → the filter
becomes a prior* — is not reachable on this trajectory; at five per seven weeks
the shelf does not fill this decade. The workbook hid this behind `#REF!` and
A11's 483.

This is a decision, not a measurement: either the shelf gets filled
deliberately (upload winter-sports references rather than waiting for lots to
expose the gap), or C4 is re-scoped to the risk-surface half of its
instrument — same-slogan cross-sport twins — and the accrual half is dropped.
**It should not sit at `SHIPPED-WATCH` accruing five a quarter.**

---

## 5. Keep — with the next reading named

**B2 — mask saturation.** Saturation incidence is **unchanged**: 122 of 637
images (19.2%) hit the trigger, against Logger_4's 19.2%. What changed is the
outcome — the two-variant chooser rescues **83 of them (68%)**, and those lots
now read a post-rescue coverage of 0.09–0.20 and go to Hough (60 of 83). The
**39 it cannot rescue** — where neither the blue-only nor the bright variant
lands in the plausible band — keep the flooded mask and fall to the grid: 37 of
39, and **14 of 14 in September**. That residual is 6.1% of all lots and is the
next detection lever. *Next reading: what the two variants score on those 39.*

**B11 — the flood gate's calibration set.** Mean coverage 0.423 per image,
p90 0.634, 39 over 0.75. Nothing is drifting. The entry's warning (this gate
has overfit its calibration set twice) is the reason to keep it, not a reading.
*Next reading: only when a flood threshold is proposed.*

**B17 — unanchored associations.** 480 images carry Gemini buttons: **3,132
buttons, 2,984 anchored (95.3%), 148 unanchored (4.7%) across 82 lots.** The
anchor gate is live and the 1979-front class is bounded, but 4.7% is the
surface where a blank-bag crop can still be associated. *Next reading: join the
82 lots to their confirmations and check none auto-confirmed.*

**B28 — whitepass telemetry.** Per image at last: **54 lots (8.5%) took the
whitepass rescue** and **83 (13.0%) a saturation fallback**, recovering **117
buttons, median 1 per lot**. September is up on August for both (12.4% vs 6.5%
whitepass; 23.3% vs 6.8% satfallback). The front is one reading from a verdict:
*is the chooser choosing well* needs those 54 lots' outcomes, which the log now
supports. Stage 3 → gradeable now.

**B31 — the fixture battery.** The invariant it asserts holds on production
data: *a non-`scale_first` path never reaches `gate=auto`* — **215 auto lots,
215 `scale_first`, zero on a bailed detector.** The battery is also **25 lots
now, not the 9 the entry names**. What stops it closing is coverage: the entry
claims the battery "covers both detectors" and **ebayscout has no copy**: no
`tests/fixtures/lots/`, no `test_detect_fixtures.py`, and neither repo has a
`.github/workflows/`, so nothing runs it unless a person does. The parity that
claim rests on was verified once, by hand. *Action: either add the fixture
runner to ebayscout or restate the claim as a manual check with a date.*

**A26 — bowl-year resolution.** The entry says "Stage 1, instrumented, zero
data". That is now false: **51 `gemini_printed_year` confirmations landed, all
in September.** The gate's own signal is a boot line — `>>> GAME_YEAR: N
bowl-offset entries indexed` — which a web session cannot read. *Action: one
look at the Cloud Run boot log for that line and its N. If N is non-zero the
front jumps from Stage 1 to Stage 3 on evidence already collected.*

**C5 — the label harvester.** `pipeline/labels/<job_id>.json` + `.jpg` sidecars
live in GCS; this session has no GCP access, so "is every pipeline lot leaving
a labeled example" cannot be answered from here. The code and the
`BUTTONMATCHER_LABEL_HARVEST` kill switch are present in both repos.
*Action: one `gsutil ls | wc -l` against the 637 images in this pool. If the
counts match, C5 is accruing as designed and can be read from the sheet
thereafter.*

---

## 6. Also found

**A17 is a `SHIPPED-WATCH` front the workbook does not know about.**
`LOGGER_FRONTS.md` has drifted between the repos: buttonmatcher's copy carries
70 fronts (B32 is new) and re-statuses A1, A2, A7, A17, A23, A25 and B6;
ebayscout's — the copy `build_goal_trackers.py` reads, and therefore the copy
the workbook was generated from — still has 69 and the old statuses. A17
("Flip `GAP_ONLY` live") became `SHIPPED-WATCH` in buttonmatcher on 2026-09-07
and so was outside this review's 17. Its own entry already names its watch
band (`[0.15,0.20)` reads 99.2% pooled but 94.1% on human truth, n=34).

The two registers should be reconciled in one pass before the next rebuild,
since the workbook cannot be more current than the file it is generated from.

## 7. Order of work

1. Re-run the Apps Script repair so the nine corrected cells in §1 show real
   numbers. Nothing below is readable from the workbook until this happens.
2. ~~Move the six **CLOSE** verdicts into `tested_hypothesis.md` and set those
   entries to `SETTLED-CONFIRMED` / Stage 6, B14 with them.~~ **Done
   2026-09-12** — `tested_hypothesis.md` **Part XIII**, §13.1-13.7, and the
   seven register entries now point at their verdict section. Nothing was
   promoted or retired: `SHIPPED-WATCH` already meant live, and A11's
   unrestricted board in particular must keep running — it is read by
   `_cross_sport_blocked()` and `_augment_results_with_nonfootball()`, so
   stopping it would delete the fix rather than the instrument.
3. Reconcile the two `LOGGER_FRONTS.md` copies (§6).
4. The two operator checks that cost one command each: A26's boot line, C5's
   sidecar count.
5. Decide C4 — fill the shelf deliberately or re-scope the front.
6. ~~B4's instrumentation, then B2's residual 39.~~ **Done 2026-09-12** —
   four columns appended to `match_log` (CJ-CM), no new compute, since both
   fronts were blocked on a number that was already being computed and printed
   to stdout: `det_unguided_band_removed` + `det_unguided_concentric_removed`
   for B4's gate, and `det_satfb_blue_cov` + `det_satfb_bright_cov` for B2's
   residual. B4 returns to `SHIPPED-WATCH`. Both need one normal feed cycle
   before they read — no crawl, no spend. Once B4's counters carry data, switch
   its second LIVE cell off the +1-cluster proxy and onto them.
