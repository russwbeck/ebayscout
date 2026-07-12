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
