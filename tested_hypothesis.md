# Tested Hypotheses — the consolidated record

*This file is the single place where strategic hypotheses go once they have
been tested — confirmed, refuted, or bounded — so the strategy docs
(`AUTOMATION_VISION.md`, `AUTOMATION_ROADMAP.md`) stay about the future and
this file holds the verdicts. Newest sections last. Byte-identical across
buttonmatcher and ebayscout.*

---

# Part I — unguided detection (white/blue on matching backgrounds + Layer-1 radius trust)

*Session 2026-07-04. Method throughout: run the REAL detector functions
(`count_circles_unguided` / `detect_buttons` in `detect.py`) on real lot photos
and grade against human/Gemini truth — never a mental model (AUTOMATION_VISION
§7). Nine lot photos were tested live and committed as fixtures
(`tests/fixtures/lots/`); Layer-1 claims are graded on 329 pooled images from
Logger_3–7. Companion docs: `AUTOMATION_VISION.md` (strategy), `log_analysis.md`
(the running data-analysis log this extends).*

---

## 0. TL;DR

The user's ask — "detect buttons that match their background (white-on-white
**and** blue-on-blue)" — resolves into **two layers**, and the data says which
is the real bottleneck:

- **Layer 2 (color-blind recovery):** a **rim-support union pass** cleanly
  recovers white-on-white buttons the color mask can't see, with zero false
  positives on 5 varied backgrounds — **but only when the radius estimate is
  good.** Ready in scope, not yet shipped (depends on Layer 1).
- **Layer 1 (radius/scale, foundational):** every failure — white-on-white
  misses, blue-on-blue, banded buttons, coins — traces to a **collapsed radius
  estimate** (`ni_scale_path == sweep_fallback`). This is the real bottleneck to
  unguided automation. The **trust gate for it already exists and is correct**
  (`scale_first`); there is **no cheap telemetry rescue** for the broken 65%.

**Shippable now:** the founding fixture battery (done, this session) + the
already-committed gate fix, validated fresh by `/crawl200`. **Not yet:** the
rim-union pass (needs Layer-1 radius first) and any `sweep_fallback` radius fix
(genuinely hard; needs the CV work, not a patch).

---

## 1. The 9 test lots and what each proved

| fixture | truth | unguided | gate | scale_path | verdict |
|---|---|---|---|---|---|
| `pure_blue_23` | 23 | **23** | auto | scale_first | ✓ clean control |
| `wood_pale_on_dark_14` | 14 | **14** | auto | scale_first | ✓ pale-on-**dark** is NOT the disease (mask handles it) |
| `granite_glare_13` | 13 | 11 | suggest | sweep_fallback | white-on-white (+glare) |
| `mixed_bluewhite_15` | 15 | 12 | suggest | scale_second_chance | white-on-white ×3 |
| `mixed_colors_shapes_22` | 22 | 19 | **auto** | scale_first | scale_first ceiling: 2 squares + faint white undetectable |
| `banded_quad_4` | 4 | 8 | suggest | sweep_fallback | banded fragmentation (over-count) |
| `banded_single_coin_1` | 1 | 2 | suggest | sweep_fallback | banded single + coin distractor |
| `navy_blueonblue_35` | 35 | 5 | suggest | sweep_fallback | blue-on-blue (mask saturation) |

(Counts are the deterministic snapshot on the committed 800px PNGs; the
regression test locks them.)

---

## 2. Layer 2 — the rim-support union pass (color-blind recovery)

**Disease (confirmed by rendering the masks):** on a white/pale background the
HSV mask goes `blue_only` and is *structurally blind* to white buttons — they
appear only as their text specks. The existing "bright variant" satfallback
(`V>bg+60 & S<80`) is **useless for white-on-white** (measured 0.000 coverage):
a white button is the *same brightness* as white paper. The only signal a
white-on-white button has is its **circular rim + edge-dense printed interior**.

