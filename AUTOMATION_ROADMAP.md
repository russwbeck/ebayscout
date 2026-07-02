# Automation Roadmap — Hough Detection & Slogan Matching

**Goal:** fully automated button identification — no human count input, no human
review on the happy path.
**Grounding data:** `buttonmatcher/log_analysis.md` (Logger_5: 2,891 match rows /
615 images; +788-lot instrumented follow-up) and the Logger_3 validation batch
(2026-07-01: five `/sort` lots with user counts + 13 daily-pipeline lots), and
the **Logger_4 instrumented 300-lot run** (2026-07-02 — the measured baseline
below).
**Convention:** detection (`detect.py` ↔ `ebayscout/detect_pipeline.py`) and
`match_logging.py` are kept in lockstep across **buttonmatcher, ebayscout, and
buybot** — every change below lands in all three.

## Phase status at a glance

| Phase | What | Status | Blocked on |
|---|---|---|---|
| 1 | Instrumentation completion (gaps 1/2/5 + saturation signal) | ✅ **merged (PRs #43/#104), deployed, validated on the 300-lot run** | — |
| 2a | Defect C: mask-saturation fallback (blue-only + hole-fill retry) | ✅ **implemented on this branch** — real 35-lot: guided 35/35 (was grid-fallback); 26-lot: 23/23 blue | — |
| 2b | Defect A: DT-radius-led re-Hough on fused lots (revised — DT peaks are the radius source, not the counter) | ◐ largely covered by 2a (saturation was the fusion driver in both real lots); re-measure residual non-saturated fusion on the next batch | next batch |
| 3 | Defect B: concentric/radius dedup (small-lot overcount) | ✅ **implemented on this branch** — 0.7–1.3×median band + concentric collapse (keep better fill) on the unguided selection | — |
| 3.5 | Tighten `ni_gate=auto` | ✅ **implemented on this branch** — AUTO now requires `scale_path=scale_first` (Logger_4: 40/40 exact vs 6/6 wrong autos on fallback paths) | — |
| 4a | Low-res guard (thumbnail auto-confirm) | ✅ implementable immediately | nothing |
| 4b | Reference coverage for 0.00-scoring slogans | ⏸ data (exists, needs a run) | one `audit_reference_coverage` run vs GCS |
| 4c | Measured auto-confirm error rate | ⏸ human data | `correction` rows in confirm_log (~100 auto-confirms reviewed) |
| 5 | Rollout: drop the guided count (revised: gate-scoped, not bucket-scoped) | ⏸ re-measure after 2a/2b/3 land | next instrumented batch |

## Measured baseline — Logger_4 (2026-07-02, the 300-lot `ignore_seen` run)

First batch with the Phase-1 instrumentation live: 298/300 lots processed,
every new column populated on every row; 265 lots had a usable Gemini count.
What it changed:

- **True unguided is much weaker than the old numbers implied.** `ni_selected`
  exact = 34% overall (singles 31.5%, with 36% overcounting by 2+). The earlier
  "~80% on singles" figure was `det_hough_pass1` — Gemini-radius-seeded, i.e.
  guided. The radius seed was doing far more work than believed; dropping
  guidance rests on fixing the radius estimate, not on Hough itself.
- **The confidence gate works.** `ni_gate=auto` selected 23.4% of lots and was
  **90.3% exact** (93.5% ±1) on them; `suggest`/`manual` correctly quarantined
  the rest (16–21% exact). Zero saturated lots reached `auto`. Five of the six
  wrong `auto` lots carry telltale signals (`ni_scale_conf` = 0 or an
  implausible `ni_r_est`) — Phase 3.5 tightens on exactly those.
- **Saturation (defect C) is 19.2% of the pool** and devastating inside it:
  guided exact 19.6% vs 64.5% on normal masks; 62.7% grid-fallback. Phase 2a is
  the biggest single detection lever.
- **DT peaks are NOT a counter in the wild** — 12% exact on the 25 fused lots,
  over-splits everywhere. They ARE the radius/fusion signal; the working design
  (verified on both real failed lots) is Hough re-run at the DT-corrected
  radius. Phase 2b is revised accordingly.
- **Defect B has a dominant simple mode**: 68% of singles overcount unguided,
  and the largest cluster is exactly +1 (69 of 216) — the concentric/glare rim.
  Uncorrelated with saturation (mean coverage ≈0.50 in both groups) — an
  independent defect with its own fix.
- **Rollout shape revised (Phase 5): gate-scoped, not bucket-scoped.** Trust
  unguided only where `ni_gate=auto` — 23% of lots today at 90%+, a share that
  grows as 2a/2b/3 land and more lots earn `auto` — and keep Gemini on the
  rest. Safer than per-bucket flips because the gate already refuses the
  saturated/fused failure modes.

---

## Phase 1 — Instrumentation completion ✅ (this branch)

**No data needed — this closes the holes that block everything else.** Three
changes, mirrored in both repos:

1. **Gap 1 — true unguided shadow count on the pipeline** (`main.py`, both
   pipeline handlers): `count_circles_unguided` now runs on every pipeline lot
   (when `BUTTONMATCHER_SHADOW_PASS` ≠ 0 — on by default) and its result lands in
   `det_count_noinput` + the `ni_*` columns (`ni_selected`, `ni_confidence`,
   `ni_pass_winner`, `ni_r_est`, `ni_scale_conf`, `ni_gate`, …). Before this,
   pipeline rows had all of these blank (confirmed in Logger_3), so unguided
   accuracy was unmeasurable exactly where automation matters. Cost: ~15 extra
   Hough calls inside the already-CPU-hot in-flight request.
2. **Gap 2 — Hough-param + rejected-radius telemetry on ebayscout's pipeline
   detector** (`detect_pipeline.py`): ported the PR #42/#103 hunks
   (`det_hough_dp/mindist/param1/param2/min/maxradius`,
   `det_rej_radius_min/median/max`) that existed in buttonmatcher's `detect.py`
   but not in `detect_pipeline.py` — ebayscout pipeline rows logged them as
   blank. buttonmatcher's *pipeline* `build_detection_diag` call also now plumbs
   the full diagnostic set (it previously passed almost none of it).
3. **Gap 5 — count-free over-merge signal** (`_prepare_detection_image`, both
   detectors): three new appended `match_log` columns —
   `det_mask_blobs_raw` (raw mask blob count), `det_dt_peaks_total`
   (summed per-blob distance-transform peak count, threshold 0.55× each blob's
   own DT max — the blob-buster's core convention, but with **no expected
   count**), and `det_mask_coverage` (foreground fraction — the defect-C
   saturation trigger). These are the Phase-2 decision metrics.

**Verified:** 37 match_logging tests pass per repo (incl. 3 new); synthetic
end-to-end runs of both detectors populate every new key; on a synthetic 5×5
touching grid fused into ONE mask blob, `dt_peaks_total=25` (exact) — and the
same held for 36-button and heavy-overlap cases. Real-photo behavior still needs
the live data below.

**Deploy steps:**
- Extend the `match_log` header row by hand with the three trailing columns
  (`det_mask_blobs_raw`, `det_dt_peaks_total`, `det_mask_coverage`), or delete
  the tab and let `_ensure_tab` recreate it (old rows then read as blanks — fine).
- Copy the `detect.py` / `match_logging.py` hunks to **buybot** (not reachable
  from this session).
- Kill switch if pipeline CPU ever bites: `BUTTONMATCHER_SHADOW_PASS=0`.

---

## Phase 2a — Defect C: mask saturation (✅ verified fix, implementable immediately)

**Pre-tested on the actual failed 35-button lot (2026-07-02).** Root cause found
and a fix verified end-to-end on that image:

- The cream quilted blanket isn't classified as "white" by the border sampler,
  so the mask path goes `blue_or_white` — and the whole blanket passes the white
  range. **Mask coverage = 89%**: buttons and background fuse into one sheet.
  Hough on that mask finds **0** circles (nothing to find — no circular edges),
  DT peaks read 5, and scale-first reads `r_est=19.6` (the white slogan text
  punches holes in what little structure remains). The code already detects
  saturation (`coverage > 0.75` caps confidence at 0.4) — it just doesn't recover.
- **Verified fix:** when saturated, rebuild with the **blue-only** mask +
  hole-fill (text holes must be filled or DT reads the text gaps, ~19.6px, not
  the button, ~38px). On the real failed image this yields **35/35 blobs,
  `dt_peaks_total=35`, radius 38.2, and Hough at that radius finds exactly 35
  circles.** Total recovery of a lot that collapsed to 1.

**Second confirmation — the failed 26-button lot (white batting in a display
box).** Same signature: `det_mask_coverage=0.81`, mask fused to 1 component,
`r_est=269` (the whole batting sheet read as one giant button), unguided passes
all 0 → collapsed to 5. The same blue-only + hole-fill fallback: coverage drops
to 40%, blob median radius **45.6 (correct)**, and Hough at that radius finds
**24 of the 23 blue buttons (+1 extra)**. The 3 *white* buttons are invisible to
a blue-only mask by construction — that's precisely the existing white-rescue
pass's job (it fires on the remaining deficit using image gradients), plus
Gemini reconcile as the pipeline safety net. Both Logger_3 dense-lot collapses
are therefore the SAME defect, and both recover under this one fallback.

Caveat learned from the 26-lot: `det_dt_peaks_total` over-split its irregular
fused blobs (33 vs 23) — so treat the DT signal as the *radius* source and
fusion flag, and let Hough-at-corrected-radius do the counting; the Phase-2b
data will quantify this distinction.

**No data hole.** The trigger (`det_mask_coverage > ~0.75`) only fires where the
current path is already blind, so the fallback can't regress a working lot.
Optional belt-and-braces: ship it shadow-first for one batch (log what the
fallback would return) — but straight adoption is defensible. Implementation
lives next to `_build_clahe_mask` as another mask variant; reuse the existing
hole-fill and re-derive `expected_r` from the fallback mask's blob DT maxima.

---

## Phase 2b — Defect A: fused-lot collapse (DT-peak blob split + DT-informed radius)

**What Logger_3 showed:** the 26-button lot is the true fusion case — buttons
touching, one mask component (`mask_components=1`), the scale estimate votes one
giant button (`ni_r_est=268px` vs ~47px true) → both unguided and guided Hough
search the wrong radius window and find ~nothing → grid fallback. (The 35-lot
turned out to be defect C above, not fusion — the pre-test separated them.)

**The fix (two parts):** (a) split any mask blob whose DT yields multiple peaks —
no `expected` needed; (b) feed per-blob DT peak radii into the scale consensus so
fused blobs stop poisoning `ni_r_est` / the guided radius window.

**Data needed to cross the hole** (the synthetic result is perfect, but real
masks have glare holes, background bleed, and partial fills):

| Question | Columns | Volume |
|---|---|---|
| Does `det_dt_peaks_total` track the true count on real fused lots? | `det_dt_peaks_total` vs `gemini_button_count`, filtered to `det_mask_components < gemini_button_count` (the fusion signature) | ~100 pipeline lots, incl. **~20 with 7+ buttons** |
| When should DT override the scale consensus? | `ni_r_est`, `ni_scale_conf`, `ni_scale_path` vs `det_radius_mean` on lots where Hough engaged | same batch |

**Adoption gate (2b):** implement when `det_dt_peaks_total` is within ±1 (or ±10% on
13+) of Gemini's count on ≥80% of fused lots. If it passes, part (a) can even
ship *shadow-first* (log what the split would return next to what detection
returned) for one more batch before switching it live.

**How to collect (zero cost):** the daily feed logs everything automatically once
Phase 1 deploys. Caveat: daily-feed lots skew single-button (Logger_3: 12/13 were
`gem=1`), so dense lots trickle in slowly. The fastest gold-standard source is
what you did on 2026-07-01: **run your own dense lots through `/sort`** — the
typed count is ground truth and every row carries the new columns. ~15–20 dense
`/sort` photos ≈ enough. A `/crawl 200` would also work but costs real eBay-API +
CPU money — not needed.

---

## Phase 3 — Defect B: small-lot overcount (radius-consistency / concentric dedup)

**The defect (Logger_5):** on 1–3-button pipeline lots, ~15% overcount; the extra
circles (glare rings, concentric rims, printed circles) **pass every current
filter**, and their radii differ from the real button (`det_radius_std ≈ 1.9` on
singles). Logger_3's `/sort` singles were clean — this shows on the
**crawl/daily-pipeline** photo population, not on curated collection photos.

**The fix:** reject circles whose radius is an outlier vs the dominant radius
cluster; collapse near-concentric circles (center distance < ~0.3·r, different
radii → keep the better-filled one).

**Data needed to cross the hole:**

| Question | Columns | Volume |
|---|---|---|
| What radius spread separates real vs spurious on *overcounted* lots? | `det_radius_min/max/mean/std`, `det_hough_pass1`, `det_count_noinput` vs `gemini_button_count=1..3` | ~50 overcounted small lots ≈ ~330 single-button pipeline lots at the ~15% rate |
| Do the filters already *see* the imposters? | `det_rej_radius_min/median/max` (only meaningful now that ebayscout's pipeline logs them — Phase 1 gap 2) | same batch |

Note: the daily feed produced ~11 singles/day in the last batch, so ~330 singles
≈ 2–4 weeks of normal feeds, **or** they accumulate automatically while Phase 2
is being validated — no action needed, no extra cost. Overcount cases can also be
spotted early: any pipeline row with `det_hough_pass1 > gemini_button_count`.

**Adoption gate:** a dedup rule that removes ≥80% of the spurious extras on the
collected overcount set while removing zero circles on exact-match lots.

---

## Phase 4 — Slogan-side hardening (independent of detection)

Slogan matching is already strong — Logger_3: CLIP's #1 was right on 39/41 crops
(95%), and 12 of 14 human picks merely ratified the #1 below the 0.85 auto
threshold. These items close the remaining risk:

- **4a — low-res guard: implementable immediately, no data hole.** The defect is
  fully characterized (a 104×104 thumbnail Gemini counted as 11 buttons was
  auto-confirmed into inventory + the reference flywheel; same 9 images recur
  across Logger_3/4/5). Rule: when the rendered button diameter
  (`2·det_radius_mean`, or image dimension / grid) is below ~64px, downgrade
  `gemini_auto` → manual review and block reference staging. Threshold can be
  refined later from `det_h/w` + radius columns, but any sane floor beats none.
- **4b — reference coverage for rare slogans** (CCB/CCNB slogans scoring 0.00 in
  HANDOFF). Data exists; it needs one run of
  `tools/audit_reference_coverage.py` against GCS (requires GCP access — run it
  locally, not from a web session) to produce the gap list, then fill from
  `reference/_staging/` or manual uploads.
- **4d — football pre-filter (`/scout`/`/inventory`): DECIDED 2026-07-02 — keep
  as-is for now; revisit after the next crawl batch.** Measured on Logger_2's
  2,590 match rows (both leaderboards + sport types): unfiltered shadow #1
  agrees with the restricted #1 91%; a non-football candidate would take #1 on
  ~15% of crawl crops but almost always below the auto threshold — only 0.6%
  at ≥0.85 (the silent-auto zone), 0 on the daily feed, 0 in Logger_3's 41
  confirmed rows. Cost of the filter: it forces typed entry for non-football
  buttons (all 3 typed slogans in Logger_3 were already #1 unfiltered). If
  revisited, the recommended shape is a SPLIT, not removal: unfiltered
  *suggestions* for humans, football-gated (or Gemini-agreement-gated)
  *auto-confirm*. Verify first on a confirmed-truth export at scale (Logger_5's
  ~329 leaderboard confirmations, or the next crawl's confirm rows).
- **4c — measured auto-confirm error rate: blocked on human data that doesn't
  exist yet.** No `correction`/`skip_correction` rows have ever been logged, so
  auto-path precision is inferred, not measured. To cross: when an auto-confirm
  is wrong during normal use, use the correction flow (don't silently fix the
  sheet). Target: ~100 auto-confirms with corrections logged → measure precision
  directly; ≥95% supports widening auto-confirm, e.g. lowering the 0.85
  `auto_sort` threshold toward 0.82 (Logger_5 says 0.82 keeps precision at
  ~0.979 with ~3.5× volume — but verify on measured corrections first).

---

## Phase 5 — Rollout: drop the guided count

**Blocked on Phase-1 data by design.** The gate table, computed per lot-size
bucket over a few hundred post-Phase-1 pipeline lots (`ni_selected` — and after
Phase 2, `det_dt_peaks_total` — vs `gemini_button_count`):

| Bucket | Gate to flip |
|---|---|
| 1 button | ≥90% exact |
| 2–6 | ≥85% exact |
| 7+ | ≥80% within ±1 |

When a bucket sustains its gate, flip that bucket to Hough-primary with Gemini
demoted to auditor (the `BUTTONMATCHER_AUTO_DETECT` / `gate_decision`
scaffolding already exists; `ni_gate=auto` was correct on all five Logger_3
`/sort` lots, including refusing the two collapsed dense lots). End state:
Gemini consulted only on disagreement — which also cuts Gem-worker load. Keep
`reconcile_with_gemini` as the safety net throughout.

**Collection is passive:** normal daily feeds. At Logger_3's rate (~13
images/day), ~2–4 weeks per few hundred lots; any `/sort` and `/scout` use adds
gold-standard rows on top. No crawls required.

---

## Data-collection cheat sheet

- **Zero-cost default:** deploy Phase 1 and let the daily feed run. Every lot
  then logs guided count, TRUE unguided count + diagnostics, Hough params,
  rejected radii, and the DT-peak signal — joinable to `gemini_button_count`
  per `job_id`.
- **Highest-value manual data:** dense (7+) lots through `/sort` with the typed
  count (Phase 2's bottleneck — daily-feed lots are mostly singles), and using
  the correction flow on any wrong auto-confirm (Phase 4c's only source).
- **Do NOT** launch `/crawl`, `?year_crawl=1`, or `?ignore_seen=1` for data
  collection — real eBay-API + CPU cost, and the passive feed suffices.
- **Grading query (any phase):** one row per `job_id` from `match_log`, bucket
  by `gemini_button_count`, compare `ni_selected` / `det_dt_peaks_total` /
  `det_hough_pass1`, and split fused lots out via
  `det_mask_components < gemini_button_count`.
