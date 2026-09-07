# The fronts — everything we are measuring with the Logger

**What this is.** One entry per *front*: a question we are actively measuring
with the Logger, the instrument that answers it, the gate that closes it, and
where it stands today. It is the index the scattered record never had —
`HYPOTHESES_IN_PROGRESS.md` holds the open experiments, `tested_hypothesis.md`
the settled verdicts, `AUTOMATION_ROADMAP.md` the phases, `log_analysis.md` the
per-batch data, `AUTOMATION_VISION.md` the rollout stages. This file is the one
list that spans all five, so a front cannot be worked twice or forgotten.

**It is not a duplicate of those docs.** Each entry is a pointer plus the six
things you need to know a front's state without opening anything: what is being
asked, what measures it, what would settle it, where it stands, and which doc
carries the full write-up. Read the source before acting on any of it.

**It drives the Progress Trackers workbook.** `build_goal_trackers.py` parses
this file and emits one spreadsheet tab per front, so the workbook cannot say
something this file does not. Add a front here and it gets a tab; change a gate
here and the tab's target changes. Never edit the workbook's structure by hand.

---

## How to read an entry

- **Track** — which half of the system the front belongs to.
- **Status** — see below.
- **Question** — the thing being asked, in one sentence.
- **Instrument** — the Logger column(s), telemetry line, and env kill switch
  that produce the evidence. `—` means the front is not Logger-graded and is
  settled by an operator decision or a code change instead.
- **Gate** — what would confirm or refute it. This is the target the tracker
  tab measures against.
- **Standing** — the most recent measured reading, with its n and its batch.
- **Source** — the doc + section carrying the full write-up.

| Status | Means |
|---|---|
| `OPEN` | being measured now, no verdict |
| `SHADOW` | logging a shadow column; grade it on the next export |
| `PROPOSED` | designed with a gate, not built |
| `BUILT-UNGRADED` | code + flag exist, never A/B'd — grade or delete |
| `SHIPPED-WATCH` | live; watching for regression |
| `BLOCKED` | waiting on data or a decision that does not exist yet |
| `SETTLED-CONFIRMED` | proven and adopted — do not re-litigate |
| `SETTLED-REFUTED` | proven wrong — do not re-propose |
| `DECIDED-HOLD` | deliberately not changing on today's data |

**Two standing directives** (from `HYPOTHESES_IN_PROGRESS.md`, do not skip):

1. **Full-data before any threshold move.** Refresh the pooled band-correctness
   reference across EVERY Logger export before touching
   `AUTO_RESOLVE_THRESHOLD`, `GREEN_THRESHOLD`, `GAP_ONLY` or `SLOGAN_GAP`. One
   batch misleads: L21 alone read [0.82,0.85) as 100%; pooled L16–L21 it is
   96.4%.
2. **Full-data before any full-res / variant call.** Same rule for the match
   shadows (A1, A2) — grade the paired A/B across all Loggers, not one batch.

---

## A. Matching and auto-confirm

### A1 — Full-res match shadow

- **Track:** Matching and auto-confirm
- **Status:** SHADOW
- **Question:** does matching the crop at ≤2200px instead of the ≤800px detection frame produce better #1s or more correct auto-confirms?
- **Instrument:** `fullres_top_json` paired against `restricted_top_json` on the same row; lever `MATCH_FULLRES_SHADOW`; live path stays off via `MATCH_FULLRES=0`
- **Gate:** paired A/B across ALL Loggers after the logging-header fix. Promote only if truth@#1 and net-new correct autos beat ≤800px at scale with zero new wrong autos. Standing expectation: no win.
- **Standing:** leaning REFUTE. Refuted live on Logger_19 (7 lots, same-photo A/B); Logger_21 paired n≈100 — 90/93 identical, −1 truth@#1; headroom ~1 correct auto/run, landing in the same [0.82,0.85) band a threshold drop would reach, so no discrimination.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §A H1; `tested_hypothesis.md` Part IX; `log_analysis.md` "Full-res match SHADOW"

### A2 — Text-variant match shadow

- **Track:** Matching and auto-confirm
- **Status:** SHADOW
- **Question:** do punctuation-normalized variants of hyphen/apostrophe puns (`I-O-Wasn't` → `i o wasnt`) get the off-board `I-O-…` family onto the board or to #1?
- **Instrument:** `variant_top_json` joined to `restricted_top_json` per crop; lever `VARIANT_SHADOW`; `TEXT_VARIANTS=1` would make it live
- **Gate:** across a batch — (a) does a confirmed truth that is OFF the restricted board come ON or to #1 with variants, and (b) does it demote ANY correct #1? Promote only if net-positive with zero correct-#1 demotions.
- **Standing:** NO DATA YET. Built 2026-07-19; needs a batch and the `match_log` tab recreated for the new header. Touches punctuated puns only (`I-O-Was`, `Pitt Isn't It`), not plain-text ones (`'Eers to Penn State`).
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §A H2

### A3 — Auto-confirm score-band reference

- **Track:** Matching and auto-confirm
- **Status:** DECIDED-HOLD
- **Question:** can the auto-confirm score floor (`AUTO_RESOLVE_THRESHOLD` 0.85) be lowered toward `GREEN_THRESHOLD` (0.82) for more autos?
- **Instrument:** every `confirm_log` row's `restricted_top_json` — #1's `overall`, graded slogan-aware against `chosen_year`/`chosen_phrase` (NOT via `rank_restricted`, which carries the year-only bug pre-L20)
- **Gate:** a band is loosenable only when it is clean at pooled scale. This front never closes — it is the standing reference that must be refreshed before ANY threshold move.
- **Standing:** HOLD 0.85. Pooled L16–L21, 2310 crops: ≥0.90 = 98.5%, [0.85,0.90) = 97.3%, **[0.82,0.85) = 96.4%** with 8 wrong-slogan #1s. `Never Badger A Lion` 2001 sits at 0.832 and was a manual pick 3× — the 0.85 floor is exactly what routed it to a human.
- **Source:** `log_analysis.md` "Auto-confirm threshold REFERENCE"; `HYPOTHESES_IN_PROGRESS.md` §A H3

### A4 — Auto-confirm gap-band reference

- **Track:** Matching and auto-confirm
- **Status:** DECIDED-HOLD
- **Question:** can the gap floor (`GAP_ONLY` 0.15) be lowered for more autos?
- **Instrument:** #1→#2 `overall` gap from `restricted_top_json`, graded slogan-aware against the confirmed answer
- **Gate:** same as A3 — refresh pooled across every export before moving. Loosen only on a band that is clean at scale.
- **Standing:** HOLD 0.15. Pooled L16–L21: ≥0.20 = 100.0% (288/288), **[0.15,0.20) = 97.8%** with 8 wrong-slogan, [0.12,0.15) = 95.5%, [0.00,0.05) = 54.9%. The raw gap at 0.15 is not clean either.
- **Source:** `log_analysis.md` "Auto-confirm threshold REFERENCE"

### A5 — No-flip auto-unlock

