# Strategic review — buttonmatcher + ebayscout (2026-09-14)

*Byte-identical in both repos, like the other shared strategy docs. This is a
review and a plan, not a change: **no code was modified.** Every item below is
written so a different person can pick it up cold: it names the file and
function, the shape of the fix, and what "done" looks like. Ticket IDs are
`SR-xx`; implementers should reference them in commit messages.*

*Scope of what was actually checked: both repos on `main` as of 2026-09-13,
both pure-Python test suites (buttonmatcher 1,032 passed; ebayscout 532
passed, 3 skipped), a `pyflakes` sweep of the main modules, shared-file
parity diffs, the GitHub PR state (re-queried: zero open PRs in either repo),
and the three Slack channels the services post to. The torch/cv2/GCP paths did
not run here and nothing in GCS or Cloud Run logs was read. See §9.*

---

## 1. The verdict, in five lines

1. **The services are healthy and running unattended.** The daily feed has
   fired every morning since at least 2026-08-20 (2–32 lots/day, ~6/day in
   September), deals post, the pipeline lots flow, and August had zero
   commits in either repo while everything kept working. That is the strongest
   evidence in the project.
2. **No critical bug was found.** The pure test suites are green, the shared
   files are byte-identical (the two deliberate exceptions are import style
   and an env-flag helper), and the four defects in §4 are all bounded. Two of
   them are data-loss edges on the audit trail; none loses a button count.
3. **The automation program is not moving toward its own goal.** Stage B
   (detection standing alone) is ~18 points short of its gate at real volume
   and the gap is accuracy, not volume; Stage D (the human as auditor) is
   gated on a number that **cannot be produced** because the correction flow
   the docs keep asking the operator to use was never built (§3.2). The
   roadmap's "volume is the constraint" line is from July and the September
   data contradicts it.
4. **The measurement apparatus has become the main cost.** 70 fronts, 91
   positional log columns, ~17,000 lines of overlapping strategy/registry
   docs, and a generated workbook whose formulas have been wrong at least 17
   times. Three "computed, printed, discarded" numbers were found in one
   session. September's engineering went almost entirely to bookkeeping the
   program rather than advancing it.
5. **The operator's time is going somewhere the program does not measure:**
   reference review threads of 370–416 replies, season sweeps run through
   `/scout` and `/sort` that never reach the inventory sheet (the operator's
   own 2026-09-13 audit: 100 buttons confirmed, 1 written), and hand
   corrections in the sheet. The next quarter should be aimed there.

**Recommendation in one sentence:** freeze the register, fix the four
defects, build the correction affordance that Stage D actually needs, and
redirect the automation effort from "drop Gemini" to "drop the human's
clicks" — measured by the two numbers that matter (clicks per lot, buttons
per operator-hour).

---

## 2. What is live today

**buttonmatcher** (`button-inventory`, Cloud Run us-east1, `main.py` 15.8k
lines, 126 Slack handlers). Slash commands `/inventory`, `/sort`, `/scout`,
`/inventory remove`, `/sort complete`, `/reference …`, `/slogan …`; the
Drive→Gem→GCS pipeline posting a mode chooser into `#inventory-bot`. Writes the
inventory sheet (keyed on SloganID since PR #156, live re-read before every
count write since #158, batched audit rows + 429 retry since #158's follow-ups
on `main`), the shared Logger workbook, the reference library, and per-lot
training-label sidecars.

**ebayscout** (`ebay-scout`, Cloud Run us-east1, `main.py` 2.7k lines). One
slash command, `/crawl <N>`. The 09:00 Cloud Scheduler `/run-scan` feeds the
day's unseen eBay lots (daily query set) into the same Gem pipeline;
`process_pipeline_lot` runs detection → Gemini reconcile → CLIP → resolve,
posts **deals only** to `#ebay-checker`, auto-stages two-signal crops into
`reference/_staging`, marks the lot seen, writes `scan_log.jsonl`, the Logger,
and (since 2026-09-12, for the first time) label sidecars. The legacy CLIP-only
scan (`_run_daily_scan`, ~650 lines with `_evaluate_listing`) survives only
behind `?year_crawl`/`?era_crawl`/`?hunt_ids` and `DAILY_PIPELINE_FEED=0`.

