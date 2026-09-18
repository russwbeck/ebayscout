# Implementation check — WS1 and RS-01/RS-02 (2026-09-15)

*For the implementers. Byte-identical in both repos. Checked against
`origin/main` (buttonmatcher `3050930`, ebayscout `3a476fc`) and the open
branch `claude/buttonmatcher-strategic-review-bev1z4` (`66a9786`). Tickets
here are `IC-xx`; each names the file, the fix shape and what "done" means.
The companion fixture for RS-05 is `tests/fixtures/reference_decisions_2026-09.json`
(buttonmatcher). This file supersedes §10 of `STRATEGIC_REVIEW_2026-09.md`
and §9 of `REFERENCE_SCORING_REVIEW.md`, which carry the same findings in
shorter form.*

## 1. Verdict

The work is good and most of it is done. WS1 is merged on both mains, the
new GitHub Actions workflow is green on every run so far (8 buttonmatcher,
4 ebayscout), both pure suites pass locally on `main` (buttonmatcher 1,075;
ebayscout 560) and on the RS branch (1,132), no PR is open, and every shared
file is still byte-identical. Commit messages say what did not run against
Cloud Run, GCS or Sheets, which is right. The AST-level wiring tests are the
correct tool for a module that cannot be imported outside the deployed image.

| Ticket | Where | State |
|---|---|---|
| SR-01 audit tail flushed in-request | main `2ebce12` | **Partly** — IC-01 |
| SR-02 one seen-file writer | main `83aa1c4` | Closed |
| SR-03 chunked confirm loop | main `d89e125` | Closed, residual — IC-02 |
| SR-04 partitioned scan log | main `faffab2` | Closed; operator sizes the legacy blob (ask #2) |
| SR-06 ledger reseed from Bot Writes | main `dc8eebc` | Closed, notes — IC-05 |
| SR-07 undervalued flag | main `4ce0e91` | Closed as a switch; default now `True` (matches live). Ask #5 is now a real decision |
| SR-08 dead `/scout` handlers | main `4ce0e91` | Closed; legacy scan frozen (DECISIONS #33). Ask #6 open |
| SR-10 patch directory | main `102f74d` | Closed |
| SR-11 CI | main `ab898cb` / `14119a6` | **Runs, but skips 305 tests** — IC-03 |
| SR-12 stale records | main `ab898cb` / `14119a6` | Closed, one leftover — IC-06 |
| SR-05 correction affordance | not started | Correctly deferred to WS2 |
| RS-01 decision log + retired prefix | branch `66a9786` | Closed, follow-ups — IC-04, IC-07 |
| RS-02 auto-decide dormant | branch `66a9786` | Closed; diagnosis better than the ticket's — §3 |
| RS-05 margin re-fit | blocked on data | **Unblocked by the fixture** — §4 |

## 2. Findings, ranked

### IC-01 (Medium) — the audit flush hook does not reach Slack click handlers
**Where:** buttonmatcher `main.py`, `App(...)` at ~750, `_handle_confirm` at
~13966, `_flush_audit_rows_after_request`.
**What:** `after_request` fires when the HTTP response is produced. Bolt is
constructed without `process_before_response=True`, so a listener returns the
response at `ack()` and runs the rest of its body, including the sheet write
and the audit enqueue, in Bolt's listener thread after the hook has already
run. A click's rows are flushed by the *next* request's hook, or by the
throttled timer, which is the original defect. The pipeline loop and
`/internal/match` are covered, and those are where the bursts are; the last
click of a session is not.
**Fix:** call `_flush_audit_tail("listener")` at the end of every listener
that can write: `_handle_confirm` (bound to `confirm_match_1..4`), the skip,
edition-pick, twin-pick and removal handlers, or wrap them in one small
decorator. A per-click append is human-paced and inside the 60/min quota.
**Done when:** `test_audit_flush_wiring.py` asserts each writing listener
calls it, and a session that ends on a single click shows that click's Bot
Writes row within seconds.

### IC-02 (Medium) — the chunk cursor lives in memory
**Where:** buttonmatcher `main.py`, `_pipeline_mode_cursor`, `_run_pipeline_mode`,
`_load_pipeline_job`, `_kick_pipeline_mode`.
**What:** a container restart between chunks loses the cursor. The next
hand-off arrives as `start=25` against an expected `0`, is dropped with only
a stdout line, and the lot pauses at a chunk boundary until someone clicks
the mode again. That second click re-posts review cards for the first chunk's
non-auto crops (no double count, thanks to SR-06, but duplicate cards).
**Fix:** write the cursor into `pipeline_jobs/<job_id>.json` at each hand-off
and read it back in `_load_pipeline_job` so a resumed lot continues from the
cursor; post one line to the lot thread when a chunk is refused for
`start > 0` or a kick fails: "paused at crop N — click the mode again to
resume".
**Done when:** `test_pipeline_chunk_wiring.py` asserts the snapshot carries
the cursor and the refusal posts to Slack.

### IC-03 (Medium) — CI only runs files that have a `run_*_tests.py`
**Where:** `.github/workflows/tests.yml` in both repos.
**What:** the loop is `for f in tests/run_*.py`. Test modules without a
runner never run. Counted by `def test_` (class-based included):

| repo | modules skipped | tests skipped |
|---|---|---|
| buttonmatcher | `test_buy_flow` 22, `test_deficit_fill_gate` 15, `test_label_harvest` 8, `test_reading_order` 6, `test_hole_invert` 5 | **56** |
| ebayscout | `test_main` 82, `test_seen_items` 25, `test_etsy_client` 19, `test_ebay_client` 18, `test_clip_matcher` 18, `test_sheets_client` 14, `test_scoring` 13, `test_notifier` 12, `test_label_harvest` 8, `test_market` 8, `test_master` 8, `test_audit` 7, `test_crawl500` 7, `test_import_stdout` 6, `test_merge_scan_log` 4 | **249** |

Some need slack_sdk, gspread or torch and would fail to import, which is
exactly the "suite that quietly stops running" the workflow says it exists to
catch.
**Fix:** `pip install pytest` and run `pytest tests` (ebayscout:
`pytest ebayscout/tests`) with an explicit `--ignore` for the modules that
genuinely need the deployed stack, so the skipped set is named in the
workflow rather than implied by a missing file. Keep the runner loop if you
like; it is not "every suite".
**Done when:** the workflow's job summary lists the collected count and the
ignored modules by name.

### IC-04 (Low) — `_retired/` leaks into the no-cache hydration listing
**Where:** buttonmatcher `main.py` ~1651 (`hydrate_data`, the loose-object
listing) and the `ref_folders` filter below it. On the RS branch.
**What:** the listing is `prefix="reference/"` excluding only `_staging`. A
cold start with no `vectors.pt` downloads every retired blob and then logs
`!!! HYDRATION: reference folder '_retired' is neither a year nor a text_db
entry id — skipped`. Nothing is mis-encoded (the folder holds sub-folders,
not images), but it is wasted download and a false alarm on the recovery path.
**Fix:** exclude `reference/_retired` alongside `_staging` in both places.
**Done when:** a test in `test_reference_flow_wiring.py` asserts the exclusion.

### IC-05 (Low) — two notes on the ledger reseed
**Where:** buttonmatcher `main.py`, `_reseed_write_ledger`.
**What:** (a) the lot is marked reseeded before the tab read completes, so a
second concurrent first-write into the same resumed lot proceeds unreseeded
(narrow); (b) it reads the whole Bot Writes tab per new lot, which is fine
today and will not be at tens of thousands of rows.
**Fix:** (a) hold a per-lot event until the read completes, or accept the
window and say so in the docstring; (b) bound the read (`findall(thread_ts)`
or a tail window) when the tab passes ~10k rows. No urgency.

### IC-06 (Low) — one number still disagrees
`LOGGING.md` says `match_log` is 92 columns (correct: `len(MATCH_HEADER) == 92`).
`LOGGER_FRONTS.md` (shared) and `STRATEGIC_REVIEW_2026-09.md` §5 still say 91.
Fix both in the next shared-doc commit.

### IC-07 (Operator) — lifecycle rule on `reference/_retired/`
RS-01 retires references instead of deleting them; the review asked for a
30-day lifecycle rule on that prefix and the commit does not add or mention
one. Without it the prefix grows one image per swap forever. One `gcloud
storage buckets update --lifecycle-file` on the bucket.

### IC-08 (Note) — flag default flipped
`ENABLE_UNDERVALUED_ALERTS` was dead and read `False`; it is now read and
defaults to `True`, matching live behaviour. Whether undervalued alerts are
wanted at all (review ask #5) is now a decision that changes something.

## 3. RS-02: what the implementer found beyond the ticket

The review named the 50,000-pixel area floor. The commit replaces it with a
160 px short-side floor and replays the review's nine worked shelves as 7 of
9 auto-swaps (the other two are +7/+8, inside the 10-point margin and
therefore RS-05's). The second finding is the more important one: the auto
pass scored incumbents against a mean they were **part of** while candidates
were scored against a mean they were not, worth up to 18–27 composite points
on a varied shelf, so the pass defended the status quo hardest on exactly
the shelves the library wants. The Slack table was always leave-one-out; the
pass was not. One definition now. Refusal reasons are counted per session
and printed in the header, and both tables show each crop's size.

**Watch the first `/reference` after deploy:** the header's `left for you:`
line names the cause. "Too small to swap in" means the floor was it and is
fixed. "Within the margin" means RS-05 is next.

## 4. The fixture, and how RS-05 uses it

`tests/fixtures/reference_decisions_2026-09.json` (buttonmatcher) holds the 57
decisions the review tabulated by hand: the two sessions' thread ids, each
slogan's entry id, every reference and candidate with `q`, `sharp`, `expo`,
`on_slogan` exactly as the bot printed them, the flagged weakest, the raw
reply, and the reply parsed with `parse_review_command`'s grammar into
0-based swaps plus `stop`/`next`. Pixels and short side were not printed at
the time, so the size floor cannot be reconstructed from it; RS-02's tables
now print them, and `reference_log` will carry them from the next session.

Read back from the file, the review's numbers reproduce:

| | |
|---|---|
| decisions | 57 |
| swapped the flagged weakest | 26 |
| swapped a reference that was not the weakest (only) | 25 |
| no change (`next` / `stop` alone) | 6 |
| ended with `stop` | 26 |

Best candidate minus the flagged weakest, by what the operator did:

| delta | swapped weakest | swapped other | no change |
|---|---|---|---|
| ≥ +3 | 20 | 12 | 0 |
| +2 | 5 | 6 | 1 |
| +1 | 1 | 4 | 1 |
| ≤ 0 | 0 | 3 | 4 |

**RS-05 acceptance, restated on the fixture:** a `plan_at_cap_decisions`
with swap margin +3 and discard margin 0, given each decision's refs and
candidates, must propose a swap of the flagged weakest on all 20 "≥ +3,
swapped weakest" rows and a discard on the 4 "≤ 0, no change" rows, and must
not auto-swap on any "≤ 0" row. The 25 "swapped other" rows are **RS-06's**
target set, not RS-05's: no margin on the weakest can reproduce them, which
is the point of §3.6 and §3.7 of the reference review. A unit test that loads
the fixture and asserts those counts is the whole gate; it costs nothing to
run and needs no GCS.

## 5. What was and was not verified

Read: every WS1 commit's diff, the RS branch diff, the CI run list and PR
state from the GitHub API, the Bolt construction in `main.py`, the hydration
listing. Ran: both pure suites on `main` and the RS branch; pyflakes on the
touched modules; the fixture validation above. Did not run: anything
against Cloud Run, GCS, Sheets or Slack, so IC-01 and IC-02 are code
readings of documented Cloud Run behaviour, not reproductions.

---

# 2026-09-18 alignment check: `main` and the active branches

Same method as above: every commit read from its diff, PR and CI state
re-queried from the GitHub API on 2026-09-18, both pure suites run on `main`
(buttonmatcher 1,159 passed; ebayscout 563 passed; heavy modules still not
importable here). Items are numbered on from IC-08.

## 6. `main` since the 09-15 check

Both mains are aligned with the plan. Nothing on either `main` moves a
threshold, adds an infra flag, or touches the frozen legacy scan.

| commit | repo | plan item | verdict |
|---|---|---|---|
| 8047960 (#175) | buttonmatcher | IC-05 / RS-02 incumbents-vs-mean, IC-06 size floor, RS-01 retire, RS-03 log | in plan |
| 1937e64 (#177) | buttonmatcher | RS-03 log completeness: AUTO_REVIEW, per-crop `reason`, `sibling_cos` on auto rows | in plan; this is the evidence RS-05/RS-06 need |
| 8348c3c (#176) / fd001a0 (#86) | both | none: per-axis Gemini coordinate scale in `pipeline_ingest` | out of plan, justified. A real bug (mixed percent/permille axes), fixed once, synced byte-identical, tested |
| 3a89c84, 9746cb6 (#85) | ebayscout | IC-03 CI runner loop, SR-07 seen-marks single writer, SR-08 partitioned scan log, `ENABLE_UNDERVALUED_ALERTS` read | in plan |
| 407670f (#87) | ebayscout | none: `gsutil` -> `gcloud storage` in docs | out of plan, harmless; see IC-11 |

**IC-09 (record, not code).** The 2026-09-17 `/reference` session's result,
185 candidates inside the margin and 10 swaps, exists only in the message of
1937e64. `HANDOFF.md` on `main` does not carry it. Add the one line; it is
the first post-fix measurement of the auto pass and the next session's
baseline.

## 7. The active branches

### 7.1 `claude/buttonmatcher-strategic-review-bev1z4` (buttonmatcher, 552b9f8 + 38978ae, unmerged, CI green)

Adds `tools/eval_reference_value.py`, `tools/eval_logic.py` helpers, 12
tests, and front **C8** in `HYPOTHESES_IN_PROGRESS.md`.

**Aligned.** The headline finding is the right one and matches the direction
of the reviews: 83.0% of confirmed slogans already rank #1, and 63% of the
581 misses never reach the stored top-10 board at all. That is a retrieval
and year-folded-board problem (C7, §3.7 of the reference review), not a
crop-quality problem, and it is the correct reason to stop spending
curation effort on composite tuning. The tool is stdlib-only, read-only and
runs off a CSV export, so it costs nothing to keep.

**IC-10: C8 drops RS-05 on a misread of the fixture.** The commit says the
composite delta "does not separate a crop the operator took from one they
left: agreement sits at the 57% base rate for every margin from 2 to 10".
That metric asks whether the margin predicts *which* reference the operator
replaced. RS-05 never claimed that; §4 above already assigns the "swapped
other" rows to RS-06. Replayed on the fixture, the margin predicts *whether*
a swap happens, which is the only thing an auto-swap gate has to get right:

| best candidate minus flagged weakest | swapped weakest | swapped other | no change |
|---|---|---|---|
| ≥ +3 | 20 | 12 | 0 |
| +1 / +2 | 6 | 10 | 2 |
| ≤ 0 | 0 | 3 | 4 |

At ≥ +3 the operator swapped something on 32 of 32 shelves; at ≤ 0 they
swapped the weakest on 0 of 7. An auto pass at +3/0 makes the 20 weakest
swaps and the 4 discards with no error against the operator, and leaves the
12 + 3 "swapped other" rows to RS-06's novelty term. So: **RS-05 stays**, with
the acceptance in §4 unchanged; C8's evidence is the reason RS-06 comes
first, not the reason RS-05 goes. Do not carry the "dropped" wording into
`REFERENCE_SCORING_REVIEW.md` or `HANDOFF.md`.

**IC-11: the front freeze was bypassed twice.** Plan §7: no new fronts
unless one is retired in the same PR. `LOGGER_FRONTS.md` on `main` is at 71
and nothing has been retired, and this branch opens C8 while the docs
branch below opens B14a. Both fronts are worth having. The fix is the rule
as written: each PR that opens one names the front it retires (the C-track
has at least one candidate whose exit condition C8 has now answered), and
the count stays flat.

### 7.2 `claude/sheet-write-source-a5xnst` (both repos, 09447d3 / 385c290, unmerged, pushed after #176 / #86 merged)

Docs only: the 2026-09-18 Gem prompt with its two unapplied corrections
(flagged index must reuse the detected index; the schema example breaks its
own counting rules), `tested_hypothesis.md` §4.5a, the Phase 1 correction
in `GEMINI_PIPELINE.md` (recovery does drop crops and does fire at deficit
0), B13 amended, new B14a, PROGRESS test counts. The content is correct and
worth merging; the two shared docs are identical across the two branches.

**IC-12: commits on a merged PR's branch.** #176 and #86 merged at 01:38 and
01:45 UTC; these commits were pushed at 11:07 and 11:08 UTC onto the same
branch. CLAUDE.md names this exact mistake. They need a fresh branch off
`main` and their own PR; nothing on `main` references them yet, so nothing
is lost by moving them.

**IC-13: the ebayscout copy is red, and B14a is the cause.** Run
35338118051, 3 failed / 614 passed:

```
SystemExit: malformed front heading: '### B14a — The anchoring gate cannot reject a phantom it created'
ebayscout/tools/build_goal_trackers.py:569
```

`parse_register` matches `### ([A-E]\d+) —` and so does the Apps Script the
tracker repair emits (`^([A-E]\d+) `), so an `a` suffix is not a front id
anywhere the register is read by a machine. The buttonmatcher copy passes
only because buttonmatcher has no register parser. Renumber it **B20** (B19
is the last B on `main`) before the branch is re-cut, in both repos, and the
three `test_goal_tracker_repair` tests go green with no code change.

### 7.3 `claude/gsutil-deprecation-migration-j3hkdf` (buttonmatcher, d61ad85, unmerged)

The buttonmatcher half of ebayscout's #87. Simulated merge onto `main` is
clean. Until it lands, `STRATEGIC_REVIEW_2026-09.md`,
`REFERENCE_SCORING_REVIEW.md`, `SHIPPED_WATCH_REVIEW.md` and this file
differ between the repos by the `gsutil` lines alone (this file: one
sentence in IC-06). Merging it restores byte parity on all four. That is
the whole action; no other PR should touch those files until it is in.

## 8. Order of work from here

1. Merge the gsutil branch (7.3) to restore doc parity.
2. Re-cut 7.2 off `main` in both repos with B14a renumbered to B20; open the
   two PRs; ebayscout CI must be green before merge.
3. On 7.1: change the C8 text from "RS-05 dropped" to "RS-05 gated behind
   RS-06", name the front C8 retires, then merge.
4. RS-06 (novelty term, fed by the `sibling_cos` now on auto rows), with the
   fixture's 25 "swapped other" rows as its acceptance set; RS-05 follows on
   the same fixture at +3/0.
5. WS2 `correction` rows (SR-05) are still not started and still gate
   Stage D. Nothing on either branch moves them.

Verified as in §5: diffs, API state, CI logs, both pure suites, the fixture
replay above. Not run: anything against Cloud Run, GCS, Sheets or Slack.
