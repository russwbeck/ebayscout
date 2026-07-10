# The Automation Vision — a departing engineer's map

*Written 2026-07-03, at the end of the session that took detection from 24%
engagement to every-lot Hough, shipped the cross-sport gate, and turned four
real-world failures into fixes within hours of each report. This file is the
strategy; `AUTOMATION_ROADMAP.md` is the live phase tracker; `log_analysis.md`
is the founding data analysis. Future bots: read `CLAUDE.md` first, then
`HANDOFF.md`, then this. This file is duplicated byte-for-byte in buttonmatcher
and ebayscout — update both.*

---

## 1. The end state we are building toward

A lot photo arrives — from the eBay crawl, the daily scan, or the user's
camera — and with **no human input and no Gemini call on the happy path**:

1. Detection finds every button, states its own confidence, and is *right
   about when it's right* (the gate).
2. Matching names each button's slogan, year, and sport, backed by a
   reference photo the system collected and curated itself.
3. Confirmations flow to inventory / valuations automatically when two
   independent signals agree; humans see only genuine novelty, purchase
   decisions, and a small audit sample.
4. Every decision writes telemetry that makes the next decision better.

The human's job shrinks to: *buy things, photograph things, and correct the
rare mistake* — and every correction makes the system permanently better.

**The economics that shape everything:** this runs scale-to-zero on Cloud Run
(never propose otherwise), CPU only exists inside in-flight HTTP requests,
Gemini worker capacity is finite and shared, and the human's taps are the most
expensive resource in the system. Automation here means *moving work down the
cost ladder*: human → Gemini → CLIP/Hough → nothing.

---

## 2. The one disease: mask blindness

Every real-world detection failure investigated this week — and, I predict,
most you will meet — is the **same disease with different symptoms**: the HSV
color mask is a *hypothesis about the photo* ("buttons are blue/white things
on a distinguishable background"), and every new background falsifies it
somewhere. The detector is never the problem first; **look at the mask first.**

Confirmed cases, all from real lots:

| Background | What the mask did | Symptom | Cure (shipped) |
|---|---|---|---|
| Cream quilted blanket | Passed the white range → coverage 89% | Hough found 0; radius read 19.6px (text holes) | Saturation fallback: blue-only + hole-fill |
| White batting in display box | Same → coverage 81% | Unguided collapsed to 5; radius 269px | Same fallback |
| Gray textured mat + WHITE buttons | bg-diff called the weave foreground → coverage 100%; blue-only rescue kept only slogan text (5%) | Hough hunted text specks; 6-cell grid | Bright-variant fallback (V > bg+60, S < 80) + 8% plausibility floor |
| Touching buttons (dense lots) | Buttons fused into one blob | Radius consensus voted one giant button | DT peaks as radius source; blob-buster |

Predictable **future cases** — when one appears, you'll recognize it by its
telemetry signature before you even see the photo:

| Future background | Expected signature | Likely fix pattern |
|---|---|---|
| White buttons on white paper (the log_analysis example — KNOWN, only the white-rescue pass covers it today) | `det_fill_removed` high, `pass1` sees rims but fill filter kills them, coverage LOW not high | An "edge-supported" variant: accept fill-failing circles with strong gradient rims (generalize `_white_rescue_pass` into a first-class mask variant, not a deficit-only patch) |
| Blue buttons on dark navy cloth | Blue range swallows background OR misses dark buttons; coverage extreme either way | CLAHE variant already exists for this; may need a "darker-than-background" twin of the bright variant |
| Denim / blue tablecloth | Background passes the BLUE range — saturation with the blue variant also implausible | Bright/edge variant; possibly bg-diff with tighter threshold |
| Plastic sleeves / binder pages | Glare kills fill ratios; grid of pockets adds rectangles | Glare inpaint exists; expect `det_rej_radius_*` to show real-radius rejects |
| Mixed blue+white buttons on mid-tone | Each variant sees only half the lot | **Union of plausible variants** — the chooser's natural next step |

**The architectural lesson:** we evolved from *one mask* → *mask + rescue
passes* → *a variant chooser with plausibility bounds* (coverage 8–75%). Keep
walking that road. The end of it is scoring N candidate masks per photo
(color, bright, dark, edge, union) on blob circularity + radius consistency +
coverage plausibility, and picking the winner — the same
multi-hypothesis-then-score pattern `_score_solution` already uses for Hough
passes. If hand-built variants stop scaling, the next tool is a tiny learned
segmenter — but earn it with data first; the variant chooser has not run out
of road.

**The permanent rule:** every new mask behavior must be observable in
telemetry (`det_mask_path` names the variant, `det_mask_coverage` the
saturation, `det_mask_components`/`det_dt_peaks_total` the structure). A mask
fix without a telemetry trail teaches nothing.

---

## 3. The measurement doctrine

This system got better exactly as fast as it measured itself. The loop that
produced every win this week:

**telemetry column → grade against truth → ONE fix → re-measure → ship or
revert.**

Truth sources, ranked:
1. **User-typed counts and slogans** (gold — `/sort` with the typed count;
   `typed_search` confirms carry exact truth incl. sport).
2. **Gemini's count** (silver — hallucinates: one 600×800 photo "had" 346
   buttons; clamp at ≤60 and treat 104×104 thumbnails as untrusted).