**Shared** (`detect.py`↔`detect_pipeline.py`, `match_logging.py`,
`detect_gate.py`, `gemini_*`, `edition_twins.py`, `rerank.py`, `sheet_retry.py`,
`confusable_slogans.py`, `label_harvest.py`, `pipeline_ingest.py`,
`detect_color/mask/scale.py`): all byte-identical today except the documented
import-style and env-helper hunks. `tests/test_buttonmatcher_parity.py` pins
the detector ASTs and the scoring constants. **Neither repo has CI**; the
23 MB fixture battery and every cv2/torch test run only when a person runs
them locally.

Hard constraints (scale-to-zero, no `--no-cpu-throttling`, no
`--min-instances`, `--max-instances=1`, no CLIP quantization) are honored in
`service.yaml`, `cloudbuild.yaml` and the code. `DEPLOY.md`/`HANDOFF.md`
still carry the open operator item that Cloud Scheduler's `attemptDeadline`
should be 1800 s; today's feeds are small enough that it has not bitten.

---

## 3. Direction check — is the code driving toward what we log for?

The stated end state (`AUTOMATION_VISION.md` §1): a photo arrives and, with
**no human input and no Gemini call on the happy path**, detection finds every
button, matching names it, confirmations commit on two-signal agreement, and
the human sees only novelty, purchases and an audit sample. The staircase is
A (everything logged) → B (detection stands alone on gated lots) → C (Gemini
becomes an auditor) → D (the human becomes an auditor).

### 3.1 Where each stage actually stands (from the register's own 2026-09-12 readings)

| Stage | Gate | Standing | Reachable with current approach? |
|---|---|---|---|
| A | fully instrumented | done | — |
| B | ≥98% gated-unguided count agreement with Gemini | **79.6% exact, 96.0% ±1** (n=201 scored of 215 gated images, per-image) | **No.** Even the ±1 relaxation the register names as "the cheapest next question" is already answered in the same table: 96.0%, short of 98. Unguided detection overall is 60.8% exact vs the user count. |
| C | B stable **and** `ref_sim` separates right from wrong at ≤2% miss | blocked on B; A19 says `ref_sim` works only as a *second* signal (0.002 margin solo) | Not on this path. |
| D | ≥98% measured auto precision over ≥300 confirmations via `correction` rows | **0 correction rows in three months** | **Not as specified — see 3.2.** |

Two more facts sharpen this:

- **The ruler for Stage B is unverified where 92% of the volume is.** The gate
  compares Hough to Gemini's count, justified by "the operator visually
  confirms every Gemini decision" — true for buttonmatcher's reviewed lots,
  false for ebayscout's pipeline, where nobody looks. Against human truth the
  gated stratum is 8/9 (n=9) and 90.7% exact at n=22 lots carrying a typed
  count. There is no human-truth detection set of meaningful size, though the
  label sidecars (523 from buttonmatcher, ebayscout's only since 09-12) and
  `/sort` typed counts are exactly the material for one.
- **The small-lot overcount (B4) is still 48% of small lots** after its fix,
  and the saturation residual (B2) still sends 6% of all lots to the grid.
  Those are the two live detection defects; both were re-instrumented on
  09-12 and need one feed cycle before they can be read.

### 3.2 The Stage-D gate is blocked on a feature that does not exist

`LOGGER_FRONTS.md` A10, `AUTOMATION_ROADMAP.md` 4c and `AUTOMATION_VISION.md`
§6.4 all say the same thing: auto-confirm precision "is inferred, not
measured — when an auto-confirm is wrong during normal use, **use the
correction flow**." `match_logging.py` line 670 and `LOGGING.md` document the
`correction` / `skip_correction` sources.

**Nothing in either `main.py` writes those sources.** A grep for `correction`
in buttonmatcher's `main.py` finds only comments, the `/buy` Dussellbot path
and a `slogan_correction` modal mode; no handler on an auto-confirmed line
produces a `correction` row, and the register itself notes elsewhere that
"`auto_overridden` has no UI affordance yet." The operator has been asked for
months to use a flow that was never built, and the project's "only unmeasured
load-bearing number" is unmeasured because of that, not because of discipline.
The 759/759 `gemini_auto` visual audit (C6) was done by hand for the same
reason and survives only in chat.

This is the single most important gap in the review: **the gate to the stage
that would actually reduce the operator's clicks is unreachable by design.**
Ticket SR-05 builds it.

### 3.3 Where the engineering effort went

Commit cadence: June 32, July 178, **August 0**, September 155 (both repos).
September's commits, by subject: inventory-sheet write correctness (SloganID
keying, live re-read, quota batching, idempotency claims, lost-crop register),
tracker-formula repairs (three passes, 23 cells), register reconciliation and
two long grading reviews, the bowl-year parity wiring, the label-harvester
fix, tiny-circle guard, and the two new modes (`/inventory remove`, `/sort
complete`). All of it was necessary — the write path really was losing
buttons — but **none of it moved a stage gate**, and roughly half of it was
maintaining the measurement apparatus rather than using it.