**Discriminator evolution (each step forced by a lot that broke the previous):**
1. *Interior edge-density vs a border reference* (today's `_white_rescue_pass`):
   fails because the border reference is inflated by any textured edge
   (granite/placemat), and because sparse-print pale buttons (the "Sit!" button)
   have interior edge density (0.05) indistinguishable from background.
2. *`min` across the 4 border strips* — more robust than "sides only", but still
   an edge-density test, so still fails sparse-print buttons.
3. **Rim support** (fraction of the circle's circumference backed by a real
   gradient) — **works for blue AND pale/sparse buttons alike**, because every
   genuine button has a complete rim while glare/texture does not.

**Result (union of {color-mask circles} ∪ {rim circles with rim_support ≥ 0.40}):**

| lot | base (unguided) | union | truth | false positives |
|---|---|---|---|---|
| mixed_bluewhite_15 | 12 | **15** | 15 | 0 |
| granite_glare_13 | 11 | **13** | 13 | 0 |
| pure_blue_23 | 23 | 23 | 23 | 0 (adds nothing when already right) |
| wood_pale_on_dark_14 | 14 | 14 | 14 | 0 |
| mixed_colors_shapes_22 | 19 | 20 | 22 | 0 (misses 2 squares + 1 faint) |

**Confirmed:** the union pass fixes white-on-white and is *safe* — 0 false
positives on granite, wood, and a clean blue grid. Strict Hough (param2=30) at
the button radius never nucleated a junk circle on any textured background
tested, so the rim-support gate hasn't even had to reject junk yet.

**Refuted / boundaries found:**
- It **depends on a good radius** (see Layer 1). On `navy_35`, `coin_1`,
  `banded_4` the base `r_est` is garbage, and the rim pass — seeded from that
  same radius — inherits the failure (navy 5→13, still far from 35).
- **Dedup bug:** on banded buttons the rim pass finds the true outer circle but
  *stacks* it on the color-mask fragments instead of absorbing them (center-
  distance dedup misses contained fragments). Fix rule: *a base circle fully
  contained in a larger rim circle is an interior feature — absorb it.*
- **Not a fix for:** squares (Hough finds only circles), size outliers (a button
  half the neighbors' size falls outside the radius band), and **coins/round
  clutter** (a coin has a strong rim; the rim gate would accept it — untested at
  a correct radius, but a known latent false-positive).

**Status:** correct in scope, **not shipped** — blocked on Layer 1 (radius) and
owing the contained-fragment dedup fix. Prototype: `scratchpad/union_eval.py`.

---

## 3. Layer 1 — radius/scale is the real bottleneck (the data)

Graded on **329 pooled images** (Logger_3–7, truth = typed or Gemini count).

### 3.1 The trust signal is `scale_path`, not `scale_conf`

| set | n | exact | ±1 |
|---|---|---|---|
| `gate=auto` AND `scale_path=scale_first` | 46 | **96%** | 100% |
| `gate=auto` but not scale_first | 24 | 67% | — |
| `gate=suggest` + scale_first | 56 | 45% | — |
| `sweep_fallback` (any gate) | 215 | 32% | 59% |

- `scale_first` **alone** is only 68% exact — a 20-image sample earlier showed
  100% and **lied** (exactly the "confidence is not truth" warning,
  AUTOMATION_VISION §7). The trustworthy set is `auto AND scale_first`.
- `scale_conf` buckets do **not** separate cleanly (0.85–1.0 bucket is 44%
  exact) because `sweep_fallback` computes its own inflated confidence. Use the
  **path**, not the score.

### 3.2 No cheap rescue for broken lots

When `scale_first` fails, can a count-free signal substitute? **No.** Median
|error| vs truth: `ni_selected`=0, `det_dt_peaks_total`=**7**,
`det_mask_blobs_raw`=**21** (a single button reads as 36 dt-peaks from its text
holes). The `dt_peaks` "count-free estimate" the vision doc hoped for does not
work as a count. `sweep_fallback` lots need real radius CV or the Gemini/human
guide — do not chase a telemetry shortcut.

### 3.3 The gate is already correct (and this data validates it)

`gate_decision` already requires `scale_first` for `auto` (committed `3777a10`,
2026-07-02 19:45). All 24 `auto`+non-`scale_first` rows — including a **`271→1`**
auto-confirm (`scale_conf=0.00`, but `layout_conf=1.0` because one circle is
trivially "aligned") — are timestamped **before** that commit. So the 329-image
analysis **independently re-derived a fix that is already live**; the wrong-auto
class is entirely pre-fix. The two remaining `auto+scale_first` misses are both
**±1** and feature-indistinguishable from correct autos, so **no further gate
tightening is safe** — 96%/100%±1 is the ceiling.

### 3.4 Structure inside `scale_first` (where the residual error lives)

- **Singles over-count** (+2.4 mean gap, 66% exact) — glare/concentric rings
  survive as different-radius circles (`log_analysis.md` defect B).
- **Dense lots under-count** — touching buttons fuse in the mask (defect A).
- Sweet spot is **2–6 buttons** (77% exact). `layout_conf ≥ 0.9` → 76%/96%±1.

---

## 4. Confirmed / Refuted / Pending

**Confirmed**
- White/blue-on-matching-background is one disease: *color-blind to a button
  whose color matches its background; only the rim survives.*
- Rim-support gate recovers white-on-white with 0 FP on 5 backgrounds.
- `scale_path=scale_first` (with `gate=auto`) is the count-trust signal (96%).
- Radius collapse (`sweep_fallback`) is the shared root cause of every hard lot.
- The gate fix is already deployed and correct (validated on 329 images).

**Refuted**
- "`scale_first` ⇒ ~100%." (Small-sample artifact; 68% at scale.)
- "`dt_peaks`/`mask_blobs` give a usable count-free estimate." (Off by 7–21.)
- "The gate under-promotes trustworthy lots." (Withheld lots are 45% exact —
  the gate is right to hold them.)
- "The bright/`V>bg+60` variant catches white-on-white." (0.000 coverage.)

**Pending (needs more data / images)**
- Coin / round-clutter false positive at a *correct* radius (latent, untested).
- The exact button-count density boundary where `scale_first` starts fusing.
- ~~The automatable volume % (what fraction of pipeline lots are `scale_first`)~~
  — **answered 2026-07-08: ~33% of the organic feed** (Part II §3).

---

## 5. What was implemented this session

1. **Fixture regression battery** (AUTOMATION_VISION §3, roadmap item #1):
   `tests/fixtures/lots/` — 8 real lots at 800px PNG + `manifest.json` +
   `tests/test_detect_fixtures.py`. Locks the unguided snapshot, guards the two
   clean lots at `auto`+exact, and asserts the Layer-1 safety property
   (*a non-`scale_first` path never reaches `gate=auto`* — the scale_path
   plumbing between `detect_unguided` and `gate_decision`). Lives in
   buttonmatcher; `ebayscout/detect_pipeline.py` produces **identical** counts on
   all 8 (parity verified this session), so the battery covers both detectors.
2. **This document** (byte-duplicated to ebayscout, per the AUTOMATION_VISION
   convention) **+ `log_analysis.md`** Layer-1 section (buttonmatcher only).

**Deliberately NOT implemented** (would be premature/unsafe):
- The rim-union pass (blocked on Layer-1 radius; owes the dedup fix).
- Any `sweep_fallback` radius fix (needs the hard CV work + the failing images).
- Further gate tightening (no safe lever left; the 2 misses are ±1).

---

## 6. Next, in priority order

1. **`/crawl200` (ignore-seen)** — first fresh-data validation of the live gate:
   confirm `gate=auto` fires only on `scale_first`, and measure `auto+scale_first`
   agreement vs Gemini (the Stage-B entry gate; needs ≥98%, currently 96%) and
   the true `scale_first` volume %. It also *harvests* the failing single/dense
   lots (with images) for the next fix.
2. **Singles over-count** (concentric/glare-ring dedup) — the biggest residual
   error in the trustworthy set; `/crawl200` measures it directly and supplies
   the failing images to test against.
3. **Layer-1 radius robustness** for `sweep_fallback` (banding/saturation/
   blue-on-blue) — the hard research track. Self-bootstrapping wide-radius rim
   Hough was tried and is *not* a clean win (multimodal radius: logos + arcs +
   rims). Earn a learned segmenter with data first (AUTOMATION_VISION §2).
4. **Then** the rim-union pass for white-on-white, once radius is trustworthy —
   with the contained-fragment dedup fix, gated behind a kill switch, re-run
   against all 9 fixtures.

---

# Part II — Logger_10/11 round (2026-07-08→09): the gate loophole, ref_sim, and human-truth grading

*Data: Logger_10 (organic daily-feed export; operator visually audited all 759
`gemini_auto` confirmations — 759/759 correct) and Logger_11 (8 Jul pipeline
batch, graded against the operator's confirmed truth). Full working analysis:
`buttonmatcher/log_analysis.md` Layer-1/Layer-2 sections (Layer-2 currently on
branch `claude/log-analysis-hough-gate`).*

**Confirmed**

- **The trust gate had a loophole, and closing it works.** Hypothesis:
  `ni_gate=auto` could survive when the *guided* detector bailed to
  grid/Gemini-led — the shadow numbers then describe a detector that wasn't
  used. Confirmed in Logger_10 (gated shadow-vs-truth disagreement 2.6%, and
  the one harmful case was exactly this shape — the 66-button lot). Patch —
  `demote_auto_on_detector_bailout`: AUTO demotes to SUGGEST unless
  `detector_used` starts with `hough` (merged buttonmatcher #116 /
  ebayscout #50). **Post-patch the gated organic disagreement is 0%.**
- **Gemini per-button reads: 96.5% correct raw** (the operator overwrote the
  rest — confirmed rows ARE truth). ~~Per-lot Gemini is weak (74%), so grade
  Stage B against human truth only~~ — **CORRECTED 2026-07-10 by the
  operator**: every Gemini decision is visually confirmed and overwritten
  when wrong, so **Gemini can be assumed correct in the reviewed flow**. The
  74% "per-lot" figure was mis-attributed — it measured *pipeline* per-lot
  cleanliness, and the typical intervention was a **Hough-misplaced circle
  or a non-button object in the photo**, with Gemini's count right. Revised
  doctrine: the Stage-B **count** gate may be certified against Gemini count
  agreement (passive, accrues free); **placement/identity** errors are what
  a count ruler cannot see — grade those per-button (review taps, and
  Gemini's per-button x/y as a passive placement ruler:
  `gemini_geometry.plan_reconciliation` already computes the Hough-only
  unmatched-circle list and currently discards it). Against human truth,
  auto+scale_first measured 8/9 (n=9). The raw-crawl guards stay for
  unreviewed inputs (the 346-button hallucination, thumbnail distrust,
  ≤60 clamp).
- **Count-exact ≠ button-exact** (lot `1855dcee`): detection found the right
  *number* of circles while one was a non-button and one real button was
  missed. Per-button review taps (`not_a_button` / `missed_button`) are the
  only signal that catches this — and each tap is a labeled training example
  for the learned-detection track.
- **The `scale_first` automatable share is ~33%** of the organic feed —
  answers Part I's open volume question and sizes the Stage-B prize.
- **The live-gate invariant held on fresh data:** 0 `gate=auto` rows with
  `ni_scale_path != scale_first` in Logger_11 (0/9).

**Refuted**

- **"`ref_sim` has been flowing since PRs #110/#46."** 0/300 leaderboard rows
  had it non-null — the serializer was wired but the rows never carried the
  value. Fixed at the source (stamp entry-level sims onto both trimmed
  snapshots in `match_crops_with_diagnostics`; merged #116/#50). The
  `ref_sim` calibration (VISION §5) only starts accruing data from that merge.

**Newly instrumented, verdict pending**

- **Whitepass telemetry** (merged buttonmatcher #118 / ebayscout #51): the
  guided white-rescue pass now tags `mask_path += "+whitepass"` and logs
  `det_white_recovered`. Hypothesis to test on the next export: the rim
  rescue is doing real, previously-invisible work on white-button lots.
- **Label harvester** (`label_harvest.py`, merged): every pipeline lot writes
  `pipeline/labels/<job_id>.json` + `.jpg` sidecars (detection-space image,
  circles with provenance, Gemini reading verbatim; confirms join via
  `confirm_log.job_id`). This is the data engine for the learned-segmentation
  track — no verdict until a training set accrues.

---

# Part III — older strategic decisions, since validated (moved here from the strategy docs)

These were once open recommendations in `log_analysis.md` /
`HOUGH_AND_LOGGING_UPDATES.md`; they shipped and their predictions held, so
they are now record, not strategy:

- **Relax the guided Hough acceptance floor of 6** (`log_analysis.md`,
  2026-06-19): predicted Hough would own few-button lots. **Validated live on
  685 images** (2026-06-20→22): engagement 0%→92% on singles, **95% exact /
  98% ±1 overall**, 99/100 singles returned exactly 1 crop.
- **Agreement gates reference staging, not a score threshold**: the 0.90 gate
  kept a 0.968 *wrong* crop (wrestling-pin visual twin) and blocked ~3.5×
  more good ones. Replaced by Gemini-agreement/human-confirm saves. The
  Logger_10 audit (759/759 `gemini_auto` correct) is the strongest evidence
  yet that agreement-gated auto-saves are precise.
- **Multi-pass unguided Hough (3×param2) + contour fallback + CLAHE variant**
  (`HOUGH_AND_LOGGING_UPDATES.md` phases 1–3): shipped and long since
  superseded as the frontier — the binding constraint moved to the mask and
  the radius estimate (Part I), not the Hough pass structure.
- **"DT peaks can be a count-free counter"** (hoped in the Logger_5
  instrumentation plan): **refuted twice** — Logger_4 (12% exact on fused
  lots) and Part I §3.2 (median |error| 7–21). DT peaks remain the
  radius/fusion *signal* only. Kept here so nobody re-proposes it.
- **Cross-sport auto-confirm gate + unfiltered 4th suggestion** (the 4d
  split): shipped after a live basketball lot auto-confirmed as football
  twins; the 4th option saved 30 of 35 typings on its first lot. The
  football pre-filter survives as a prior on auto-confirm only.

---

# Part IV — building on small-N failure sets: gate the new path, verify it's on-target, fall back

*Session 2026-07-12. Method: the REAL detector (`detect_buttons`) run in-session
on the actual failing lot (cv2 installed), bisected across commits, circle
placement graded numerically against the image — never a mental model.*

## 4.1 The turf-cross regression (deficit-fill over-trust) — CONFIRMED, fixed

**Symptom.** A 32-button blue-on-green-turf lot in a cross layout went from a
clean run to phantom circles on empty grass and inflated numbering after the
2026-07-11 detection changes. The first guess was low image quality; **refuted**
— same file, same pixels, only the code changed (both runs cap identically at
2200px).

**Root cause (bisected on the real image).** `bb12ebf`'s **deficit-fill** (Fix C).
When the guided Hough holds ≥60% of `expected` it keeps those circles and
*commits the lot to the hough path, permanently skipping the projection
fallback*. But blue-on-turf defeats the colour mask — it floods **~68% of the
frame** and Hough locks onto grass texture: measured, **18 of 21 "kept" circles
land on grass (blue-fill 0.00), only 3 on buttons.** deficit-fill saw "21 ≥ 60%
of 32" and trusted the grass. The *old* path discarded those circles for a blind
projection grid, which the pipeline's Gemini-reconcile step then snapped onto the
real buttons — so **detection was always failing here; projection + reconcile was
silently rescuing it**, and deficit-fill removed the rescue. (The mask-radius
prior in the same commit was ruled out empirically: conf 0.52 < 0.55 threshold,
byte-identical output prior-on vs prior-off.)

**Fix (shipped, both repos).** Gate deficit-fill on mask-foreground fraction
(`_deficit_fill_decision`, `DEFICIT_FILL_MAX_MASK_FRACTION = 0.50`): a mask
covering > 50% of the frame has not isolated the buttons, so **decline and fall
back to projection**. Calibrated on the deficit-fill fixtures — the one legit
case (`case1_wood_glare_37`) fills 30% of the frame, the turf failure 68%, floor
at 0.50 clears both. Verified: turf restored 21→32; all 15 fixtures unchanged;
`test_deficit_fill_gate` locks the predicate + the calibration margin.

## 4.2 The reusable lesson

**When you build a detection path from a small N of failure examples, it will
overfit — so gate it on an independent check that its own output is actually
on-target, and defer to the prior behaviour when the check fails. Then iterate.**

- **Six examples is not a distribution.** deficit-fill was tuned on `case1..6`,
  none a many-button lot on a same-saturation background. It generalised to
  *trust garbage* on the 7th real case. Every small-N feature should be assumed
  to overfit until real traffic says otherwise.
- **Gate on an independent, output-checking signal — not on the training
  metric.** The failure was invisible to the count metric it was built on (21 ≥
  60% looked fine). The cheap independent guard was "are the new circles on
  buttons?" (mask-foreground fraction / on-mask fill). A new path that can't pass
  its own sanity check must **defer, not overwrite**.
- **Keep the fallback reachable; make the new path reversible.** Prefer a gated
  rollout with a kill switch and a logged decline (`deficit_declined_mask_
  fraction`) over "once a lot earns this path it is committed." Widen the new
  path only as more *real* failures confirm it.
- **Grade on the real detector, on the real image, before and after.** Running
  `detect_buttons` in-session and measuring 18/21 circles on grass is what turned
  "maybe low quality" into a one-line root cause and a calibrated threshold.
  This is the same method as Parts I–II; the turf case is the reminder that it
  applies to *guardrails on new heuristics*, not just the heuristics themselves.

**Still open (not solved by the gate).** This restores the prior behaviour
(projection + Gemini reconcile); it does **not** teach Hough to find buttons on a
same-saturation background — the Layer-1/Layer-2 mask+radius problem (Part I)
remains the real bottleneck. The gate just stops a small-N heuristic from
overwriting the rescue that was covering for it.

## 4.3 Carpet round (2026-07-12) — the flood gate generalised, and where it stops

Textured carpets are the same disease as turf (a background the colour mask can't
separate from the buttons), reported as a fresh fail case. Two raw lots run
through the real detector in-session, then a ~10-image pipeline verification by
the operator ("many successes, few failures"). What the round established:

- **Navy-on-carpet reached the acceptance floor on the plain hough path**, so the
  deficit-fill gate (4.1) never fired — yet the mask flooded 66% and all the
  "detected" circles were on the rug. Fix: `_guided_mask_floods` generalises the
  flood gate to the WHOLE guided acceptance (refuse + route to projection for a
  non-small lot whose mask floods), not just the deficit-fill fork. Zero fixture
  impact (no fixture floods AND has expected > 5); shipped both repos.

- **The generalised gate is only as good as the rescue behind it.** Pipeline
  verification, image 3 (navy-8): the gate routed to projection correctly, but the
  result was an empty 8-cell grid sprawled on the carpet with **zero Gemini
  confirmations** — because on a busy carpet **Gemini also returned nothing
  usable**, so there were no coordinates to snap the projection onto (the
  detector-is-grid → gemini-led-crops path had no slogans). Lesson extending 4.2:
  a "route to the fallback" fix silently assumes the fallback works; when the
  background defeats *both* the mask and Gemini, projection-on-carpet is worse
  than useless. Candidate direction (unbuilt): detect "flooded mask AND no usable
  Gemini reading" and **flag the lot unreadable for manual handling** rather than
  emit a carpet grid.

**Open item #1 — minority-colour miss + count-driven carpet phantoms (diagnosed,
NOT yet implemented; operator holding until a non-carpet regression check).**
Distinct from the flood cases: the mask is *clean* (does not flood), so the flood
gate correctly does not engage, but two things still go wrong on carpet:

- *A minority-colour button is missed.* White-on-gray lot (5 buttons: 4 white + 1
  blue): the mask isolates the four white buttons, but the lone **blue** button
  is absent from the (white-biased) mask and never detected. Pipeline confirmed 4,
  missed the blue entirely.
- *A count-driven fill then places a phantom on the carpet.* To reach the expected
  count, `white-rescue` (and the fill family generally) adds an "edge-supported"
  circle — carpet weave has edges — that lands on empty carpet, not on the missed
  button (measured: on the raw lot, 4/5 circles on-mask, the 5th on carpet with
  0.00 on-button fill). The pipeline correctly routes it to *review*, not
  auto-confirm, so nothing wrong enters inventory — but it is reviewer noise and
  the real button is still uncatalogued.
- Small-lot carpet phantoms (1–2 button lots) show the same fill-phantom, and are
  *deliberately* outside the flood gate (scoped to expected > 5), so they need the
  same fix, not a wider flood gate.

  **Proposed fix (per 4.2's rule — gate the fill on an independent on-target
  check):** when the mask is clean (not flooding), require a fill/`white-rescue`
  proposal to sit on the button mask (non-trivial on-mask fill) before it is
  added; an off-mask "edge-supported" circle on a clean mask is background, so
  drop it. Optionally add a minority-colour mask variant so the blue button is
  seen in the first place rather than back-filled. Validate on the fixtures that
  legitimately use white-rescue (case2, granite_glare, mixed_bluewhite) so the
  guard doesn't suppress real recoveries — this is the risk that warrants the
  operator's non-carpet regression pass first.

## 4.4 Decision (2026-07-12): carpets are too niche to over-code — DASH the gates, ship the count companion

Operator verdict after the pipeline run: **carpets are too niche a case to build
a stack of gates for, and the residual failure is Gemini OVER-COUNTING the busy
background — an upstream input we do not control.** Compensating for a bad input
count with downstream heuristics is the §4.2 overfitting trap in another outfit.
So:

- **Open items #1 (white-rescue on-target gate) and #2 (flag-unreadable) are
  SHELVED.** They are recorded above for provenance; do not re-propose them for
  carpets. The carpet phantoms they targeted are already safely routed to *review*
  (never auto-confirmed into inventory), so the cost of not building them is
  reviewer noise, not bad data.

- **Kept learning (worth knowing, documenting, logging): Gemini is *also* confused
  by these backgrounds.** On turf/patterned carpet Gemini's `total_button_count`
  runs ahead of the buttons it can actually place (image 3: an empty projection
  grid because Gemini returned nothing snappable). The busy background beats the
  mask *and* the reader.

**Companion SHIPPED (both repos) — small, broad-value, and a free data engine:**

1. **Guide detection with the conservative count**
   `expected = min(total_button_count, len(detected_slogans))` (falls back to
   `total − flagged` only when Gemini localised nothing). Caps Gemini's claimed
   total at what it actually localised, so an over-count can no longer drive
   back-fill phantoms — **this one change would have prevented case-7's phantom.**
   No-op on consistent lots: when `total == localized + flagged` it equals the old
   `total − flagged`, verified (10→8 both; over-count 15→8 vs old 13). Lives in
   `main.py` (pipeline handler) in both services.

2. **Log `gemini_count_inconsistent`** in the label record (`label_harvest.py`,
   byte-shared, computed once so both services agree): True when Gemini's claimed
   total ≠ the length of its `detected_slogans` list. Each pipeline lot now emits a
   free **measured-Gemini-error** row (the detector's own count is the circles
   list, for a cross-check), feeding Phase 4c and the Stage-B "Gemini as ruler"
   question with real data instead of guesses. Pipeline stdout also prints the
   inconsistency inline for live triage.
   *(Realigned 2026-07-12 with the updated Gem prompt: Counting Rule 1 now defines
   `total_button_count = len(detected_slogans)` and puts flagged buttons in a
   separate list, so the check is `total ≠ len(detected)` — the earlier
   `≠ localised + flagged` would false-positive on every lot that has a flagged
   button. See `GEMINI_PIPELINE.md` "The Gem prompt".)*

Net: we stopped trying to out-gate a bad upstream count, took the one conservative
count change that removes the phantom class for free, and turned the failure into
a logged measurement that informs whether Gemini can be trusted as the counter at
all.

## 4.5 The real "navy-8 complete fail" cause: Gemini coordinate SCALE (0-100 vs 0-1000) — FIXED

The navy-8 carpet "complete fail" was **never a mask/detection problem, and not
an overcount** — it was a coordinate-units bug, proven from three live label
records + their images:

- **Gemini did not fail navy-8. It aced it.** `button_count: 8`, zero flagged, all
  8 slogans located and read correctly. But the output was a blind projection grid
  with **every circle `gemini_backed: false`** — we discarded a perfect reading.
- **Root cause:** the Gem prompt asks for 0-100 PERCENT coordinates, but Gemini
  **intermittently answers on its native 0-1000 scale**. Downstream
  `gemini_geometry.pct_to_px` divides by 100, so a 0-1000 lot lands ~10x off the
  frame: every point outside the image → `gemini_led_crops` returns empty →
  `reconcile_with_gemini` matches nothing → blind grid. Overlaying the navy-8
  coords **÷1000** onto its own detection-space label image lands **dead on all 8
  buttons** (r≈44, exact). Overlaying ÷100 puts 0/8 on-image.
- **Confirmed inconsistent across lots** (the key evidence):
  | lot | coords | scale | result before fix |
  |---|---|---|---|
  | navy-8 (IMG-1001) | 224–692 | **0-1000** | ÷100 → off-image → blind grid ✗ |
  | single button (IMG-1014) | 40, 45 | **0-100** | ÷100 → matched, `gemini_backed` ✓ |
  | white-carpet ×5 (IMG-0125) | ≤74 | **0-100** | ÷100 → 4/5 matched ✓ |

**Fix (SHIPPED, both repos):** `parse_gemini_response` (byte-shared
`pipeline_ingest.py`) detects the scale from the response's max coordinate — a
percent value can't exceed 100, so **max > 100 ⇒ 0-1000 ⇒ rescale ÷10 back to
percent**; otherwise leave as percent. One normalization point, everything
downstream unchanged. Verified against all three real records (navy-8 → permille,
rescaled, lands on buttons; both percent lots untouched). Logs `coord_scale`
(percent/permille/None) per lot in the label record, so we can measure how often
Gemini ignores the percent instruction. Unit tests in `test_pipeline_ingest.py`.

**Why this matters beyond carpets:** it is a **general correctness bug**. Any lot
where Gemini answers in 0-1000 silently lost its entire localization and fell to a
blind grid — the carpet lots only made it visible because Hough also failed there,
so there was no second safety net. This is the highest-leverage fix in the whole
carpet investigation, and it is a units bug, not a CV problem.

**Still separate / still open:** the white-carpet residual (blue minority-colour
button missed by Hough, a carpet phantom taking its count slot — open item #1) is
NOT a coordinate issue; it persists after this fix (verified: that lot is percent
and unchanged). Resolved in 4.6 below.

## 4.6 Two-signal reconcile swap: a Hough phantom was SUPPRESSING a real Gemini button — FIXED

Open item #1 (the white-carpet "missed blue button") was mechanised from a live
label record: **it was not a separate miss — the carpet phantom and the missing
blue were the SAME failure.**

- 5 Gemini slogans, all located (percent), **including the blue at (472,172),
  confidence 0.9**.  5 Hough circles: 4 whites + 1 carpet phantom (124,480,
  `gemini_backed:false`).  The blue is missing from the output.
- Cause: `plan_reconciliation` caps recovery at the **count deficit** =
  `len(gemini) − len(detected)` = 5 − 5 = **0**.  The phantom inflated the
  detected count, zeroed the deficit, and the genuinely-uncovered blue was never
  recovered.  **The phantom ate the blue's slot.**  (The deficit cap exists to
  stop double-counting on the projection path — it wasn't wrong, just blind to a
  false positive.)

**Fix (SHIPPED):** a **two-signal swap** — the risky action is DROPPING a Hough
circle, so gate it on two independent signals: the circle is **unbacked**
(coverage geometry) AND sits **off the button mask** (`fill < 0.50`, photometric).
Both hold ⇒ it's a phantom; drop it and recover the uncovered Gemini button in its
place (1 in, 1 out, count invariant, deficit-cap guard preserved).  Kill switch
`*_RECONCILE_SWAP`; two-phase so the mask is only built when a swap is plausible.

**The measurement that reshaped the design — worth remembering.** The first plan
gated recovery on the *button's* mask-fill too.  Tested on the REAL pipeline mask,
it failed: **phantom fill 0.0 (great) but the real blue button fill 0.2** — because
the mask that MISSED the blue (blue_or_white flooded on the bluish carpet → white-
only fallback) is the same mask, so it reads ~0 there.  **Mask-fill can flag a
phantom but cannot vouch for a miss** (the mask is the shared failure point).  So
recovery is gated on **Gemini's own confidence** (≥0.90/high) instead — consistent
with the pipeline already trusting Gemini's x/y everywhere.  End-to-end on the real
image: phantom dropped, blue recovered at its Gemini location with its slogan,
count still 5, associations aligned.

**Generalization (SHIPPED) — the drop must not require a button to recover.** The
first 4.6 fire condition only DROPPED a phantom when it could be PAIRED with an
uncovered high-confidence miss (gate `n_uncovered > deficit`), and the swap loop
`zip`'d phantoms to recoverable buttons. Three live label records showed that
misses the common case:

| lot (job) | scene | phantom(s) | uncovered button to recover? |
|---|---|---|---|
| c0f97de7 | 1 real button on carpet | 1 (huge r=116 carpet circle) | **none** — the 1 button is covered |
| 84e75a23 | 31-button corkboard | 2 on bare wood (ann. "24","27") | none spare |
| db3afe3c | 15-button board | 1 on bare wood (ann. "12") | none spare |

In every case `deficit = 0` and the phantom is COVERED-count-neutral, so
`n_uncovered > deficit` was false and the probe never ran — the phantom shipped
as an extra button. **An unbacked off-mask circle is a Hough false-positive
whether or not a Gemini button can take its place** (Gemini located all its
buttons; an unbacked off-mask circle is not one it merely missed). So: (1) the
detect-side gate now fires on `n_unmatched_crops` alone; (2) `plan_reconciliation`
DROPS every off-mask phantom, PAIRS each with an uncovered high-conf miss where one
exists (true swap, count invariant), and drops UNPAIRED phantoms outright — they
only inflated the count. Each drop logs `recovered` (bool) so paired vs unpaired
drops are distinguishable in `det_reconcile_swaps_json`.

**Same root cause fixes the "misparsed slogan" symptom.** The operator also saw
slogans mislabeled. It was not a JSON parse bug (`json.loads` handles embedded
apostrophes like `PSU Dots the 'I' In Win`; `parse_gemini_response` drops only
non-dict/empty entries). A surviving phantom in the FINAL crop set consumes an
`associate_slogans` nearest-neighbour slot, stealing a real button's slogan and
shifting the labels after it. Dropping the phantom BEFORE association (reconcile
removes `dropped_crop_indices` from `final_centers`) fixes the count AND the
labeling in one move.

## 4.7 The recurring error class — audit heuristic (operator's "look for errors like this")

Three of the carpet failures (4.4, 4.5, 4.6) were the **same shape**: **Gemini read
the lot correctly and the pipeline threw its answer away.**

| § | Gemini got right | how we discarded it |
|---|---|---|
| 4.4 | localized N buttons | trusted its inflated *scalar* count over the localized ones → phantom back-fill |
| 4.5 | 8 buttons, exact x/y | misread its coordinate SCALE (0-1000 as 0-100) → points off-image → blind grid |
| 4.6 | blue button, x/y + conf | a Hough phantom zeroed the recovery deficit → its localization suppressed |

**Heuristic when a lot fails: FIRST check whether Gemini already got it right.**
The label record has everything to tell — `gemini.button_count`, `slogans` with
x/y + confidence, `coord_scale`, `count_inconsistent`.  Overlay the slogans on the
detection-space image (`pipeline/labels/<job>.jpg`).  If Gemini's reading is good
but the output is wrong, **the bug is in how we CONSUME Gemini, not in Gemini or
the detector** — and it is usually cheap and general to fix (a scale divide, a
count min, a swap), unlike the genuinely hard mask/detection work (Part I).  These
are the highest-leverage bugs in the pipeline; look for them before touching
detection.  Signals that a lot is in this class: `detector_used == "grid"` with
0 `gemini_backed` circles (4.5); `count_inconsistent == true` (4.4); an unbacked
off-mask circle coexisting with an uncovered high-confidence slogan (4.6).

## 4.8 HYPOTHESIS (under test): flip to Gemini-anchored, Hough-refined layout

**Claim.** The architecture is backwards. Hough is the *primary* detector and
Gemini's x/y only a fallback (`gemini_led_crops` on grid-collapse) + a patch
(`reconcile`/swap). But every carpet/turf/low-contrast failure this session was
**Hough garbage + Gemini right**, and we keep re-deriving "trust Gemini's x/y" one
patch at a time. Gemini supplies a position for *every* button, so the natural
design is **Gemini-anchored, Hough-refined**: place one crop per Gemini button at
its x/y; snap to a nearby Hough circle for the exact centre/radius when one exists;
else use Gemini's position + edge radius. That makes phantoms **structurally
impossible** (you never crop where Gemini sees no button) while keeping Hough's
precision where it helps.

**Why it's a hypothesis, not a patch.** It makes us fully dependent on Gemini's
*count* (over/undercount propagates directly — the new prompt's anti-hallucination
rules, §4.4 companion, are the precondition), and the anchored crop's quality for
CLIP is unproven. So: **measure before flipping.**

**The shadow (shipped 2026-07-12, measurement-only, zero extra cost).** The
reconcile match already *is* the A/B comparison — covered buttons are ones Hough &
Gemini agree on, the match distance is how far Gemini's centre is from Hough's,
misses are Gemini-only, unmatched crops are Hough-only. `plan_reconciliation` now
emits a per-lot `gemini_anchored` summary, logged to the `det_gemini_anchored_json`
Sheet column:

| field | reads as |
|---|---|
| `snap_px_median` / `snap_frac_median` | how tight Gemini's centre is vs Hough's (as px and as a fraction of a radius). **Small ⇒ Gemini can anchor the crop.** |
| `n_agree` | buttons both found (the snap sample) |
| `n_gemini_only` | Hough missed — anchoring would **ADD** these (join to confirms: are they real?) |
| `n_hough_only` | Hough circles no Gemini backs — anchoring would **DROP** these (the phantoms) |

**First real datapoint** (example-3 white-carpet 5-lot): `snap_frac_median 0.072`
(Gemini's centres ~6px from Hough's, 7% of a radius), `n_gemini_only 1` (the blue
Hough missed), `n_hough_only 1` (the carpet phantom). i.e. on this lot anchoring
would add the blue, drop the phantom, and place the 4 agreed buttons essentially
where Hough did. Promising, but n=1.

**Measure "how often is Gemini's x/y correct?" against the FINAL decision.** Each
shipped crop carries its source and its associated Gemini slogan (`crop_to_slogan`),
and `confirm_log` carries the outcome per crop, joined on `job_id`+`crop_num`. So
over a crawl: of Gemini's positions, how many map to a crop that *confirmed* to a
real button (correct) vs none (Gemini hallucinated) vs a real button with no Gemini
position (Gemini missed). That precision/recall of Gemini's x/y against confirmed
truth is the go/no-go for the flip.

**Signals to watch for defaulting to Gemini x/y (the "over time" goal).** low
`snap_frac_median` (tight positioning); high `n_hough_only` correlated with
`det_mask_coverage` high / turf-carpet backgrounds (Hough phantoms cluster there);
`n_gemini_only` buttons that consistently confirm (Hough's misses were real). When
those hold on a background class, that class should **default to Gemini x/y** — the
first concrete step toward the flip, ahead of a full switch.

**Next step (not yet built):** once the shadow shows the agreement holds, add a
`GEMINI_ANCHORED` flag that actually *ships* the anchored layout for an A/B crawl,
so the confirm-rate comparison is downstream-real, not just positional.

## 4.9 The flood gate overfit its own calibration set: dense lots ARE a flooded mask — FIXED (2026-07-15)

The §4.2 lesson applied to the §4.1/4.3 guard itself.  A clean dense lot (11
large Mellon-1984 buttons on tan cardboard, operator-reported) came back as
eleven full-width projection strips: Hough had found a PERFECT 11/11 set
(radius std 2.6, zero rejections, every centre on a button — verified by
running the real detector in-session), and `_guided_mask_floods` REFUSED it
because the mask read 58% > 50%.  But 11 buttons at r≈85 on a 644×800 frame
cover ~50% of it *by themselves* — the "flood" was the buttons.  The gate was
calibrated on turf 68% vs case1's 30% with nothing dense in between (§4.2:
six examples is not a distribution — this time the GUARD was the overfit
small-N feature).

**Fix (SHIPPED, both repos, same §4.2 shape — an independent on-target check
before the destructive action):** before refusing an accepted guided set, ask
whether the circles EXPLAIN the mask: `_circles_explain_mask` = fraction of
mask foreground covered by the accepted circle disks.  Dense-Mellon measures
**0.834**; turf/carpet by area arithmetic sits ~0.1–0.3 (a flooded background
vastly exceeds its circles).  `FLOOD_EXPLAINED_MIN = 0.60` splits them with
wide margins; `_flood_refusal_decision` is pure and unit-tested, a failed or
erroring check degrades to the shipped refusal (never a silent accept), and
`guided_flood_explained` is logged per lot.  Scoped to the whole-acceptance
refusal fork only — the deficit-fill branch (starved Hough, §4.1 turf) is
unchanged, because an incomplete circle set under-explains by construction.
Verified: dense-Mellon restored 11×1-grid → hough 4×3 11/11 (buttonmatcher
AND ebayscout parity on identical bytes); all 15 prior fixtures + gate tests
unchanged (35 pass); the lot is committed as `dense_mellon_11.png` with a
guided regression test locking hough-path + all 11 centres.


## 4.10 The hole-inversion ate a good mask: slogan-text blocks read as "button holes" — FIXED (2026-07-15)

Same class as 4.9 — a rescue path replacing a good result — reported as a
"confusing" 6-lot failure: 6 blue Citizens buttons on white speckled paint
came back as 4 offset projection rectangles with two buttons uncropped.  Run
in-session: the `blue_only` mask was GOOD (mask-scale prior r_est=107,
conf=0.98) — but the **dark-on-light hole-inversion** replaced it.  The
multi-line slogan-text blocks inside each button are enclosed holes, and
after the morphology close they pass the circularity/area filters: 15
"circular button-holes", cov 0.043, r~21.  Hough on the text-speck mask found
1 circle, the fill filter (against the same broken mask) killed it → 2×3
projection with 2 cells dropped as "background-only" → the 4 offset rects.
The inversion's guard premise ("holes cannot occur when buttons ARE the
foreground") was refuted — text holes are exactly that; the existing
synthetic test only covered THIN specks, which the filters do drop (§4.2:
synthetics are small-N too).

**Fix (SHIPPED, both repos):** `HOLE_INVERT_MIN_COVERAGE = 0.08` — the kept
holes must cover ≥ 8% of the frame before the inversion may replace the mask
(the same 8% plausibility floor the mask variants already use).  Measured:
real buttons-as-holes (dark_on_white_13) = **0.157**; the text-hole failure =
**0.043** — ~2× margin both ways.  A sub-floor holes mask now means "leave
the mask alone".  Verified: the 6-lot restored 4-offset-rects → hough 2×3
6/6 (and unguided healed too: garbage → 6 @ `scale_first`), buttonmatcher +
ebayscout parity on identical bytes; dark_on_white_13 still inverts; full
battery 41 green.  Fixture `blue_on_white_text_6.jpg` + a real-shape
text-block unit test lock it.

**Pattern note (4.9 + 4.10, one session):** both failures were *rescue paths
firing on lots that did not need rescuing* — the flood refusal discarding a
perfect Hough set, the hole-inversion discarding a confident mask.  When a
lot fails confusingly, check the §4.7 heuristic first (did we discard a good
Gemini read?), then this one: **did a rescue/fallback OVERRIDE a good primary
result?**  The telemetry tells: a confident upstream signal (`mask_radius_
conf` 0.98, `det_raw_hough` == expected with tight radius std) followed by
`detector_used=grid`/`projection` or a `+holeinvert`/refusal tag.


## 4.11 Two more doors into the projection-strips failure — the flood floor overfit AGAIN, and the fused band under the saturation trigger (2026-07-16)

Operator report: "a lot of the same errors in /sort" — full-width strip crops.
Both lots run through the real detector in-session; two distinct mechanisms,
both now fixed and fixtured:

- **CCB-11 on dark wood — the 4.9 fix's own §4.2 event.**  Guided Hough found
  every button (12 raw, 11 kept, radius std 2.9) and the flood gate refused
  them: mask 61% (the wood leaks into the blue range), circles explained only
  53% < the 0.60 floor.  The explained-RATIO was calibrated on ONE positive
  case (dense-Mellon 0.83) and conflates "leaky mask" with "circles on
  background".  **Fix: judge the RESIDUAL** — mask area OUTSIDE the circles
  as a fraction of the frame (dense-Mellon 0.10, CCB 0.30 → accept;
  navy-carpet ~0.56, turf ~0.58 → refuse; `FLOOD_RESIDUAL_MAX = 0.42`, ~0.14
  margins both ways).  Restored 11/11 hough; carpet margins re-verified in
  the pure tests.
- **Mellon-13 on a white envelope — the fused band below the trigger.**  The
  envelope passes the white range → one fused sheet at **72% coverage, just
  UNDER the 0.75 saturation-fallback trigger** — so no rescue fired, Hough
  found 0 of 13, and the count-guided projection shipped 13×1 strips.
  Lowering the trigger blindly was rejected (healthy dense lots live at
  0.58-0.61 — thin margins).  **Fix: the starved-Hough fused-mask retry** —
  only when pass-1 finds under 30% of `expected` AND coverage sits in the
  0.50-0.75 band, rebuild with the SAME satfallback variants and re-run
  Hough once (`_fused_mask_fallback`, tag `+fusedretry_*`).  Gated on the
  demonstrated failure of the current mask (§4.2), so a working lot can
  never switch.  On the real lot: blue variant (34% coverage) → 12 circles +
  white-rescue recovers the white Fiesta button = **13/13** (the whitepass
  earning its keep on exactly its designed case).

Fixtures `ccb_darkwood_11.jpg` + `mellon_envelope_13.jpg` lock both; battery
43 green; buttonmatcher/ebayscout parity verified on identical bytes.
**Meta-lesson (now twice):** every constant calibrated on one positive
example WILL meet its counterexample; prefer quantities whose accept/refuse
populations separate by construction (residual area) and rescues gated on
the primary path's demonstrated failure (fused retry) over threshold
tuning.


---

# Part V — slogan auto-confirm signals (Logger_14 dual-run, 2026-07-15)

*Data: Logger_14 — the operator's dual-run batch (same buttons through the
Gemini pipeline and `/sort`), 731 gradeable buttons / 69 wrong-#1 rows. Full
working analysis: `buttonmatcher/log_analysis.md` (Logger_14 layer). Method:
replay every confirmed button's match-time leaderboards against
operator-reviewed truth; price every candidate rule by wrong autos, not
coverage.*

**Confirmed**

- **gap_only (raw #1→#2 gap ≥ 0.15) — third independent validation:**
  222/222 correct, **452/452 cumulative** across Logger_11/12/14. Flipping
  `BUTTONMATCHER_GAP_ONLY_LIVE=1` is justified; raises `/sort` auto coverage
  of the Gemini-certified set from 23.3% → 43.8% with zero code.
- **The raw gap is edition-blinded; the slogan-level gap is the fix.** Gap to
  the first candidate with a *different normalized slogan* (edition siblings
  crowd the top and crush the raw gap without contesting the slogan).
  `slogan_gap ≥ 0.12`: 327 fires, 0 wrong (worst wrong row = 0.0956, so
  0.12 keeps a real cushion; 0.10 cleared this batch by only 0.004 — hold it
  for a second clean batch). Cumulative union at 0.12 → 52.6% coverage.
- **ref_sim separates right from wrong at entry level — but only as a second
  signal** (the VISION §5 calibration question, first real read since the
  #116/#50 fix). Two-signal combo `slogan_gap ≥ 0.05 AND ref_sim ≥ 0.90`:
  +42 confirms, 0 wrong, and the arms veto each other's failure modes —
  wrong rows passing the gap arm read ref_sim ≤ 0.802; wrong rows passing
  the ref arm are visual near-twins (≤ 0.888) with slogan_gap ≤ 0.0093.
  The full ladder (0.85-score ∪ gap_only ∪ slogan_gap ∪ combo) certifies
  **68.9% of what `gemini_auto` certifies, 0 wrong slogans in 731**.
- **The twin guard is load-bearing for score-based auto TODAY:** 4
  right-slogan/wrong-year edition twins score above the live 0.85 bar
  ("MSU Green With Envy" at 0.941). Every certified wrong-year case in the
  batch (13) was an edition twin visible to the registry (`fam_size > 1`).
  Slogan-level certification keeps the edition picker; no rung outranks the
  blocks. (Cross-sport block vetoed 0 correct rows — it costs nothing.)

**Refuted**

- **"ref_sim is an independent signal as logged."** 74% of leaderboard
  entries have ref_sim *identical* to `image_score` (median |Δ| 0.0003); it
  diverges on a minority of rows and only there adds information. Never use
  it alone (best solo zero-wrong threshold cleared the worst wrong row by
  0.002 — noise).
- **"A score/gap/ref boost can fully match Gemini."** 3.6% of `gemini_auto`
  confirms had the truth at CLIP rank 2–6 (unreachable by any #1-certify
  rule), and 35 typed rows had the truth in no leaderboard (reference
  coverage gaps, Phase 4b). The reader keeps the residual.

**Validated on Logger_15 (2026-07-15 batch, 256 reviewed rows) — VERDICTS**

- **`slogan_gap ≥ 0.12`: LIVE.**  Second independent clean batch — 121/121
  (cumulative **448/448** across Logger_14/15) — satisfying its documented
  gate.  Flipped 2026-07-15 (source `auto_slogan_gap`, kill switch
  `BUTTONMATCHER_SLOGAN_GAP_LIVE=0`, annotated-image status pass mirrored,
  both blocks still win, the unvalidated all-one-slogan snapshot never
  fires).
- **`ref_combo` (sgap ≥ 0.05 AND ref_sim ≥ 0.90): REFUTED at these
  thresholds — permanent shadow.**  Its first validation batch produced a
  wrong fire: "A&M: Remember the Nittany Lions" carried **ref_sim 0.986**
  against a "Here Come The Voluntears" button (sgap 0.057) — a visual twin
  WORSE than the 0.968 wrestling pin.  ref_sim alone can hit ~0.99 on the
  wrong button; the combo stays measurement-only (`SLOGAN_GAP_SHADOW
  rule=ref_combo` lines) unless a future calibration finds a safe shape.
  The shadow-first discipline (§4.2) caught this before it cost an
  inventory error.
- **`gap_only` (raw ≥ 0.15): fourth clean batch** — 85/85 on Logger_15,
  cumulative **537/537**.
- **Deep-agreement tiers: first live day.**  31/233 `gemini_auto` confirms
  (13.3%) arrived via the new tiers — 10 deep-pool (truth at leaderboard
  rank 4-10) + 21 DB-direct (truth on NO year-folded board row at all) —
  vs a 0.7% deep share and ZERO off-board in pre-fix Logger_14 (the
  attribution control).  Typed entries fell 35 → 2 per batch; "I-Oh-Was"
  auto-confirmed.  No `correction` rows logged against any deep-tier
  confirm.  The residual typed class is structural: puns Gemini reads in
  normalized form ("Voluntears" as "volunteers") — agreement cannot fire
  on a normalized mismatch; a fuzzy-agreement tier is the candidate lever
  if volume warrants.
- **Stage-B accrual:** gated `auto`+`scale_first` vs Gemini count 13/13
  exact on the day's feed (streak continues).

---

# Part VI — the text-blocked pun slogans (Logger_14 layer 2, 2026-07-15)

*Operator question: why do "I-owa Doubt It" / "I-O-Wouldn't" / "I O Won't" /
"I-Oh-Was" / "Stuck In a Rut" never rank top-3 even when Gemini reads them
correctly?  Full working analysis: `buttonmatcher/log_analysis.md` (Logger_14
layer 2).  Method: leaderboard replay against operator truth + code trace +
fixes verified by replaying the real shipped functions over the batch.*

**Confirmed**

- **CLIP text-side deficit is real and phrase-level.** These puns' own-crop
  text_score reads 0.35–0.44 vs the batch own-crop truth median 0.671
  (n=698) — bottom ~5% — while their image/ref side is fine ("I-O-Wasn't":
  best-in-board image 0.853 + ref_sim 0.853, ranked 10).  Mechanism: one raw
  punctuated string per slogan into CLIP, cosine ~0.22–0.24, squashed by the
  `normalize_slogan` [0.15,0.35] window.
- **Year-folded leaderboards create within-year shadowing.** One row per
  year (best slogan per year) means "I-owa Doubt It" 1995 is eclipsed by
  "Michigan Impossible" 1995 at EVERY depth — no top-N widening alone can
  surface it.  The agreement pool additionally sat at the trimmed top-3 in
  buttonmatcher only (ebayscout has always passed 10).
- **The typed rescue was tokenizer-broken for apostrophe puns.**
  "wasn't"→wasn+t meant typing "I o wasnt" scored 0.067; hyphen-only
  "I oh was" scored 1.148.
- **Fixes verified by replay:** deep agreement pool (top-10) + DB-direct
  tier (Gemini read is a known DB slogan, conf ≥0.85) + apostrophe-safe
  tokenize → **10/10 Logger_14 family rows resolve `gemini_auto` with
  correct slogan AND year** (7 required typing before).  Kill switch
  `BUTTONMATCHER_GEMINI_DEEP_AGREE=0`; `matched_rank` logs the stratum.

**Refuted**

- "STOPWORDS/rarity penalize all-stopword puns" (i/oh/was aren't stopwords;
  rarity only adds, capped 0.04).
- "Twin registry or key normalization shadows the family" (distinct keys).
- "Reference coverage gap" (ref_sim 0.64–0.85 where charted).
- "A deeper top-N alone fixes it" (within-year shadowing survives any N —
  the DB-direct tier was required; replay proved 3/10 → 10/10).

**Newly instrumented / pending validation**

- Deep-pool + DB-direct precision per `matched_rank` stratum accrues in
  confirm_log from the next batch; any wrong deep auto → flag off.
- `BUTTONMATCHER_TEXT_VARIANTS` (default OFF): punctuation-normalized CLIP
  text variants, max-per-year additive.  Enable after A/B run clean; judge
  on family own-crop text_score (~0.4 → 0.55+ expected) and no new wrong
  #1s.  ("Stuck In a Rut" gets no variant — unpunctuated; its rescue is the
  agreement tiers.)

**Layer 3 (2026-07-15) — the per-slogan baseline advantage: CONFIRMED, centering shadow SHIPPED**

- **Confirmed: slogans carry a de-facto text advantage independent of the
  crop.**  Per-phrase background text_score (mean over appearances where the
  phrase is NOT the truth, n≥3) spans **0.34–0.80 across 515 phrases** in
  Logger_14 — a "hot"-embedding slogan ("Lady Lion Proud" 0.80) starts every
  contest up to +0.46 text_score (+0.23 overall) ahead of a "cold" one
  ("Deny Lehigh" 0.37) before any crop evidence.  This is the attractor
  disease ("Penn State Pins To Win" topping 17/72 listings, 2026-05-30)
  quantified, and the driver of the within-year shadowing above.
- **The correct normalization axis is per-SLOGAN, not per-year:** center each
  row's cosine on that slogan's own cross-crop baseline (within-year
  normalization on a single crop cannot tell "hot embedding" from "actually
  matches").  Precedent: the ref_sim CENTERED adjustment (2026-07-03
  "Plaster Pitt") is the same move for reference similarity.
- **Log-replay effect is positive but the test is structurally limited:**
  re-ranking within logged top-10 boards moves truth@#1 94.5% → 95.5%
  (17 improved / 10 worsened / 464 same).  It cannot see the cases centering
  should help most (truths shadowed OFF the board), and top-10-only baseline
  estimates are biased — the honest verdict needs the live shadow.
- **Shipped (measurement only): `rank_centered`.**  Baselines = mean cosine
  of each text row vs the ENTIRE reference bank, one matmul at hydration
  (`_recompute_text_baselines`, fail-open); `build_centered_leaderboard`
  (byte-shared match_logging.py) ranks each crop with centered text; the
  confirmed year's rank lands in the appended `rank_centered` confirm_log
  column.  Live ranking untouched.  **Decision gate:** flip only if centered
  beats raw at scale on confirmed truths (including off-board ones), and
  only WITH a recalibration of every score threshold (0.85 auto, green,
  gap rules) — they are all calibrated to today's distribution.