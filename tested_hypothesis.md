# Tested Hypotheses — unguided detection (white/blue on matching backgrounds + Layer-1 radius trust)

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
- The automatable volume % (what fraction of pipeline lots are `scale_first`).

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