### 3.4 What the operator's time is actually spent on (from Slack, this week)

- **Reference review:** `#inventory-bot-debug` shows sessions queuing 54, 61,
  65, 73 and 74 slogans, with threads of 371 and 416 replies. The at-cap
  auto-replace rule (`_ref_auto_replace_pass`) and `REF_FLOOR = 4` auto-fill
  exist, yet the queue is dozens of slogans per session. 41 slogans have no
  reference photo at all (`/reference check`, 2026-09-12); 8 `_year_*`
  folders are still "migration territory."
- **Season sweeps through the wrong mode:** the operator's own audit posted
  2026-09-13 in `#inventory-bot` — 100 buttons in 2011–2016 confirmed across
  17 lots, **one** reached the sheet, because the sweep was run through
  `/scout` and `/sort`, which never write it. `/sort complete` (PR #171/#172,
  merged 09-13) addresses the season-checklist half; the "I photographed it,
  count it" half is still a separate `/inventory` pass.
- **Alert triage in `#ebay-checker`:** roughly one needed-button alert per
  day, most of them for lots where the needed value is a small fraction of
  the asking price (a $130 lot for one $0.50 button; a $16.89 lot for two
  $0.50 buttons), plus relists re-alerting under a new item id (the same
  kathaslip listing on 09-06 and 09-13).

None of these three has a front, a column or a gate. **They are the product.**

### 3.5 Conclusion on direction

The code is faithful to the *logging* program: every decision writes a row,
every lever has a kill switch, every fix ships to both repos. But the program's
own data now says the detection-first staircase (B → C) is not the path to
"fewer human clicks" in any reasonable horizon, while the human-side stage (D)
is blocked on an unbuilt affordance and the largest human costs are unmeasured.
The recommendation is to keep detection in Gemini-guided steady state, stop
adding measurement, and put the next quarter into the human-side levers with
two headline metrics: **operator taps per confirmed button** and
**buttons written to the sheet per operator-hour**, both derivable from
`confirm_log` + `Bot Writes` today.

---

## 4. Defects found

Ranked. "Sev" is impact on data or money, not effort. Line numbers are as of
`main` 2026-09-13 (buttonmatcher `89e78e0`, ebayscout `847e9ec`).

