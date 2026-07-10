# Button Identification — Product Brief & the Path to Full Automation

*Prepared 2026-07-10 for management review. Plain-language summary; the
engineering record behind every number here is in `AUTOMATION_ROADMAP.md`,
`tested_hypothesis.md`, and the analysis logs. Alternatives considered but
not recommended are in Appendix A, deliberately outside the main plan.*

---

## 1. What the product is

We operate an automated buying-and-cataloging system for Penn State gameday
buttons. It watches eBay and Etsy daily, and for every listing photo it:

1. **Finds** each button in the photo,
2. **Identifies** it — slogan, year, sport — against our curated photo
   library,
3. **Values** the lot and flags listings containing buttons we need,
4. Routes decisions to a one-person review queue in Slack, where errors are
   corrected in one or two taps.

The system runs on pay-per-use cloud infrastructure that costs nothing while
idle. That budget discipline is a design constraint, not an accident: every
technical decision below was made under the rule that the expensive resources
are (a) the reviewer's time and (b) per-photo calls to external AI services —
and automation means steadily moving work off both.

## 2. The goal

A photo arrives and — with no human input on the happy path — the system
finds every button, names it, updates inventory and valuations, and asks a
human only about genuine novelty, purchase decisions, and a small routine
audit sample. Every correction the human does make permanently improves the
system. The reviewer's job shrinks to: buy things, photograph things, and
catch the rare mistake.

## 3. Where we stand — the numbers

| Capability | Status |
|---|---|
| **Naming buttons (slogan/year/sport)** | Auto-confirmed identifications were manually audited: **759 of 759 correct**. Identification is no longer the bottleneck. |
| **Counting/finding buttons with no human input** | The system now *knows when to trust itself*: on the ~1/3 of photos it certifies, it is **96% exactly right and 100% within one button** (measured across 329 photos). On the live feed since the last fix: **zero disagreements**. |
| **Reviewer workload** | Falling by design. One recent feature alone (offering the best cross-sport suggestion instead of forcing typed entry) eliminated 30 of 35 manual typings on its first real lot. |
| **Measurement** | Every decision the system makes writes a structured log row. Every claim in this document traces to those logs, not to impressions. |

## 4. What we tried that failed — and what each failure bought us

We believe this list is the strongest evidence we are on the right path,
because each failure was caught by our own measurement process, converted
into a rule, and never repeated.

- **We trusted confidence scores. They lied.** A match the system was 96.8%
  "confident" about was wrong (two visually near-identical buttons), and so
  were several 87%-confident ones. *Rule adopted:* a score alone never
  commits anything; two **independent** signals must agree. That rule is
  what produced the 759/759 audit result.
- **We used a quality-score threshold to grow our photo library. It kept the
  one wrong photo and blocked 3.5× more good ones.** Replaced with the same
  agreement principle. The library — 3–4 verified photos per button — is now
  our most durable asset; it compounds with every confirmed lot.
- **We hoped for a shortcut counting method. The data refuted it — twice.**
  A cheap geometric estimate we hoped could count buttons for free was off
  by 7–21 buttons on real photos. We killed it before it ever shipped, and
  documented the refutation so it doesn't get re-proposed.
- **We celebrated a 100% accuracy reading that was a statistical mirage.**
  A 20-photo sample read 100%; at 329 photos the same method read 68%. Our
  own process caught the error. *Rule adopted:* trust *how* an answer was
  produced, not how confident it looks — which led directly to the
  self-certification gate that now works.
- **One-size-fits-all photo processing kept breaking on new backgrounds.**
  Cream quilts, white batting, gray textured mats — each new background
  defeated detection in a new way. Each failure was reproduced on the actual
  photo, fixed, and locked into an automated regression test (nine real
  failed lots now guard every future change). Detection recovered from
  finding *1 of 35* buttons to *35 of 35* on the worst of them.
- **A loophole let stale results slip through the trust gate.** Found in a
  routine log audit, patched the same week; disagreement on trusted photos
  went from 2.6% to 0%.

## 5. What works — proven, not promised

- **The improvement engine itself.** Every gain above came from the same
  loop: log the decision → grade it against verified truth → make one change
  → re-measure → keep or revert. The system improves exactly as fast as it
  measures itself, and it now measures everything.
- **Agreement-based auto-confirmation** (two independent readers must agree
  before anything commits): 759/759 audited correct.
- **The self-certification gate**: the system reliably separates the photos
  it can handle alone from the ones it can't, instead of guessing everywhere.