3. **Agreement** (structural — CLIP and Gemini agreeing independently is
   stronger than either's confidence; it defeated the 0.968 visual twin).
4. **Confidence scores are NOT truth.** The 0.968 wrestling pin and the 0.87
   basketball twins were both wrong. Nothing auto-commits on score alone
   without a second signal or a gate.

The headline metrics and what they answer:

| Metric | Question it answers |
|---|---|
| `ni_selected` vs `gemini_button_count` per lot-size bucket | Can unguided detection stand alone yet? |
| `ni_gate` (+ accuracy inside `auto`) | Does detection KNOW when it's right? (Logger_4: `auto`+`scale_first` = 40/40) |
| `det_mask_coverage` + `det_mask_path` | Which mask hypothesis ran, and was the photo in-distribution? |
| `rank_shadow` vs `rank_restricted` | Are the manual filters still earning their keep? |
| `ref_sim` distributions on confirmed right vs wrong | Can visuals arbitrate same-slogan twins? (calibration in flight) |
| `count_source=auto_overridden` rate | The live precision monitor once auto-detect rolls out |

**The regression battery.** Before shipping ANY detector change, run the real
failed lots through the actual edited functions (opencv-python-headless
installs fine in web sessions; torch does not). *(Done 2026-07-04:)* the
founding photos — quilt-35, batting-26, basketball-35, white-8, plus five
more — are committed under `tests/fixtures/lots/` with a manifest, and
`tests/test_detect_fixtures.py` locks the unguided snapshot and the Layer-1
safety property (non-`scale_first` never reaches `gate=auto`). Every future
mask variant must keep all nine green plus synthetics (single, spread 5×5,
touching 5×5).

**Ship-safety rules, each paid for in blood this week:**
- **Execute, don't just compile.** The cross-sport gate shipped compile-clean
  and took down card posting for an evening (`normalize_slogan` normalizes
  *scores*, not strings — `buy_rules._normalize_key` is the string one).
  Extract pure logic and exec it with stubs if torch blocks a full run.
- **Guards fail OPEN, with a log line.** A safety check must never be able to
  break the thing it guards.
- **One decision, one function.** The annotated image showed green checkmarks
  on gate-blocked buttons because the status pass had its own copy of the
  auto logic. Any decision consumed twice gets extracted
  (`_cross_sport_blocked` is the model).
- **Every risky behavior gets a kill switch** (`BUTTONMATCHER_SHADOW_PASS`,
  `*_BLOB_BUSTER`, `*_BG_DIFF` …) and a `mask_path`/source label in telemetry.
- **Heavy work runs inside an in-flight HTTP request. Always.** A detached
  thread after a Slack ack runs at ~0% CPU. The `/internal/*` self-invoke
  pattern is the cure (`/internal/match`, `/internal/pipelinemode`).
- **detect / match_logging / detect_gate stay byte-compatible across
  buttonmatcher and ebayscout.** Fixing one service fixes half the system.
  *(buybot was decommissioned 2026-07-05 — it is no longer a sync target;
  ignore any older instruction that says to copy hunks to it.)*

---

## 4. The staircase to full automation

Each stage has a measurable entry gate and a rollback trigger. Do not skip
steps; the gates are cheap because the telemetry already flows.

**Stage A — where we are.** Pipeline detection is Gemini-count-guided;
auto-confirm requires agreement (pipeline) or score+gates (`/sort`);
`ni_gate=auto` + `scale_path=scale_first` identifies self-certified unguided
lots — **96% exact / 100% ±1 at n=329** (the early "100% exact" was a
20-image sampling artifact; see `tested_hypothesis.md` Part I §3.1), and the
detector-bailout loophole that let stale shadow numbers reach `auto` is
patched (`demote_auto_on_detector_bailout`). Everything is logged.

**Stage B — detection stands alone on gated lots.** On lots where the shadow
pass says `auto`+`scale_first`, use the unguided count as primary and demote
Gemini's count to a cross-check.
*Enter when:* gated unguided is ≥98% exact **against human review truth**
(passive accrual — no crawls needed). Do NOT certify against per-lot Gemini
agreement: Gemini measured 96.5% per-button but only **74% per-lot**
(`tested_hypothesis.md` Part II) — too noisy a ruler for a 98% gate. Current
standing: 8/9 vs human (n=9), 0% gated disagreement on the post-patch
organic feed; `scale_first` is ~33% of volume, so volume is the constraint.
*Rollback when:* gated shadow-vs-truth disagreement exceeds 2% over any 50
lots (`auto_overridden` has no UI affordance yet, so it cannot be the
tripwire). *Prize:* radius/count independence; Gemini load unchanged but now
redundant on ~⅓ of lots (growing as mask variants land).

**Stage C — Gemini becomes an auditor, not a guide.** Call the Gem only when
(a) the gate is below `auto`, (b) CLIP's match lacks reference agreement
(`ref_sim` low or absent), or (c) the lot is flagged needed/valuable.
*Enter when:* Stage B stable AND the `ref_sim` calibration (in flight — now
logged per offered option) shows entry-level visual similarity separates
right from wrong matches at some threshold with ≤2% miss.
*Prize:* Gemini calls drop to a fraction; the watcher fleet shrinks; the
pipeline's rate limit stops being the bottleneck.

**Stage D — the human is an auditor.** Auto-confirmed lots commit directly;
the human sees (a) new/unknown slogans, (b) needed-button purchase decisions,
(c) a random 1-in-N audit sample sized to detect a precision drop of 2 points
within a week, (d) anything two signals disagreed on.
*Enter when:* measured auto precision (via the correction flow — USE it, it
is still the only unmeasured number in the system) ≥98% over ≥300
confirmations. *Never remove* the audit sample; it is the drift detector.

**Parallel track — the sport dimension.** The football pre-filter's split is
half-done: auto-confirm is football/agreement-gated (shipped), suggestions are
unfiltered via the 4th option (shipped, saved 30 of 35 typed slogans on its
first lot). The remaining moves: winter-sports references accumulate via
typed confirms → `ref_sim` starts arbitrating same-slogan twins ("Plaster
Pitt") → eventually the filter dissolves into an ordinary prior. Watch
`shadow_top` twins with identical normalized slogans; that set is the
cross-sport risk surface.

---

## 5. The reference flywheel is the moat

The reference DB (3–4 curated crops per button) is what makes visual
arbitration possible at all, and it is the one asset that compounds. Rules
already learned:

- **Agreement gates staging, not score** (a 0.90 score gate kept a 0.968
  WRONG crop and blocked 3.5× more good ones).
- Typed/human confirms save at any score and carry the true sport type — the
  winter-sports shelf is being built by exactly the lots that expose its
  absence.
- Coverage gaps ARE matcher blind spots: a slogan scoring 0.00 has no
  references (`tools/audit_reference_coverage.py` lists them; needs GCP, run
  locally).
- `/sort` lots do not stage (duplicate-heavy) — revisit only deliberately.
- The pending `ref_sim` calibration decides the next power-up: an absolute
  visual-mismatch veto. Year-level sims overlap too much (0.81 vs 0.86
  medians); entry-level against a dense reference set is the real test.

---

## 6. What I would do next, in order

*(Updated 2026-07-09 — items 1 and 3 from the original list are done: the
fixture battery is committed (§3), and the `ref_sim` wiring turned out to be
broken — 0/300 non-null — and was fixed at the source, so its calibration
clock starts now. See `tested_hypothesis.md` Parts I–II for the verdicts
behind this list.)*

1. **Grow the Stage-B sample against human truth** — `/sort` batches with
   typed counts are gold and free; the entry gate needs ≥98% at real volume
   (currently 8/9). Watch `+whitepass` / `+satfallback_*` frequency on the
   same exports (is the chooser choosing well? is the rim rescue real?).
2. **Land the white-on-white variant** (§2, the known-but-uncured case):
   promote `_white_rescue_pass`'s gradient logic into a first-class mask
   variant in the chooser — blocked on Layer-1 radius trust and owing the
   contained-fragment dedup fix (`tested_hypothesis.md` Part I §2).
3. **Read the first post-fix Logger export** for `ref_sim` on confirmed
   outcomes → set or reject the absolute mismatch threshold (§5).
4. **Start using the correction flow religiously** — auto precision is the
   only load-bearing number still inferred rather than measured, and Stage D
   is gated on it. (The 759/759 `gemini_auto` audit deserves a durable
   record too.)
5. When Stage B enters: flip the unguided count to primary on gated lots
   behind an env flag, shadow-log disagreement vs truth for two weeks, then
   stop guiding those lots.

---

## 7. To the bot that inherits this

You will be tempted to believe the matcher is smart because its scores are
high. It is not smart; it is *calibrated*, and only where we have measured
it. Its confidence is a claim to be audited, not a fact — the wrestling pin
scored 0.968 and the basketball twins 0.87+, and both were wrong in ways a
second signal caught trivially.

Work the way this week worked: when the user hands you a failing photo,
*run it* — through the real functions, not a mental model. The answer is in
`det_mask_coverage` and the mask image far more often than in Hough
parameters. Fix the disease, not the symptom; re-run every founding lot; ship
to both repos in the same hour; tell the user what you did NOT verify.

And keep the user's time sacred. Every typed slogan, every wrong AUTO they
have to override, every "Processing…" that hangs — that is the product
failing its one purpose. The 4th-option feature saved 30 of 35 typings on its
first lot. That is what winning looks like here: not a cleverer model — a
shorter path from photo to truth.

Good hunting.