| ID | Sev | Repo | Where | What |
|---|---|---|---|---|
| SR-01 | Med-High | buttonmatcher | `main.py` `_audit_bot_writes` / `_audit_flusher_loop` (~4003–4090) | The Bot Writes tail is flushed by a daemon thread that sleeps 4 s; after the in-flight request ends Cloud Run throttles that thread, and at scale-to-zero the last <20 queued rows of a session are lost. The forced end-of-request render exists for the Slack summary (lines 7613, 8729) but not for the audit queue. |
| SR-02 | Med | ebayscout | `main.py` `_run_daily_scan` (2169, 2445, 2455) vs `_mark_item_seen_now` (1284) | The legacy CLIP path loads `seen` once and later does a wholesale `save_seen(seen)` outside `_seen_lock`; a pipeline confirmation that lands during a year/era/hunt crawl is erased from `seen_items.json` and the lot is re-fed and re-alerted. |
| SR-03 | Med | buttonmatcher | `main.py` `_kick_pipeline_mode` (timeout 890) + `_run_pipeline_mode_now` | The per-lot confirm loop is one HTTP request against a 900 s Cloud Run limit. Observed ~6.8 s/button (353 buttons in 40 min) plus 1/3/8 s 429 backoffs; an 80-button lot (they occur) is at the edge, and past it the loop continues CPU-throttled with the claim held — the 15.5 h cliff pattern from `DECISIONS.md` #23. |
| SR-04 | Med | ebayscout | `seen_items.append_scan_log` under `_scanlog_lock` | Every lot downloads and re-uploads the entire `scan_log.jsonl`. Unbounded growth, linear cost per lot, serialized across concurrent Gem results. |
| SR-05 | Med (program) | buttonmatcher | no producer for `source=correction` / `skip_correction` | See §3.2. The Stage-D gate's only instrument does not exist. |
| SR-06 | Low-Med | buttonmatcher | `_inventory_written`, `_load_pipeline_job` | The write ledger is in-memory; a lot resumed from its GCS snapshot after a restart has no ledger, so stale review cards for already-written crops become live double-count traps (documented on the 2026-09-08 lot). Bot Writes is durable and holds exactly the keys needed to reseed it. |
| SR-07 | Low | ebayscout | `config.ENABLE_UNDERVALUED_ALERTS` | Defined `False`, never read. `process_pipeline_lot` posts undervalued alerts whenever `lot_value > asking`. The flag and `DECISIONS.md`'s "deferred" note are wrong about live behavior. |
| SR-08 | Low | ebayscout | `main.py` 1997–2110, `_post_yellow_review`, `_run_daily_scan`/`_evaluate_listing` | Dead or near-dead code left from PR #17 (`/scout` removed): `scout_verify_*`/`scout_count_*` handlers with nothing posting their cards; ~650 lines of CLIP-only scan reachable only by flags. `CLAUDE.md` still says ebayscout "serves a manual `/scout` mode." |
| SR-09 | Low | ebayscout | `_check_needed_hit` / `send_needed_alert` | Alerts have no relevance score and no relist suppression (§3.4). Product noise, not a defect. |
| SR-10 | Low | buttonmatcher | `tools/ebayscout_patches/` | A cross-repo patch directory whose one patch is applied; sprawl. |

### Fix shapes and acceptance

**SR-01 — flush the audit queue inside the request.** Call
`_flush_audit_rows()` at the end of every in-flight request that can enqueue:
the end of `_run_pipeline_mode_now` (next to the forced summary render), the
end of `process_grid`'s confirm phase, and after each Slack action handler
that writes (confirm/pick/skip/removal). Simplest robust form: a Flask
`teardown_request` that flushes when the queue is non-empty, so no handler can
forget. Keep the timer thread as backstop. *Done when:* a 5-button `/inventory`
lot followed by no activity shows all 5 Bot Writes rows within seconds, and a
test asserts the teardown calls the flush.

**SR-02 — one seen-file writer.** Route the legacy path's marks through the
same locked load→add→save helper `_mark_item_seen_now` uses (or make its
checkpoint a reload-merge-save). *Done when:* a unit test with a fake store
shows a mark made between the scan's load and its save survives.

**SR-03 — chunk the confirm loop.** Have `/internal/pipelinemode` process at
most N crops per request (N≈25) and re-kick itself with a cursor, claim held
per chunk and released in `finally`; or, as the cheap interim, raise the
service `--timeout` to 1800 s as ebayscout already does and log loop duration
per lot. *Done when:* an 80-crop synthetic lot completes with every crop
carded or written and the claim released; loop duration is on the `>>>
PIPELINE` line.

**SR-04 — partition the scan log.** Write `scan_log/YYYY-MM.jsonl` (append by
read-modify-write of the current month only), and teach
`tools/merge_scan_log.py`, `build_master_dataset.py` and
`audit_reference_coverage.py --scan-log` to read the prefix. *Done when:* a
lot's write touches only the current month's blob; the tools produce the same
output on the concatenation as before. Operator: `gsutil du` the current blob
first so the decision is sized.