- **The correction flywheel**: reviewer taps aren't just fixes — each one is
  recorded as labeled training data. We recently started harvesting these
  automatically; the training set for the next generation of detection now
  grows at zero marginal cost.
- **A new blind-spot detector, validated in its first days live**: we now
  measure when the system draws a circle around something that *isn't* a
  button (previously invisible to all counting metrics). Its first real
  catch was exactly right — a listing of ten round non-button objects.

## 6. The path forward — staged, gated, reversible

Each stage has a numeric entry condition and a rollback tripwire. We do not
skip steps, and no stage requires new spending — the evidence accumulates
passively from the daily feed.

**Stage B — trust the system's own count on photos it certifies (~1/3 of
volume).** Enter when its count agrees with the independent AI reading on
≥98% of certified photos at meaningful volume (currently 100% since the last
fix; volume accruing daily). Roll back automatically if disagreement exceeds
2% in any 50-photo window. Before flipping: finish instrumenting the one
error type counting can't see (right count, wrong object) — that
instrumentation shipped this week.

**Stage C — the external AI becomes an auditor, not a crutch.** Once Stage B
holds, the external AI service is consulted only on disagreement or
low-confidence cases. This directly cuts our largest variable workload. Our
data shows the external AI's *reading* of button text is excellent and worth
keeping longest; its *counting* role is what the system replaces first.

**Stage D — the human becomes an auditor.** Auto-confirmed results commit
directly; the reviewer sees novelty, purchase decisions, and a permanent
random audit sample (the drift alarm — never removed). Entry requires
measured precision ≥98% over 300+ confirmations via the correction log —
measured, not inferred.

**Beyond — a detector trained on our own data.** Hand-built methods reliably
cover about a third of photos; the data says the rest need a learned
detector. The training data for it is already accumulating automatically
(every processed lot, every reviewer correction, every confirmed non-button).
This is the long-term path to covering the remaining two-thirds, and it costs
nothing until the data justifies training.

## 7. What we need

- **No new budget.** The plan runs on the existing pay-per-use footprint.
- **Continued reviewer discipline**: using the correction flow (rather than
  silently fixing errors) is what produces the measured-precision numbers
  Stages C and D are gated on.
- **Patience measured in weeks, not quarters**: the entry gates fill from
  the daily feed on their own.

## 8. Honest risks

- **Volume, not accuracy, is the current constraint** on advancing Stage B —
  the trusted slice is performing, but we certify at volume, not on small
  samples (we were burned once; see §4).
- **The remaining two-thirds of photos** stay human/AI-assisted until the
  learned detector earns its way in. We are explicit that hand-built methods
  have hit their ceiling there.
- **Drift**: buttons, photo styles, and marketplaces change. The permanent
  audit sample and the always-on logging are the alarm system, and they are
  non-negotiable in every stage.

---
---

# Appendix A — alternatives considered (not in the recommended plan)

*Held in reserve for discussion. Neither is recommended now; both were
evaluated against data rather than dismissed.*

### A1. "Let a frontier AI model do everything" — replace our detection and matching with a large multimodal model on every photo

**The case for it:** modern multimodal AI reads our button text at 96.5%
per-button accuracy already; pointing a top-tier model at every photo would
likely lift raw accuracy immediately with little engineering.

**Why not now:** it converts our near-zero variable cost into a permanent
per-photo fee on every listing scanned, forever — and daily marketplace
scanning is high-volume and mostly negative results. It also builds no
compounding asset: our verified photo library and labeled corrections are
what make the system *ours* and progressively cheaper; renting all
intelligence per-photo makes the system a cost center that never learns.
**Reasonable middle path if asked:** we already use this pattern where it
pays — the external AI audits and disambiguates rather than doing everything
— and Stage C *reduces* that spend rather than growing it. A one-time
benchmark run of a frontier model on a fixed photo set would be a cheap way
to price this option precisely.

### A2. "Buy the training data" — pay external labelers to fast-track the learned detector

**The case for it:** the learned detector's bottleneck is labeled photos,
which currently accrue passively over months. Paid labeling could compress
that to weeks.

**Why not now:** cost aside, label *quality* is the product. Our reviewer's
labels come with ground truth external labelers can't match (they know the
actual buttons, the sports, the eras), and our harvesting pipeline attaches
each label to full photo context automatically. Bought labels would need a
verification pass by the same reviewer they're meant to relieve.
**Reasonable middle path if asked:** targeted, not bulk — if one photo
category (e.g., dense piles) stays data-starved after a quarter of passive
accrual, commission labels for that category alone, verified by spot-audit.