- **Track:** Matching and auto-confirm
- **Status:** SHADOW
- **Question:** when the visual-final flip does NOT fire (reference agrees with #1), can we auto-confirm at a lower bar than 0.85?
- **Instrument:** shadow line `NOFLIP_UNLOCK_SHADOW: would auto-approve …`; levers `NOFLIP_GAP` (0.05) + `NOFLIP_OVERALL` (0.70). Nothing live.
- **Gate:** grade the would-auto rows across a batch it was NOT tuned on — every would-auto correct, no wrong fire.
- **Standing:** shipped as a shadow on Logger_18 and tuned on that pool, so per the §4.2 rule it must clear a fresh batch before it can ship.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §A H4; `log_analysis.md` Logger_18 "NOFLIP_UNLOCK shipped as a shadow"

### A6 — Mode 1: forcing true #1s higher

- **Track:** Matching and auto-confirm
- **Status:** OPEN
- **Question:** the wrong high-confidence #1s split into Mode 2 (truth on-board, image flips it — no safe auto-fix) and Mode 1 (truth OFF-board because the pun is cold-embedding and a strong generic image wins by default). Can Mode 1 be closed?
- **Instrument:** no single switch — resolves through A2 (variants, for punctuated puns) and C1 (reference curation, for the sticky attractors). Tracked via the FLAGGED wrong-#1 lists.
- **Gate:** do the off-board truths reach the board (A2), and do the attractors stop winning after their references are pruned (C1)?
- **Standing:** diagnosed and pooled L16–L21. The repeat over-promoters are IMAGE attractors at img 0.90–0.98: `Happy 125th Penn State` 1980 ×6, `Penn State and Proud of it` 1992 ×6, `Never Badger A Lion` 2001 ×5.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §A H5; `log_analysis.md` threshold REFERENCE FLAGGED lists

### A7 — Within-year slogan margin

- **Track:** Matching and auto-confirm
- **Status:** SHADOW
- **Question:** every leaderboard is folded to one row per YEAR, so a slogan that loses its own year has no row at all — ~92% of the 498-slogan catalog is off every board on every crop. Score, both gap rules and the visual veto all compare ACROSS years, so all four are structurally blind to a wrong within-year slogan. Can a within-year margin catch that stratum?
- **Instrument:** `within_year_json` — `{year, image_score, n_slogans, runner_up_margin, winner_is_top1, top[5]}`. `runner_up_margin` is the winner's lead over the runner-up inside its OWN year, the number no other column can show.
- **Gate:** join `runner_up_margin` to confirmed truth across a batch. Promote "demote when `runner_up_margin` < M" only if some M catches the wrong within-year picks at an acceptable coverage cost — EVERY button has same-year siblings, so measure autos lost per wrong auto prevented before shipping.
- **Standing:** NO DATA YET; column just added and the operator must extend the `match_log` header by hand. Shipped a wrong AUTO on 2026-09-03 ("'Eers to Penn State" confirmed as same-year "Penn State and Proud of it", margin ~0.001, clearing four gates at once). Control group in hand: a 12-button 1987 board held all four same-year flagged pairs and matched every one correctly, 7 on AUTO — so same-year is not itself predictive; the discriminator looks like SHARED TEXT. Guarded today by a curated allowlist of one pair.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §A H13; `confusable_slogans.py`

### A8 — Low-res auto-confirm guard

- **Track:** Matching and auto-confirm
- **Status:** OPEN
- **Question:** a 104×104 thumbnail that Gemini counted as 11 buttons was auto-confirmed into inventory AND into the reference flywheel. Should a rendered-diameter floor downgrade those to manual review?
- **Instrument:** `det_radius_mean` (×2 for diameter), `det_h`/`det_w`, grid dimensions
- **Gate:** none needed — the defect is fully characterised and the same 9 images recur across Logger_3/4/5. Rule: when rendered button diameter is below ~64px, downgrade `gemini_auto` → manual review and block reference staging. Any sane floor beats none; refine from the radius columns later.
- **Standing:** implementable immediately, no data hole. Not yet shipped.
- **Source:** `AUTOMATION_ROADMAP.md` Phase 4a

### A9 — Reference coverage for 0.00-scoring slogans

- **Track:** Matching and auto-confirm
- **Status:** BLOCKED
- **Question:** which slogans score 0.00 because they have no reference photos at all?
- **Instrument:** `tools/audit_reference_coverage.py` against GCS — needs GCP access, so it runs locally, never from a web session
- **Gate:** one audit run produces the gap list; fill from `reference/_staging/` or manual uploads, then re-run and confirm the 0.00 set shrinks.
- **Standing:** the data exists; the run has not happened. CCB/CCNB slogans are the known offenders.
- **Source:** `AUTOMATION_ROADMAP.md` Phase 4b; `AUTOMATION_VISION.md` §5

### A10 — Measured auto-confirm error rate

- **Track:** Matching and auto-confirm
- **Status:** BLOCKED
- **Question:** what is the actual precision of the auto-confirm path?
- **Instrument:** `correction` / `skip_correction` rows in `confirm_log` — the only source. Nothing else can produce this number.
- **Gate:** ~100 auto-confirms with corrections logged → measure precision directly. ≥95% supports widening auto-confirm, e.g. lowering `auto_sort` toward 0.82 (Logger_5 says 0.82 keeps precision ~0.979 at ~3.5× volume — but verify on measured corrections first). Stage D needs ≥98% over ≥300 confirmations.
- **Standing:** ZERO correction rows have ever been logged. Auto-path precision is inferred, not measured — the only load-bearing number in the system still unmeasured. When an auto-confirm is wrong during normal use, use the correction flow; do not silently fix the sheet.
- **Source:** `AUTOMATION_ROADMAP.md` Phase 4c; `AUTOMATION_VISION.md` §4 Stage D, §6.4

### A11 — Football pre-filter split

- **Track:** Matching and auto-confirm
- **Status:** SHIPPED-WATCH
- **Question:** should the football restriction be dropped, kept, or split into unfiltered suggestions plus a gated auto-confirm?
- **Instrument:** `shadow_top_json` vs `restricted_top_json`; `chosen_type`; the shadow #1's sport
- **Gate:** the auto-confirm half is shipped — score-only AUTO is blocked whenever the shadow #1 is a different, non-Football candidate. If misses persist because the shadow #1 stays football, widen to "any non-football candidate in shadow top-3 outscoring the football top".
- **Standing:** measured on Logger_2's 2,590 rows — unfiltered shadow #1 agrees with restricted #1 91%; a non-football candidate takes #1 on ~15% of crawl crops but only 0.6% at ≥0.85. Decision to keep-as-is was overtaken the same day by a basketball lot auto-confirming as football twins (5 wrong AUTOs), which forced the auto-confirm half. The unfiltered-suggestions half remains open.
- **Source:** `AUTOMATION_ROADMAP.md` Phase 4d; `AUTOMATION_VISION.md` §4 parallel track

### A12 — Per-slogan text-baseline centering

- **Track:** Matching and auto-confirm
- **Status:** SETTLED-REFUTED
- **Question:** slogans carry a de-facto CLIP text advantage independent of the crop (per-phrase background text_score spans 0.34–0.80 across 515 phrases). Does centering each slogan's cosine on its own reference-bank baseline place truths at #1 more often?
- **Instrument:** `rank_centered` vs `rank_restricted` in `confirm_log`
- **Gate:** centered must place confirmed truths at #1 at least as often as raw AT SCALE, including truths raw ranking leaves off-board — and only together with a recalibration of every score threshold.
- **Standing:** REFUTED at n=478 (Logger_18): 19 better / 65 worse, 423→386 truths at #1. The baseline "advantage" encodes a real prior. Logger_16 first read was already negative at n=50. The column keeps logging for the record; the live formula is unchanged. Do not re-propose.
- **Source:** `AUTOMATION_ROADMAP.md` Phase 4e; `tested_hypothesis.md` Part VI layer 3

### A13 — Reference-shelf completion, log-targeted

- **Track:** Matching and auto-confirm
- **Status:** OPEN
- **Question:** which slogans' reference shelves, if filled, would widen the most gaps? A gap is (crop vs the right entry's evidence) − (crop vs the runner-up's); references raise only the first term.
- **Instrument:** from each export, every slogan that won with gap < 0.15 or lost at ranks 2–5, read off `restricted_top_json`
- **Gate:** fill those shelves to the 4-cap first, then confirm the low-gap offender list shrinks on the next export. Likely 30–50 slogans do most of the damage.
- **Standing:** proven as the dominant lever — "Stuck in a Rut" went rank-31 → rank-1 in four days purely from confirms staging references (Logger_12, Jul 4→11).
- **Source:** `AUTOMATION_ROADMAP.md` Gap-widening Track 1 item 1

### A14 — Calibrate REF_CHECK_WEIGHT

- **Track:** Matching and auto-confirm
- **Status:** OPEN
- **Question:** is the centered ref-photo check weighted right at 0.15? It is the score's one explicitly contrastive term, so it widens exactly the correct-vs-wrong gap where references exist.
- **Instrument:** `ref_sim` telemetry on confirmed outcomes — only flowing since the 2026-07-09 fix, so the first post-fix export is the first calibration opportunity
- **Gate:** calibrate on real outcomes from one export; compounds with A13.
- **Standing:** not yet calibrated. Note `ref_sim` as logged is 74% identical to `image_score` (median |Δ| 0.0003) — it is not an independent axis on most rows.
- **Source:** `AUTOMATION_ROADMAP.md` Gap-widening Track 1 item 2

### A15 — TTA on low-gap crops

- **Track:** Matching and auto-confirm
- **Status:** PROPOSED
- **Question:** compressed scores often mean degraded crops (blur, small, glare flatten every sim). Does test-time augmentation widen gaps when targeted at low-GAP crops rather than low-SCORE crops?
- **Instrument:** existing TTA machinery (`BUTTONMATCHER_TTA`, tight re-crop + rotations), currently aimed at sub-0.65 crops; measure gap change on confirmed rows
- **Gate:** one export answers it — does it widen gaps on confirmed rows?
- **Standing:** not run. Distinct from D1, which is the same machinery aimed at its original low-score target.
- **Source:** `AUTOMATION_ROADMAP.md` Gap-widening Track 1 item 3

### A16 — Confusable-pair registry

- **Track:** Matching and auto-confirm
- **Status:** PROPOSED
- **Question:** some pairs never widen (the 0.968 wrestling pin; Slash/Trash the Flash; Mellon-era templates). Can they be enumerated from logs and quarantined, so everyone else's small gaps become safe to trust?
- **Instrument:** any two entries that ever swapped ranks on a confirmed row, mined from `restricted_top_json` across exports
- **Gate:** quarantine score-only autos within known pairs (as twin families already route to the picker), then show the remaining population's small gaps are clean — the eventual justification for lowering `GAP_ONLY` below 0.15 for everyone else.
- **Standing:** not built. Generalises the existing twin registry.
- **Source:** `AUTOMATION_ROADMAP.md` Gap-widening Track 2 item 4

### A17 — Flip GAP_ONLY live

- **Track:** Matching and auto-confirm
- **Status:** OPEN
- **Question:** can `BUTTONMATCHER_GAP_ONLY_LIVE=1` be flipped? It is built, with zero new engineering, for ~3× score-based auto coverage.
- **Instrument:** the `gap ≥ 0.15` rule graded against confirmed truth
- **Gate:** one clean shadow batch.
- **Standing:** 537/537 correct on the validated population; three independent validations totalling 452/452 cumulative (Logger_11 108, Logger_12 122, Logger_14 222). Named as the next-week lead action.
- **Source:** `AUTOMATION_ROADMAP.md` Gap-widening Track 2 item 5; `log_analysis.md` Logger_14 ladder rung C

### A18 — The signal ladder

- **Track:** Matching and auto-confirm
- **Status:** SHIPPED-WATCH
- **Question:** how much of `gemini_auto`'s coverage can score-side rules take over without a wrong slogan?
- **Instrument:** cumulative union of the rules graded on 731 rows — `overall ≥ 0.85` → `gap ≥ 0.15` → `slogan_gap ≥ 0.12` → `slogan_gap ≥ 0.05 AND ref_sim ≥ 0.90`
- **Gate:** each rung must hold 0 wrong slogans on the batch that adopts it, and clear a fresh batch before the next step-down.
- **Standing:** Logger_14, 0 wrong at every rung — A alone 23.3% of `gemini_auto`, A∪C 43.8%, ∪S 52.6%, ∪R 68.9%. `slogan_gap` 0.12 shipped with a 0.024 cushion; 0.10 held as the step-down after one more clean batch (its margin was 0.004 — one batch of luck).
- **Source:** `log_analysis.md` Logger_14 "the ladder"; `tested_hypothesis.md` Part V

### A19 — ref_sim as an absolute visual-mismatch veto

- **Track:** Matching and auto-confirm
- **Status:** OPEN
- **Question:** does entry-level visual similarity separate right from wrong matches at some absolute threshold, so it can veto a mismatch outright?
- **Instrument:** `ref_sim` per offered option, on confirmed outcomes
- **Gate:** separation at some threshold with ≤2% miss. This is the Stage C entry condition.
- **Standing:** YES as a second signal, NO alone. Truth-#1 rows read ref_sim ~0.873 median vs 0.723 wrong in the low-gap stratum, and the two-signal combo adds +42 confirms with 0 wrong — but the lowest zero-wrong solo threshold is 0.890 against a wrong row at 0.888, a 0.002 margin that is noise. Year-level sims overlap too much (0.81 vs 0.86 medians).
- **Source:** `AUTOMATION_VISION.md` §5, §6.3; `log_analysis.md` Logger_14 ladder rung R

### A20 — Visual-final flip as a resolver

- **Track:** Matching and auto-confirm
- **Status:** SETTLED-REFUTED
- **Question:** when the best-reference candidate differs from #1, should the flip be *acted on* — i.e. commit the reference's pick?
- **Instrument:** the visual-final shadow on 473 reviewed rows (Logger_18)
- **Gate:** the flip had to fix more than it broke.
- **Standing:** REFUTED as a resolver — 8 fixes vs 37 wrong commits; `inter_sim` does not separate (Never/Too Badger reads 0.913). SHIPPED INVERTED as the `VISUAL_VETO`: the disagreement withholds an auto instead of resolving it. There is no safe auto-fix for Mode 2. Do not re-propose.
- **Source:** `tested_hypothesis.md` Part V; `HYPOTHESES_IN_PROGRESS.md` settled list

### A21 — ref_combo

- **Track:** Matching and auto-confirm
- **Status:** SETTLED-REFUTED
- **Question:** is `slogan_gap ≥ 0.05 AND ref_sim ≥ 0.90` safe as a live auto-confirm rule?
- **Instrument:** the combo graded on Logger_15's 256 reviewed rows
- **Gate:** zero wrong on the batch that would adopt it.
- **Standing:** REFUTED at these thresholds — one wrong on its first batch, the 0.986 A&M/Voluntears twin. Kept as a permanent shadow.
- **Source:** `tested_hypothesis.md` Part V "Validated on Logger_15"; `HYPOTHESES_IN_PROGRESS.md` settled list

### A22 — Near-twin auto suppression

- **Track:** Matching and auto-confirm
- **Status:** PROPOSED
- **Question:** distinct-slogan mispicks inside a shared look/word family (`Attack The Pack` vs `Sack The Pack`, `Hoot On Temple` vs `Temple Whoo?`) produce NO visual flip, because best-ref equals #1 for both siblings — so the veto never fires. Can top-2 phrase similarity suppress the auto instead?
- **Instrument:** proposed shadow line `NEAR_TWIN_SUPPRESS_SHADOW`; kill switch `BUTTONMATCHER_NEAR_TWIN_SUPPRESS`; reuses the CLIP text embeddings already in hand
- **Gate:** shadow first; calibrate `NEAR_TWIN_SIM` so it withholds the near-twin autos without touching the validated `gap_only` (537/537) and `slogan_gap` (448/448) populations.
- **Standing:** all observed cases were human `pick` corrections, so no auto-error yet — the risk is `slogan_gap`/`gap_only` committing the wrong sibling with no human in the loop. It only withholds; it never flips #1→#2.
- **Source:** `log_analysis.md` Logger_20 class E

### A23 — Typed puns are image-recoverable

- **Track:** Matching and auto-confirm
- **Status:** SHADOW
- **Question:** for the typed-pun crops, does image similarity alone rank the truth better than the live blend?
- **Instrument:** `rank_image_only` vs `rank_restricted` in `confirm_log`
- **Gate:** grade slogan-aware (the rank columns carry a year-only bug pre-L20) and decide whether an image-weighted rescue is worth shipping.
- **Standing:** directionally persistent across all five runs — `rank_image_only` places the typed-pun truth at #1 at least as often as the live blend in EVERY run (L16 3·2, L17 5·3, L18 9·7, L19 6·6, L20 11·9). L20's clean slogan-aware read: 6 of 16 typed had image_only strictly better. Real but modest — a candidate lever, shadow-first. Centering (A12) is NOT the lever here.
- **Source:** `log_analysis.md` Logger_20 class C, cross-log trends

### A24 — The bank picker's cost

- **Track:** Matching and auto-confirm
- **Status:** OPEN
- **Question:** is the era/bank picker costing more clicks than it saves?
- **Instrument:** `bank`, `source` and pick rates across `/sort` + `/inventory` confirmations
- **Gate:** operator decision once the click economics are measured across a full export.
- **Standing:** measured across Loggers 16–18 as costing more clicks than it saves.
- **Source:** `log_analysis.md` Logger_18 "The bank picker"

### A25 — Edition-twin wrong-year picks

- **Track:** Matching and auto-confirm
- **Status:** DECIDED-HOLD
- **Question:** 17 `edition_pick` rank>1 rows are same-slogan wrong-year twins. Should the picker be changed?
- **Instrument:** `source='edition_pick'` rows with `rank_restricted` > 1; `edition_shadow_json`
- **Gate:** none — operator decision 2026-07-18: the edition-picker interaction is working as intended; the picker exists precisely for these and the click is acceptable.
- **Standing:** stable at ~1.2–1.9% of confirms, no drift. `printed_year` adoption is eating into the picker cost on its own. Not pursued.
- **Source:** `log_analysis.md` Logger_20 "Not pursued: D"

### A26 — Bowl-year resolution

- **Track:** Matching and auto-confirm
- **Status:** SHIPPED-WATCH
- **Question:** a bowl button prints the game year (season+1). Can its printed marker be resolved to the SEASON year via `game_date`?
- **Instrument:** telemetry `n_printed_year_gamematch`; `TWIN GUARD … via game_date (bowl offset)`; lever `GAME_DATE_YEAR` (default on)
- **Gate:** `>>> GAME_YEAR: N bowl-offset entries indexed` on boot, with N ≈ the count of Jan-dated football buttons; then bowl twins resolve to the season year with non-zero `n_printed_year_gamematch` and no new wrong twins.
- **Standing:** merged 2026-07-19. Data goes live once the dated `text_db.json` is uploaded and deployed.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §A H9

---

## B. Detection

### B1 — Instrumentation completion

- **Track:** Detection
- **Status:** SETTLED-CONFIRMED
- **Question:** are the three logging holes that block every other detection front closed — true unguided count on the pipeline, Hough-param telemetry on ebayscout's detector, and a count-free over-merge signal?
- **Instrument:** `det_count_noinput` + the `ni_*` block; `det_hough_*` + `det_rej_radius_*`; `det_mask_blobs_raw` / `det_dt_peaks_total` / `det_mask_coverage`
- **Gate:** every new column populated on every row of a real batch.
- **Standing:** merged (PRs #43/#104), deployed, validated on the 300-lot run — 298/300 lots processed, every new column populated, 265 lots with a usable Gemini count. 37 match_logging tests pass per repo.
- **Source:** `AUTOMATION_ROADMAP.md` Phase 1

### B2 — Mask saturation (defect C)

- **Track:** Detection
- **Status:** SHIPPED-WATCH
- **Question:** on lots where the mask floods (coverage > 0.75), buttons and background fuse into one sheet and Hough finds nothing. Can a blue-only plus bright two-variant chooser recover them?
- **Instrument:** `det_mask_coverage`, `det_mask_path`, `det_detector_used`
- **Gate:** guided detection engages instead of falling to the grid, on the real failed lots.
- **Standing:** merged and deployed. Saturation was 19.2% of the Logger_4 pool and devastating inside it — guided exact 19.6% vs 64.5% on normal masks, 62.7% grid-fallback. After the fix: a real 35-lot batch went 35/35 guided (was grid-fallback), a 26-lot batch 23/23 blue, the white-8 lot recovered. The biggest single detection lever.
- **Source:** `AUTOMATION_ROADMAP.md` Phase 2a

### B3 — Fused-lot collapse (defect A)

- **Track:** Detection
- **Status:** OPEN
- **Question:** on lots where buttons touch and fuse into one mask blob, can Hough be re-run at a DT-corrected radius to split them?
- **Instrument:** `det_mask_components` < `gemini_button_count` (the fusion signature); `det_dt_peaks_total` as the radius source
- **Gate:** adopt when `det_dt_peaks_total` is within ±1 (or ±10% on 13+ button lots) of Gemini's count on ≥80% of fused lots. Needs ~100 pipeline lots including ~20 with 7+ buttons; dense `/sort` lots are the fastest gold-standard source. If it passes, the blob-split half can ship shadow-first for one more batch before switching live.
- **Standing:** largely covered by B2 — saturation was the fusion driver in both real lots. The design was revised: DT peaks are the radius/fusion signal, not the counter.
- **Source:** `AUTOMATION_ROADMAP.md` Phase 2b

### B4 — Small-lot overcount (defect B)

- **Track:** Detection
- **Status:** SHIPPED-WATCH
- **Question:** 68% of singles overcount unguided, and the largest cluster is exactly +1 — the concentric glare rim. Does a radius-consistency band plus concentric collapse fix it?
- **Instrument:** `det_overlap_removed`, `det_radius_*`, `ni_selected` vs truth
- **Gate:** a dedup rule that removes ≥80% of the spurious extras on the collected overcount set while removing **zero** circles on exact-match lots. Needs ~50 overcounted small lots ≈ ~330 single-button pipeline lots at the ~15% rate — 2–4 weeks of normal feed, no action or cost required.
- **Standing:** merged and deployed — a 0.7–1.3× median band plus concentric collapse (keep better fill). The defect was 69 of 216 singles and is uncorrelated with saturation (mean coverage ≈0.50 in both groups), so it is an independent defect with its own fix.
- **Source:** `AUTOMATION_ROADMAP.md` Phase 3

### B5 — Tighten the auto gate

- **Track:** Detection
- **Status:** SETTLED-CONFIRMED
- **Question:** `ni_gate=auto` could survive even when the guided detector bailed to grid or Gemini-led, so the unguided shadow numbers described a detector that was never used. Does requiring `scale_path=scale_first` AND a non-bailed detector close the loophole?
- **Instrument:** `ni_gate`, `ni_scale_path`, `det_detector_used`; `demote_auto_on_detector_bailout`
- **Gate:** gated shadow-vs-truth disagreement on the organic feed.
- **Standing:** merged and deployed (#116/#50). Post-patch disagreement measured **0%**, was 2.6% with the loophole open. Validated at n=329: 96% exact / 100% ±1.
- **Source:** `AUTOMATION_ROADMAP.md` Phase 3.5; `tested_hypothesis.md` Part II

### B6 — Gemini-x/y crop anchoring

- **Track:** Detection
- **Status:** OPEN
- **Question:** should crops be anchored on Gemini's x/y instead of Hough's centres, fixing the grid-fallback mis-centring?
- **Instrument:** `det_gemini_anchored_json` — `{n_agree, snap_frac_median, n_gemini_only, n_hough_only}`
- **Gate:** revisit only gated on low `snap_frac` AND `n_agree ≈ n_gemini` — and there, by definition, there is little left to fix. Otherwise formally drop it.
- **Standing:** LEANING REFUTE as a blanket anchor, graded 2026-07-19 on 99 Gemini lots pooled L16/17/18/20/21. 70% of lots are already perfect (Hough == Gemini) with `snap_frac_median` 0.072 (92% ≤ 0.25) — safe but nothing to gain. The ~30% where anchoring would move something is dominated by lots where Gemini is UNreliable (agree=0 lots where Gemini read 1 button and Hough 23; frame-distortion lots at snapf 0.764). The outcome join is the tell: of 24 lots where anchoring would ADD a "missed" Gemini button, only 2 had a human `missed_button`. The useful DROP half folds into B7.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §B H6; `tested_hypothesis.md` §4.8

### B7 — On-mask phantom dup-drop

- **Track:** Detection
- **Status:** PROPOSED
- **Question:** the two-signal reconcile swap is dormant — it fired on ~1 of 78 lots — because on-mask phantoms sit on bluish pixels and score fill ≥ 0.50, so the off-mask gate keeps them. Can a third signal identify them without endangering a solo real button?
- **Instrument:** proposed `RECONCILE_DUP_SHADOW would-drop` line + `det_n_dup_dropped`; kill switch `BUTTONMATCHER_RECONCILE_DUP_DROP`. Corroborating signals already logged: `n_hough_only` from the anchor shadow and `det_gem_unmatched_json`.
- **Gate:** shadow-log would-drop circles for one cycle and grade against `not_a_button` confirmations on `(job_id, crop_num)` before dropping anything. Precision target: ≥ the 9/21 (~43%) `n_hough_only` ↔ `not_a_button` coincidence, ideally higher once off-mask/overlap gated. Hard-gate on `gemini_count>0` + over-count + overlap-with-kept so a real button Gemini merely missed is never dropped.
- **Standing:** the best-evidenced open item and the one that should lead. Persistent across the whole observable history: pooling L16–L20, 18 lots carried unmatched phantom-candidates and the swap fired on 2 — it addresses ~11% of the population it exists for. The third signal is max overlap/IoU against the KEPT circles: a solo button Gemini missed does not overlap another circle; a duplicate or split does.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §B H7; `log_analysis.md` Logger_20 §A1

### B8 — Count-padding uncertain tag

- **Track:** Detection
- **Status:** PROPOSED
- **Question:** on no-Gemini slash lots, when `det_count_user − det_count_noinput` is large, the padding manufactures non-buttons at the tail. Should those crops be tagged uncertain instead of presented as confident?
- **Instrument:** proposed `det_n_uncertain_pad`; kill switch `BUTTONMATCHER_UNCERTAIN_PAD`. Signature is `det_n_crops == det_count_user` while unguided Hough saw far fewer.
- **Gate:** the tagged crops should coincide with `not_a_button` confirmations, and must never hide a real faint button. Fire only on large gap (start `gap ≥ 3` or `≥ 0.25×expected`) plus low fill.
- **Standing:** the last crop of a lot is `not_a_button` on 5 of 64 lots = 8%, against a 1.9% base rate — padding lands at the tail to hit the number. Needs a shadow cycle to earn the demotion. Do not change the user count's role in radius calibration.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §B H8; `log_analysis.md` Logger_20 §A2/B

### B9 — White and light-background detection

- **Track:** Detection
- **Status:** OPEN
- **Question:** the bright/white mask arm floods on light backgrounds (white mailer, gray felt), collapsing Hough into grid fallback or radius collapse. What fixes it?
- **Instrument:** `det_mask_path`, `det_detector_used`, `det_count_noinput`; existing `white_rescue` / grid-hole force-fill / `+bgdiff`; a proposed saturation gate on the white mask arm
- **Gate:** targeted re-run of the known light-bg photos with `+bgdiff` on/off and, once built, the saturation gate. Pair with Gemini runs for the anchoring angle.
- **Standing:** **85% of human-touch failures sit on light/white/flooded lots.** Diagnosed on the Logger_20 wrestling board (`c31f77e1`) and `b204bf`; no single switch yet.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §B H10; `log_analysis.md` Logger_20 case study

### B10 — Grid-hole force-fill

- **Track:** Detection
- **Status:** SHIPPED-WATCH
- **Question:** white-on-white detection is resolution-fragile, but is the grid geometry itself resolution-independent — i.e. can a hole in the grid be force-filled even when the button is invisible to the mask?
- **Instrument:** `ni_est_rows` / `ni_est_cols` / `ni_layout_conf`; the Mildcats/Minnesota lot
- **Gate:** the grid gap must be a stable signal across resolutions.
- **Standing:** CONFIRMED and SHIPPED 2026-07-18, both repos. The gap is resolution-independent where detection is not.
- **Source:** `tested_hypothesis.md` Part VIII

### B11 — The flood gate's own calibration set

- **Track:** Detection
- **Status:** SHIPPED-WATCH
- **Question:** the flood gate was tuned on a calibration set that did not contain dense lots — and a dense lot IS a flooded mask. Does it reject good masks?
- **Instrument:** `det_mask_coverage`, `det_mask_path`
- **Gate:** an independent on-target check, same §4.2 shape — gate the new path, verify it is on-target, fall back.
- **Standing:** FIXED 2026-07-15. Note this gate has now overfit its calibration set **twice** (again in §4.11, the projection-strips failure) — treat any new flood threshold as suspect until it clears a set it was not tuned on.
- **Source:** `tested_hypothesis.md` §4.9, §4.11

### B12 — Hole-inversion ate a good mask

- **Track:** Detection
- **Status:** SHIPPED-WATCH
- **Question:** slogan-text blocks inside a button read as "button holes" to the hole-inversion step, destroying the mask. Can a coverage floor stop it?
- **Instrument:** `det_mask_coverage`; `HOLE_INVERT_MIN_COVERAGE = 0.08`
- **Gate:** the kept mask must clear the floor before inversion is trusted.
- **Standing:** FIXED and SHIPPED 2026-07-15, both repos.
- **Source:** `tested_hypothesis.md` §4.10

### B13 — Gemini coordinate scale

- **Track:** Detection
- **Status:** SETTLED-CONFIRMED
- **Question:** what actually caused the "navy-8 complete fail"?
- **Instrument:** `parse_gemini_response` (byte-shared); the reconcile geometry
- **Gate:** root cause reproduced on the real photo.
- **Standing:** FIXED. The cause was a coordinate SCALE mismatch — 0-100 vs 0-1000 — not a detection failure. A reminder that the recurring error class is an unstated frame/scale assumption, not a bad model.
- **Source:** `tested_hypothesis.md` §4.5

### B14 — Two-signal reconcile swap

- **Track:** Detection
- **Status:** SHIPPED-WATCH
- **Question:** a Hough phantom was SUPPRESSING a real Gemini button. Can the phantom be dropped without ever dropping a real button Gemini merely missed?
- **Instrument:** `det_n_swapped`, `det_reconcile_swaps_json`; the two signals are unbacked AND off-mask (`detected_fills < SWAP_OFF_MASK_MAX` 0.50)
- **Gate:** the risky action is DROPPING, so it needs two independent signals; generalised so the drop does not require a button to recover in its place.
- **Standing:** FIXED and SHIPPED. Working as designed — but dormant in practice (fires on ~1 of 78 lots), which is exactly what B7 exists to extend.
- **Source:** `tested_hypothesis.md` §4.6

### B15 — Deficit-fill over-trust

- **Track:** Detection
- **Status:** SHIPPED-WATCH
- **Question:** the turf-cross regression — deficit fill trusted a bad path and manufactured detections. Gate it?
- **Instrument:** the gate + on-target check; `det_detector_used`
- **Gate:** gate the new path, verify it is on-target, fall back — the reusable lesson from this front, now applied to every subsequent detection fix.
- **Standing:** CONFIRMED and fixed. One part remains **still open**: the gate restores the prior behaviour rather than solving the underlying trust question.
- **Source:** `tested_hypothesis.md` §4.1, §4.2

### B16 — Carpets

- **Track:** Detection
- **Status:** DECIDED-HOLD
- **Question:** should the flood gates be extended to cover textured-carpet backgrounds?
- **Instrument:** `det_bg_*`, `det_mask_coverage`, `det_edge_density`
- **Gate:** operator decision 2026-07-12 — carpets are too niche to over-code. DASH the gates.
- **Standing:** decided. A companion shipped instead (both repos): small, broad-value, and a free data engine.
- **Source:** `tested_hypothesis.md` §4.3, §4.4

### B17 — Unanchored associations

- **Track:** Detection
- **Status:** SHIPPED-WATCH
- **Question:** the 1979-front incident mass-auto-confirmed blank-bag crops. Does physical anchoring separate right from wrong associations?
- **Instrument:** the anchor gate; `det_gemini_anchored_json`; per-crop provenance
- **Gate:** the association must be backed by a physical anchor before it can auto-confirm.
- **Standing:** CONFIRMED on the real detector from the raw photo. Anchor-gated recovery SHIPPED 2026-07-17 as the swap's flooded-mask replacement. The incident class is ~14% of pipeline lots and two casualties went unflagged. **Still open** items remain in the write-up.
- **Source:** `tested_hypothesis.md` Part VII; `log_analysis.md` Logger_18 second pass

### B18 — Frame fit

- **Track:** Detection
- **Status:** SHIPPED-WATCH
- **Question:** in the DUAL incident ("1987 front", job `efb99c29`) Gemini's frame stretched. Should the coordinate FRAME be reconciled before any position is trusted?
- **Instrument:** the frame-fit step ahead of position reconciliation
- **Gate:** frame agreement before position agreement.
- **Standing:** SHIPPED 2026-07-17.
- **Source:** `tested_hypothesis.md` Part VII

### B19 — Layer-1 radius robustness

- **Track:** Detection
- **Status:** OPEN
- **Question:** radius/scale — not Hough — is the real bottleneck. Can `sweep_fallback` lots (banding, saturation, blue-on-blue) get a trustworthy radius?
- **Instrument:** `ni_r_est`, `ni_scale_conf`, `ni_scale_path`, `det_expected_radius`
- **Gate:** the hard research track. Self-bootstrapping wide-radius rim Hough was tried and is NOT a clean win (multimodal radius: logos + arcs + rims). Earn a learned segmenter with data first.
- **Standing:** the residual error inside `scale_first` and the blocker under B20. Radius collapse (`sweep_fallback`) is the shared root cause of every hard lot.
- **Source:** `tested_hypothesis.md` Part I §3.2, §6.3; `AUTOMATION_VISION.md` §2

### B20 — Rim-support union pass

- **Track:** Detection
- **Status:** BLOCKED
- **Question:** does the rim-support union pass recover white-on-white, colour-blind?
- **Instrument:** the Layer-2 union pass; `det_white_recovered`
- **Gate:** re-run against all 9 fixtures behind a kill switch, with the contained-fragment dedup fix, once radius is trustworthy.
- **Standing:** CONFIRMED in principle — recovers white-on-white with 0 false positives across 5 backgrounds. Deliberately NOT implemented: blocked on B19 (Layer-1 radius) and owes the dedup fix.
- **Source:** `tested_hypothesis.md` Part I §2, §5

### B21 — DT peaks as a counter

- **Track:** Detection
- **Status:** SETTLED-REFUTED
- **Question:** do `dt_peaks` / `mask_blobs` give a usable count-free estimate?
- **Instrument:** `det_dt_peaks_total`, `det_mask_blobs_raw` vs `gemini_button_count`
- **Gate:** usable accuracy against truth at scale.
- **Standing:** REFUTED. 12% exact on the 25 fused lots, over-splits everywhere; median error 7–21 depending on bucket, confirmed at larger n. They remain the RADIUS/fusion signal only. Do not re-propose as a counter.
- **Source:** `tested_hypothesis.md` Part I §4; `AUTOMATION_ROADMAP.md` status update 2026-07-09

### B22 — The placement blind spot

- **Track:** Detection
- **Status:** SHADOW
- **Question:** a count gate cannot see a misplaced circle or a non-button object — the operator's actual dominant error mode. Can it be measured per lot?
- **Instrument:** `det_gem_unmatched` + `det_gem_unmatched_json` — the Hough-only unmatched circles `plan_reconciliation` already computes and used to discard
- **Gate:** a passive per-lot placement metric that accrues alongside the Stage-B count gate. Blank means the match could not run (unknown), which is not zero.
- **Standing:** instrumented. This is the blind spot that must be instrumented **before** Stage B flips.
- **Source:** `AUTOMATION_ROADMAP.md` Phase 5; `AUTOMATION_VISION.md` §4 Stage B, §6.1

### B23 — Per-button review taps

- **Track:** Detection
- **Status:** SHIPPED-WATCH
- **Question:** count-exact ≠ button-exact — detection can find the right number of circles while one is a non-button and one button is missed (lot `1855dcee`). What catches that?
- **Instrument:** `not_a_button` / `missed_button` taps in `confirm_log.source`
- **Gate:** each tap is also a labeled training example for the learned-detection track, so the front is as much about accrual as about rate.
- **Standing:** stable and normalised — `not_a_button` ~1.5–2.5%, no drift across L16–L20. `missed_button` exploded in Logger_18 from the same root cause as the 1979-front incident.
- **Source:** `AUTOMATION_ROADMAP.md` status update 2026-07-09; `log_analysis.md` Logger_18 second pass

### B24 — Coin and round-clutter false positives

- **Track:** Detection
- **Status:** OPEN
- **Question:** does round non-button clutter at a *correct* radius produce false positives the radius filters cannot catch?
- **Instrument:** `det_gem_unmatched_json`, `not_a_button` taps
- **Gate:** needs images — latent and untested.
- **Standing:** listed as pending in the Part I record; no data yet.
- **Source:** `tested_hypothesis.md` Part I §4 Pending

### B25 — The fusion density boundary

- **Track:** Detection
- **Status:** OPEN
- **Question:** at what button-count density does `scale_first` start fusing?
- **Instrument:** `det_mask_components` vs `gemini_button_count`, bucketed by count; `det_buttons_per_megapixel`
- **Gate:** needs dense (7+) lots, the acknowledged bottleneck — daily-feed lots are mostly singles.
- **Standing:** pending. The highest-value manual data ask is dense lots through `/sort` with a typed count.
- **Source:** `tested_hypothesis.md` Part I §4 Pending; `AUTOMATION_ROADMAP.md` cheat sheet

### B26 — The automatable volume share

- **Track:** Detection
- **Status:** SETTLED-CONFIRMED
- **Question:** what fraction of pipeline lots are `scale_first`, i.e. how much of the feed is even eligible for unguided automation?
- **Instrument:** `ni_scale_path` share of the organic feed
- **Gate:** answered.
- **Standing:** **~33% of the organic feed** (answered 2026-07-08). Volume, not accuracy, is the constraint on Stage B.
- **Source:** `tested_hypothesis.md` Part I §4, Part II §3

### B27 — Grid-fallback rate

- **Track:** Detection
- **Status:** SHADOW
- **Question:** grid fallback spiked in Logger_20 (0%→5%→5%→0%→**19%**, 15 lots, 14 Gemini-backed). Is it becoming a failure driver?
- **Instrument:** `det_detector_used`, `det_mask_coverage`
- **Gate:** watch whether the rate keeps climbing, and whether the non-flooded grid lots have a distinct trigger.
- **Standing:** the spike did NOT worsen outcomes — grid-lot `rank_restricted>3` was 5% vs 6% on Hough lots, and `gemini_auto` covers them. Only 6 of 15 were flooded-mask; the rest fell to grid for other reasons. Likely a workload shift. Not currently a failure driver, so scope any fix as a targeted lot-level rescue rather than a fleet-wide change.
- **Source:** `log_analysis.md` cross-log trends

### B28 — Whitepass telemetry

- **Track:** Detection
- **Status:** SHIPPED-WATCH
- **Question:** the guided white-rescue pass was invisible. How often does it fire and what does it recover?
- **Instrument:** `det_mask_path += "+whitepass"` and `det_white_recovered` (a trailing column to hand-append)
- **Gate:** watch `+whitepass` and `+satfallback_*` frequency on the same exports — is the chooser choosing well, and is the rim rescue real?
- **Standing:** shipped (buttonmatcher #118 / ebayscout #51). The white-on-white rescue is measurable instead of invisible.
- **Source:** `AUTOMATION_ROADMAP.md` status update 2026-07-09; `AUTOMATION_VISION.md` §6.1

### B29 — The bright-variant fallback

- **Track:** Detection
- **Status:** SETTLED-REFUTED
- **Question:** does the bright `V > bg+60` variant catch white-on-white?
- **Instrument:** `ni_variant`, `det_mask_path`
- **Gate:** measurable coverage on white-on-white lots.
- **Standing:** REFUTED — 0.000 coverage. Do not re-propose.
- **Source:** `tested_hypothesis.md` Part I §4 Refuted

### B30 — The Hough acceptance floor

- **Track:** Detection
- **Status:** SETTLED-CONFIRMED
- **Question:** Hough engaged on 0% of 1–3 button lots, yet its pass-1 nailed the single button 77% of the time. Was the acceptance floor of 6 discarding correct detections?
- **Instrument:** `det_hough_pass1` vs `gemini_button_count`, bucketed by lot size
- **Gate:** accept Hough when it has enough circles to form a grid (≥6) OR when it has essentially found the expected few-button count.
- **Standing:** CONFIRMED and shipped; live-validated post-change on ebayscout `/crawl-pipeline` 2026-06-20→22. The old floor forced every ≤5-button image onto the projection-grid fallback.
- **Source:** `log_analysis.md` Logger_5 "Root cause: the Hough-acceptance floor of 6"

### B31 — The fixture regression battery

- **Track:** Detection
- **Status:** SHIPPED-WATCH
- **Question:** is there anything stopping a detection change from silently regressing the known-hard lots?
- **Instrument:** `tests/fixtures/lots/` — 9 real lots at 800px + `manifest.json` + `test_detect_fixtures.py`
- **Gate:** locks the unguided snapshot, guards the two clean lots at `auto` + exact, and asserts the Layer-1 safety property — *a non-`scale_first` path never reaches `gate=auto`*.
- **Standing:** committed in buttonmatcher; `ebayscout/detect_pipeline.py` produces identical counts on all fixtures (parity verified), so the battery covers both detectors.
- **Source:** `tested_hypothesis.md` Part I §5; `AUTOMATION_VISION.md` §3

---

## C. Reference and data

### C1 — Reference curation for sticky attractors

- **Track:** Reference and data
- **Status:** OPEN
- **Question:** a handful of reference photos over-match unrelated crops on IMAGE. Does pruning them stop the wrong #1s?
- **Instrument:** the pooled FLAGGED list of ≥0.75 wrong-#1s per reference
- **Gate:** prune, re-shoot or down-weight those references, then confirm each one's wrong-#1 count drops in the next pooled refresh **with no new attractor taking its place**.
- **Standing:** targets identified pooled L16–L21: `Happy 125th Penn State` 1980 (×6), `Penn State and Proud of it` 1992 (×6), `Never Badger A Lion` 2001 (×5), `Lions Clean House` 2005, `Lions Won't Need A Recount` 2001.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §C H11

### C2 — Reference quality metric

- **Track:** Reference and data
- **Status:** BLOCKED
- **Question:** `_ref_quality_score` is sharpness-first (Laplacian variance), which is not comparable across resolutions — a tiny thumbnail dodges the "weakest ref" flag. Should resolution become the dominant signal?
- **Instrument:** — (not a data experiment)
- **Gate:** operator decision on direction. The operator said "nevermind" mid-discussion, so confirm direction before building anything.
- **Standing:** open as a question, not as an experiment.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §C H12; `HANDOFF.md` known issues

### C3 — Reference save policy

- **Track:** Reference and data
- **Status:** SETTLED-CONFIRMED
- **Question:** should a score gate or an agreement gate decide what gets staged into the reference DB?
- **Instrument:** staging decisions joined to confirmed outcomes
- **Gate:** the gate that keeps fewer wrong crops and more good ones wins.
- **Standing:** AGREEMENT gates staging, not score. The old 0.90 score gate kept a 0.968 WRONG crop and blocked 3.5× more good ones. Typed and human confirms save at any score and carry the true sport type.
- **Source:** `AUTOMATION_VISION.md` §5; `log_analysis.md` "Second change: reference-DB save policy"

### C4 — The winter-sports shelf

- **Track:** Reference and data
- **Status:** SHIPPED-WATCH
- **Question:** can the winter-sports reference shelf be built by exactly the lots that expose its absence, until the football filter dissolves into an ordinary prior?
- **Instrument:** `chosen_type` on typed confirms; `shadow_top_json` twins with identical normalized slogans — that set is the cross-sport risk surface
- **Gate:** references accumulate → `ref_sim` starts arbitrating same-slogan twins ("Plaster Pitt") → the filter becomes a prior rather than a rule.
- **Standing:** accruing. The unfiltered-suggestions 4th option saved 30 of 35 typed slogans on its first lot.
- **Source:** `AUTOMATION_VISION.md` §4 parallel track, §5

### C5 — The label harvester

- **Track:** Reference and data
- **Status:** SHIPPED-WATCH
- **Question:** is every pipeline lot leaving behind a labeled training example for the learned-detection track?
- **Instrument:** `pipeline/labels/<job_id>.json` + `.jpg` sidecars — detection-space image, circles with provenance, Gemini reading verbatim; confirms join via `confirm_log.job_id`. Kill switch `BUTTONMATCHER_LABEL_HARVEST=0`.
- **Gate:** accrual, not a verdict — the learned segmenter has to be earned with data first.
- **Standing:** shipped in both repos.
- **Source:** `AUTOMATION_ROADMAP.md` status update 2026-07-09; `AUTOMATION_VISION.md` §2

### C6 — The gemini_auto visual audit record

- **Track:** Reference and data
- **Status:** BLOCKED
- **Question:** the operator visually audited 759/759 `gemini_auto` decisions — but the result is attested only in chat, so it cannot be cited as evidence for any gate.
- **Instrument:** — (a durable note in `HANDOFF.md` or the Sheet)
- **Gate:** a one-line durable record makes it citable.
- **Standing:** outstanding operator-side ask, unchanged across roadmap refreshes.
- **Source:** `AUTOMATION_ROADMAP.md` status update "Still waiting on operator-side data"

---

## D. Built but never graded

### D1 — TTA weak-crop second pass

- **Track:** Built but never graded
- **Status:** BUILT-UNGRADED
- **Question:** does re-matching crops whose #1 lands below `RED_THRESHOLD` with test-time augmentation improve them?
- **Instrument:** `TTA` flag, default off. No shadow column.
- **Gate:** give it a shadow following the §A pattern, or remove it. Distinct from A15, which points the same machinery at low-GAP rather than low-score crops.
- **Standing:** never A/B'd.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §D

### D2 — Two-level reference rerank

- **Track:** Built but never graded
- **Status:** BUILT-UNGRADED
- **Question:** does the two-level reference rerank (`year_score` / `sid_score` / `rerank_delta` per candidate) improve ranks?
- **Instrument:** `rerank_json` in `match_log`, `rank_rerank` in `confirm_log`; `RERANK` flag, default off "until calibrated"
- **Gate:** calibrate against confirmed truth, or remove it.
- **Standing:** never calibrated. The columns log; nothing reads them.
- **Source:** `HYPOTHESES_IN_PROGRESS.md` §D

---

## E. Rollout stages

### E1 — Stage A: everything logged

- **Track:** Rollout stages
- **Status:** SETTLED-CONFIRMED
- **Question:** is the system fully instrumented, with a self-certifying stratum identified?
- **Instrument:** the whole `match_log` / `confirm_log` schema
- **Gate:** `ni_gate=auto` + `scale_path=scale_first` identifies self-certified unguided lots.
- **Standing:** where we are. **96% exact / 100% ±1 at n=329** on that stratum (the early "100% exact" was a 20-image sampling artifact), and the detector-bailout loophole is patched.
- **Source:** `AUTOMATION_VISION.md` §4 Stage A

### E2 — Stage B: detection stands alone on gated lots

- **Track:** Rollout stages
- **Status:** OPEN
- **Question:** can the unguided count become primary on `auto`+`scale_first` lots, with Gemini demoted to a cross-check?
- **Instrument:** gated unguided count vs `gemini_button_count`, passive accrual on the daily feed
- **Gate:** **≥98% count agreement with Gemini on gated lots at real volume.** Rollback if gated disagreement exceeds 2% over any 50 lots (`auto_overridden` has no UI affordance, so it cannot be the tripwire). Instrument the placement blind spot (B22) before flipping.
- **Standing:** 0% gated disagreement on the post-patch organic feed; 8/9 vs human truth (n=9); `scale_first` is ~33% of volume. Volume is the constraint, not accuracy. Collection is passive — no crawls required.
- **Source:** `AUTOMATION_VISION.md` §4 Stage B; `AUTOMATION_ROADMAP.md` Phase 5

### E3 — Stage C: Gemini becomes an auditor

- **Track:** Rollout stages
- **Status:** BLOCKED
- **Question:** can Gemini be called only when the gate is below `auto`, the match lacks reference agreement, or the lot is flagged needed/valuable?
- **Instrument:** `ni_gate`, `ref_sim`, needed-button flags
- **Gate:** Stage B stable AND the `ref_sim` calibration (A19) shows entry-level visual similarity separates right from wrong at some threshold with ≤2% miss.
- **Standing:** blocked on E2 and A19. Prize: Gemini calls drop to a fraction, the watcher fleet shrinks, and the pipeline's rate limit stops being the bottleneck.
- **Source:** `AUTOMATION_VISION.md` §4 Stage C

### E4 — Stage D: the human becomes an auditor

- **Track:** Rollout stages
- **Status:** BLOCKED
- **Question:** can auto-confirmed lots commit directly, with the human seeing only new slogans, purchase decisions, a random audit sample, and two-signal disagreements?
- **Instrument:** measured auto precision via the correction flow (A10)
- **Gate:** **≥98% measured precision over ≥300 confirmations.** Never remove the 1-in-N audit sample — it is the drift detector, sized to catch a 2-point precision drop within a week.
- **Standing:** blocked on A10, which is still at zero rows.
- **Source:** `AUTOMATION_VISION.md` §4 Stage D