**SR-05 — build the correction affordance.** On every auto-confirmed summary
line (`🟢 GEMINI/AUTO — Button N`) add a one-tap "wrong" control that opens
the existing picker for that crop; on pick, write `confirm_log` with
`source=correction` (and `skip_correction` on skip), decrement/increment the
sheet through `_record_inventory_change` so the sheet stays right, and record
both on Bot Writes. Also add a 1-in-N audit sample: every Nth `gemini_auto`
lot posts its cards as if not auto (N≈10), so precision is *sampled*, not just
volunteered. *Done when:* the tap produces a `correction` row joined on
`(job_id, crop_num)`, the sheet delta is visible on Bot Writes, and a tracker
reads precision = 1 − corrections / sampled autos. This unblocks A10 and E4.

**SR-06 — reseed the ledger from Bot Writes on resume.** In
`_load_pipeline_job`, read the Bot Writes rows for that thread and add their
`(channel, thread, crop)` keys to `_inventory_written`; do the same lazily on
the first click into any thread the process has not seen. *Done when:* a
resumed lot refuses to write a crop the tab already holds and says so on the
card.

**SR-07 — honor or delete the flag.** Gate the pipeline's undervalued alert on
`config.ENABLE_UNDERVALUED_ALERTS` and correct `DECISIONS.md`, or delete the
constant. Decide with the operator whether undervalued alerts are wanted (they
have fired; the Slack history shows deals posting only via the needed path).

**SR-08 — remove the dead `/scout` handlers and decide the legacy scan.**
Delete the `scout_*` actions and `_post_yellow_review`; either port
`year_crawl`/`era_crawl`/`hunt_ids` onto `_run_crawl` (they are just
different query builders feeding the same pipeline) and delete
`_run_daily_scan`/`_evaluate_listing`, or mark them frozen in `DECISIONS.md`.
Fix `CLAUDE.md`. *Done when:* `test_main.py` still passes and the module loses
~700 lines.

**SR-09 — alert relevance (product).** Add a "worth it" line and sort key:
`needed_value = Σ min(amount_needed, count_in_lot) × max_price_single`,
`ratio = (needed_value + lot_value_rest) / asking`; label alerts 🟢/🟡/⚪ by
ratio and suppress ⚪ unless the needed button is rare (`max_price_single ≥
$2.50`, say). Suppress relists by fingerprint `(seller, normalized title)`
within 30 days, showing "seen 09-06 as item …" instead. *Done when:* the
09-06/09-13 kathaslip pair produces one alert and one "relist" note, and the
$130-for-$0.50 lot is ⚪.

**SR-10** — delete `tools/ebayscout_patches/`; the patch is on ebayscout `main`.

---

## 5. Records that are wrong today (fix in the same PR as the nearest ticket)

- **`buttonmatcher/HANDOFF.md`:** says `claude/sheet-write-quota` is "NOT
  merged" (its three commits `b66f3cd`, `1deb146`, `a445c93` are on `main`);
  says `tests/run_mode_parity_tests.py` fails on `main` (15/15 pass); the
  branch table lists branches that no longer exist (only `main` and this
  review's branch exist on origin); the 2026-06-20 `/reference` PR #84 entry
  is long superseded.
- **`ebayscout/ebayscout/HANDOFF.md`:** title still dated 2026-05-29; the
  Cloud Scheduler `attemptDeadline=180s` item is either done or still open —
  the operator should say which and the line should change.
- **`ebayscout/CLAUDE.md`:** "(2) serves a manual `/scout` mode" — removed in
  PR #17 (2026-06-03).
- **`LOGGING.md`:** `match_log` is 91 columns (it says 87); `confirm_log` 23.
- **`AUTOMATION_ROADMAP.md`:** "Phase 5 … volume, not accuracy, is the
  constraint" and "0% gated disagreement" are July readings superseded by E2's
  September numbers (79.6%/96.0%); the fixture battery is 26 lots, not 9.
- **`MANAGEMENT_BRIEF.md`** (dated 07-10) headlines "96% exactly right and
  100% within one button" and "zero disagreements on the live feed." The first
  was n=329 against Gemini before the per-image correction; the second was
  n=9. If this document is still shown to anyone, §3 needs the September
  figures and §6's Stage B paragraph needs to say the gate is missed.
- **`DECISIONS.md` remaining-issues table:** undervalued alerts are not
  actually disabled (SR-07).

---

## 6. The plan — next 90 days, in order

Four workstreams. Each ticket names the repo, a size (S ≤ 1 day, M ≤ 3 days,
L ≤ 1 week), and its gate. Do them in this order; nothing in WS2–WS4 should
start before WS1 is merged.

### WS1 — Hygiene and the four defects (week 1–2)

| Ticket | Repo | Size | Gate |
|---|---|---|---|
| SR-01 audit flush in-request | buttonmatcher | S | test + one live lot |
| SR-02 single seen-file writer | ebayscout | S | unit test |
| SR-03 chunked confirm loop (or timeout 1800 interim) | buttonmatcher | M | synthetic 80-crop lot |
| SR-04 partitioned scan log | ebayscout | M | tools read prefix |
| SR-06 ledger reseed from Bot Writes | buttonmatcher | S | resumed-lot test |
| SR-07, SR-08, SR-10 dead code + flags | both | S | suites green, docs fixed |
| SR-11 **CI** — a GitHub Actions workflow per repo running the pure suites plus `opencv-python-headless`+`numpy`+`Pillow` so the 26-lot fixture battery and the detector-parity AST test run on every PR (torch stays out) | both | M | the battery runs on a PR |
| SR-12 record fixes from §5 | both | S | — |

SR-11 matters more than its size: the review found "a test nobody runs" is the
norm, and the AST parity guard is the only thing standing between the two
detectors and silent drift.

### WS2 — Measure the human, then reduce the human (weeks 2–8)

| Ticket | Repo | Size | What / gate |
|---|---|---|---|
| SR-05 correction affordance + 1-in-N audit sample | buttonmatcher | L | `correction` rows flow; precision readable |
| SR-13 **two headline metrics** in the tracker: taps per confirmed button (from `confirm_log.source` — human sources vs machine) and buttons written per operator-hour (from Bot Writes timestamps); one tab, replacing nothing | ebayscout (tools) | S | tab reads from raw tabs, guarded by `test_goal_tracker_repair` |
| SR-14 **inventory from a sort** — after a `/sort` or `/sort complete` lot settles, offer one tap "count these into inventory" that runs the identified list through `_record_inventory_change` (no re-photograph, no re-match) | buttonmatcher | M | the 2011–2016 case: 100 confirmed → 100 written with one tap per lot |
| SR-15 **reference review load** — measure first: slogans queued per session, taps per slogan, share auto-resolved by the at-cap rule; then raise the auto-resolve share (e.g. auto-replace when the staged crop beats the weakest on resolution-normalized sharpness — C2's open question, decided by data rather than by asking again) | buttonmatcher | M | queue per session down by half at equal library quality (spot-audit 20) |
| SR-16 **fill the 41 empty shelves** — a one-shot list posted to Slack with photo asks; plus the 8 `_year_*` migration folders resolved | operator + buttonmatcher | S | `/reference check` shows 0 NO PHOTOS outside placeholders |

### WS3 — Detection: hold steady, decide with human truth (weeks 4–10)

| Ticket | Repo | Size | What / gate |
|---|---|---|---|
| SR-17 **human-truth detection set** — export tool that joins label sidecars (`pipeline/labels/*.json`, both services) to `confirm_log` `not_a_button`/`missed_button`/typed counts and to `/sort` typed counts, producing ≥200 lots with a human-verified count and circle set | ebayscout (tools) | M | the set exists as one file with provenance |
| SR-18 **grade Gemini's count on that set** — before it is used as a ruler for 92% of volume | tools | S | a number with n |
| SR-19 read B4 and B2's new columns after one feed cycle; ship the one dedup/mask change the data supports, shadow-first, nothing else | both | M | per the fronts' own gates |
| SR-20 **Stage B decision** — with SR-17/18 in hand, either (a) rewrite E2's gate against human truth and set a realistic target, or (b) formally park B/C and record it in `tested_hypothesis.md`. Recommendation: (b) unless SR-18 shows Gemini itself under 95% | docs | S | one paragraph, both repos |

No learned-detector training in this quarter. Labels have flowed from both
services for two days; SR-17 is the groundwork, and the data volume that
would justify training (thousands of human-touched lots) is quarters away.

### WS4 — ebayscout as a product (weeks 6–12)

| Ticket | Repo | Size | What / gate |
|---|---|---|---|
| SR-09 alert relevance + relist suppression | ebayscout | M | see §4 |
| SR-21 **weekly digest** — one Monday post: lots fed, buttons identified, needed hits, staged crops, sidecars written, top-5 needed slogans seen with links; read from `scan_log` + Logger, no new columns | ebayscout | S | the post appears |
| SR-22 port `year_crawl`/`era_crawl` onto `_run_crawl` (if SR-08 chose "port") with the cost warning `CRAWL_TRIGGER.md` already carries | ebayscout | M | one feed path |

---

## 7. Stop doing (for the quarter)

- **No new fronts, columns or shadow instruments** unless one is retired in
  the same PR. `LOGGER_FRONTS.md` is frozen at 70; the Progress Trackers
  workbook is not rebuilt.
- **No threshold moves** (already the standing directive) — and no more
  quorum tracking sessions on A2/A7/A25 until their counters move on their
  own; the quorums are months away at current accrual and re-reading them
  changes nothing.
- **No more grading reviews of the register for their own sake.** The two
  September reviews were valuable because each found real bugs (the label
  harvester, the bowl map); the next one should be triggered by a question,
  not a calendar.
- **No crawls for data** (`/crawl`, `?year_crawl`, `?ignore_seen`) — unchanged.
- **No docs longer than the code they describe.** New writing goes into
  `HANDOFF.md` (state) or `tested_hypothesis.md` (verdicts); the strategy docs
  get a dated one-paragraph status, not a new section.

---

## 8. Operator asks (each is one action)

1. Say whether Cloud Scheduler `ebay-scout-daily` `attemptDeadline` is 1800 s
   now; update the HANDOFF line either way.
2. `gsutil du gs://…/ebay_scout/scan_log.jsonl` — sizes SR-04.
3. `gsutil ls gs://…/pipeline/labels/ | wc -l` once a week for a month — the
   C5 accrual number, so SR-17 can be scheduled.
4. Confirm the 759/759 `gemini_auto` audit in one line in `HANDOFF.md` (C6).
5. Decide SR-07: are undervalued alerts wanted at all?
6. Decide SR-08: port the year/era crawls to the pipeline, or delete them.
7. Photograph the 41 shelf-less slogans (SR-16) — the list is in
   `#inventory-bot-debug`, 2026-09-12 13:58.

---

## 9. What this review did and did not verify

**Ran:** `pytest` over `buttonmatcher/tests` (1,032 passed; 4 modules
uncollectable without numpy/cv2/PIL) and `ebayscout/ebayscout/tests` (532
passed, 3 skipped; 6 modules uncollectable without numpy/cv2/openpyxl/
slack_sdk/google-cloud/gspread); `pyflakes` on both `main.py` files and the
ebayscout core modules (nothing beyond unused locals); `diff` of every shared
file; `git log`/`git branch -r` in both clones; the GitHub API for open and
recently closed PRs; the last ~40 messages of `#ebay-checker`,
`#inventory-bot` and `#inventory-bot-debug`.

**Did not run:** anything needing torch, cv2, GCS or Sheets — so the fixture
battery, the detector parity AST test's cv2-dependent siblings, the Logger
writers and the sheet write path were read, not executed. No Cloud Run logs
and no GCS objects were read. Every number in §3 is quoted from the
register's 2026-09-12 readings, not recomputed here.

**Defects were found by reading**, not by reproduction. SR-01 through SR-04
are confident from the code's own structure and the repo's documented
Cloud Run behavior; SR-05 is a grep result; SR-06 is the repo's own hazard,
restated with a fix.
