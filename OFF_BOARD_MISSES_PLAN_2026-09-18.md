# Off-board misses: verification and plan (2026-09-18)

Answers `OFF_BOARD_MISSES_2026-09-18.md` (buttonmatcher branch
`claude/buttonmatcher-strategic-review-bev1z4`, commit b550325). Synced to both
repos because the mechanism lives in a shared file and the fix lands in both.

Read: `score_slogans` and the match-time board build in buttonmatcher
`main.py`; `build_leaderboard`, `trim_top`, `CONFIRM_HEADER` and the match_log
columns in `match_logging.py`; `_deep_candidates`, `_gemini_db_candidates` and
the pipeline agreement pool; `_augment_results_with_nonfootball` and
`_match_blocks`; ebayscout `clip_matcher._crop_leaderboard` and
`pipeline_classify.gemini_db_candidates`; fronts A6, A7, C1, C7, C8;
`tools/shadow_quorum.py`; the branch's `tools/eval_logic.py` and its tests.
Not run: anything against the confirm_log export, GCS, Sheets or Cloud Run.
Every number below that comes from the corpus is the document's, not
re-derived; every claim about mechanism is from the code on `main`.

## 1. Verdict

**The issue is real, and it is not new: it is fronts A7 and C7 measured on
outcomes for the first time.** The document's one open worry, that the 63% is
an artefact of the unrestricted shadow board, is settled by the code without
running the diff: the fold is the same function on both boards, so a
football-vs-football same-year collision is off the production board exactly
as it is off the shadow board.

What the number means in production is the part the document could not see,
and it is answerable from the export the document already has. §3 below is
that run. It decides which of two costs the 364 rows carry, and the plan in §4
is the same either way; only its priority order changes.

## 2. What is verified in the code

**2.1 The fold.** `score_slogans` iterates `year_scores` and keeps one
phrase per year, the text-similarity argmax inside that year
(`main.py` 1841–1926). `build_leaderboard.best_by_year` does the same
(`match_logging.py` 139–210). The image term is per YEAR (max over that
year's reference photos), the text term is per SLOGAN, and the argmax is on
text alone. So a slogan that is not its year's best text match has no row on
any board at any depth, with any image evidence. A7 stated this; the code
confirms it.

**2.2 Both boards fold identically inside a year.** `restricted_top` and
`shadow_top` are the same `build_leaderboard` call with different
`allowed_years` / `allowed_types` (`main.py` 2950–3035). Era and type filters
remove YEARS and TYPES; they never change which slogan wins inside a year that
survives. Two consequences:

- On the pipeline path (`allowed_types=None`, every `gemini_*` row) a truth
  that is "year taken" on `shadow_top` is "year taken" on `restricted_top`.
  Reading 1 in the document's §5 is wrong for this path, which is the bulk of
  the corpus.
- On the slash paths (Football-only) the boards can differ only when the
  usurper is a non-football slogan. That slice is small and is the only thing
  the restricted/unrestricted diff will move.

**2.3 The operator sees three rows, one per year.** The live candidate list is
`score_slogans(..., limit=10)` over at most 8 merged years, cut to
`results[:3]` before the card is built (`main.py` 2689, 2850–2860); the
winter-sports augment can add a fourth (`_augment_results_with_nonfootball`).
Every option is a different year. On a human-lane crop whose truth lost its
own year, no click resolves it: the routes are typed entry, Dussellbot
suggest, or the twin/confusable picker when the pair is curated. Reading 2 is
therefore correct for human-lane rows, and it is worse than the document
says, because the production board is 3 deep, not 10.

**2.4 The pipeline already has a rescue, and it is the one C7 is worried
about.** `_gemini_db_candidates` (`main.py` 2005–2070; ebayscout
`pipeline_classify.gemini_db_candidates`) exists for exactly this case: when
Gemini's read is a known DB slogan at confidence ≥ 0.85 on an anchored crop,
its DB rows are appended to the agreement pool with `overall=None`,
`db_direct=True`. The resolver can then auto-confirm it, and the row is logged
as `gemini_auto` with `det_db_direct=1` in match_log. So a large share of the
364 are probably NOT misses the operator ever saw; they are rescued crops.
Their cost is C7's: no CLIP corroboration, therefore excluded from
`reference/_staging`, therefore never acquiring the photos that would let CLIP
see them next time. C7 already measured the exposure: 92.9% of confirmed
buttons share a year with a lot sibling, 68.6% of lots are single-year.

**2.5 The rescue fails in three known ways**, and those are the rows that
become manual: Gemini confidence below 0.85, an unanchored association
(the 1979-front rule), or a flagged index. None of those is logged as a
distinct reason in confirm_log; `det_db_direct` is blank on them like on any
un-rescued crop.

**2.6 ebayscout shares the fold.** `clip_matcher._crop_leaderboard` calls
`match_logging.build_leaderboard` directly and `_score_slogans` folds the
same way; `pipeline_classify.gemini_db_candidates` names it as "the dominant
reason a big `/crawl` yields a handful of reference crops out of hundreds of
buttons". Any fix goes into the shared file and both live scorers.

**2.7 The instrument's known false positives.** `confirm_outcomes` keys on
(`_basic_norm(phrase)`, `_year4(year)`). An edition-twin confirmation carries
the edition's year while the board row carries the same phrase under whichever
year won the fold, so `source ∈ {edition_pick, edition_pick_unknown}` rows can
read as off-board when the phrase is on the board. They must be excluded or
matched on phrase alone before the 364 is quoted again.

## 3. The run that decides it (no code, one afternoon, the same export)

Three splits of the 364, all from columns that already exist. `source` is in
confirm_log; `det_db_direct` and `within_year_json` are in match_log and join
on (`job_id`, `crop_num`), the pairing `tools/shadow_quorum.py` already does.

1. **By `source`**, twins excluded first. Machine (`gemini_*`, `auto*`) vs
   human (`pick`, `suggest`, `dussellbot_invoke`, `user`, typed). The human
   count is the operator's actual typing cost today. The machine count is
   C7's rescue load.
2. **Machine rows by `det_db_direct`.** `1` = rescued through DB-direct, the
   C7 cost. Blank on a `gemini_*` row = resolved some other way (majority,
   printed-year rung); worth a look, small.
3. **`offboard_diagnosis` as written** (year taken vs year absent), then for
   the year-taken rows where the usurper sat at rank 1: is the truth in
   `within_year_json.top[5]`, and at what `runner_up_margin`? That is the
   **rescue rate of un-folding** on today's data, with no new logging. Rank-1
   usurpers should be most of the mass (the usurper inherits the year's image
   score, which is why it won); if they are, the whole shadow in §4.2 is
   already measurable.

Also run `--board restricted_top_json` once, for the record; §2.2 says it will
move only the non-football slice.

**Decision rule.** Whatever the split, the code change in §4 is the same. If
the human-lane count is material, its priority is above RS-05/RS-06 (it
removes typing on every such crop). If the rows are almost all
`det_db_direct=1`, its priority is C7's: it converts rescued crops into
CLIP-corroborated ones, which lets them stage, which is what breaks the
starvation loop C7 describes.

## 3a. Results: the confirm_log export, run 2026-09-18

The operator supplied the confirm_log CSV (4,669 rows, 2026-07-20 to
2026-09-17, both JSON boards intact on every row). Everything in §3 that
needs only confirm_log was run; the two splits that need match_log
(`det_db_direct`, `within_year` margins) are still pending that export.
The branch tool reproduces the document's numbers exactly (3,426 scored,
83.0% #1, 581 misses, 364 off-board). Corpus is effectively September:
3,340 of the 3,426 scored rows.

**Both boards agree, as §2.2 said.** Restricted: 359 off-board, 63% of 573
misses. Shadow: 364, 63% of 581. Where a year is on both boards it carries
the same phrase 32,882 times and a different one 528 times, and all 528 are
on the Football-only slash paths (`pick`, `auto_sort`, `auto_gap_only`). The
document's §5 reading 1 is closed: this is production.

**Twin false positives are negligible:** 5 rows (3 `gemini_printed_year`, 2
`edition_pick`) have the phrase on the board under another year. The clean
count is 359 on shadow.

**Who carries the 359:**

| source | off-board | scored rows with that source |
|---|---|---|
| `gemini_auto` | 322 | 2,550 (12.6%) |
| `typed_search` | 19 | 24 |
| `missed_button` | 8 | 14 |
| `pick` | 7 | 371 |
| `gemini_printed_year` | 2 | 90 |
| `confusable_pick` | 1 | 1 |

Human lane: 35 of 552 human confirmations (6.3%), 27 of them with a typed
slogan. Machine lane: 324, and 304 of the 322 `gemini_auto` rows share a
year with a sibling in their own lot. So the 364 is C7's number first and
the operator's typing cost second. The operator types for it about once in
sixteen confirmations; the pipeline leans on the DB-direct rescue for it
about once in eight.

**Year taken vs year absent splits evenly, and the two halves need different
repairs:**

| | rows | `gemini_auto` | human | the truth has references elsewhere |
|---|---|---|---|---|
| year TAKEN | 183 | 151 | 30 | 115 of 183 |
| year ABSENT | 176 | 171 | 5 | 166 of 176 |

Usurper depth on the taken rows: rank 1 on 119 (65%), rank 2 to 5 on 37,
rank 6 to 10 on 27. The rank-1 usurpers are what `within_year_json.top[5]`
can price today; the deeper ones need the row-key shadow in §4.1.

**The usurpers are few and generic.** 81 distinct slogans hold the 183
stolen slots; the top five hold 54. `Penn State and Proud of it` 1992 alone
holds 28, all at rank 1, which is C1's sticky attractor doing exactly what
C1 said. The usurper rows average text score 0.482 against 0.645 for
genuine #1 rows, on an image score of 0.841 the truth would have inherited.
68 of the 183 usurper phrases contain "Penn State", "Lions" or "Nittany":
generic wording with a hot text embedding wins the year's argmax and the
year's image score does the rest.

**The year-absent half is an image miss with the photos on the shelf.** 166
of the 176 truths have references (they appear on-board with a `ref_sim`
in other rows) and only 8 were never on any board. Their year sits deep on
image alone: `rank_image_only` 21 to 40 on 70 rows, past 40 on 48, 11 to 20
on 33, top-10 on 23. So the shelf exists and did not recognise the crop,
and the text side (a cold pun, A6 Mode 1) did not rescue it. That is the
one slice where reference variety, C8's question, can still matter. It is
about 30% of all misses and it is NOT reachable by the un-fold.

**What this changes in §4.** The un-fold's ceiling is the taken half: 183
rows, half the off-board set, a fifth of the misses. Its job is
visibility, not #1: the truth's row lands just under the usurper (same
image score, lower text), which is enough for the review card, the
agreement pool and the gap rule, and not enough to rank first on its own.
Two more levers now have numbers behind them:

- **C1, immediately:** retire or re-shoot the attractor shelves.
  `Penn State and Proud of it` 1992 is 28 of 183 on its own. This is the
  cheapest 15% of the taken half and needs no code.
- **Within-year argmax on centered text** (the `rank_centered` machinery,
  `build_centered_leaderboard`) as a second shadow: the usurpers win on
  embedding temperature, which centering removes by construction. It can
  be logged as a `text_centered` key on the same row shadow as 4.1.

**match_log joined (4,308 rows, 2026-09-04 to 09-18; 2,964 of the 2,989
September confirmations join on `job_id` + `crop_num`).**

*`det_db_direct` cannot answer its question yet: buttonmatcher never writes
it.* All 169 populated rows are ebayscout pipeline rows; the 2,098
buttonmatcher pipeline rows since 09-12 are blank on every one. ebayscout
`main.py` passes `db_direct=` into its match record (line 1258);
buttonmatcher `main.py` computes the candidates and never logs the flag. So
C7's only instrument is blind on the service that carries all 322 off-board
gemini autos. The inference that those rows resolved through DB-direct
rests on the mechanism in §2.4, not on a measurement. **One-line fix, do it
in the next buttonmatcher PR:** pass `db_direct` per crop into
`build_match_record` the way ebayscout does; the column already exists.

*The un-fold rescue rate, measured on the 96 rank-1-usurper rows since
09-04 (94 with a within-year read):*

| truth's position inside the usurped year | rows |
|---|---|
| runner-up (position 1) | 53 |
| positions 2 to 4 | 18 |
| deeper than the logged top 5 | 20 |
| it IS the year's text winner (non-football usurper on the all-sport board) | 3 |

Winner-minus-truth text margin on the 74 rows in the top 5: p10 0.001,
median 0.028, p90 0.098, max 0.181. Years involved hold 13 to 47 slogans
(median 16), which is why a K=2 cap saturates:

| M_UNFOLD | rescued with K=2 (runner-up only) | rescued at any position within M |
|---|---|---|
| 0.02 | 23 / 96 | 27 / 96 |
| 0.05 | 39 / 96 | 47 / 96 |
| 0.10 | 51 / 96 | 68 / 96 |
| 0.15 | 53 / 96 | 73 / 96 |
| 0.20 | 53 / 96 | 74 / 96 |

*The cost, on the only autos a same-year #2 can touch.* `gemini_auto`
resolves by slogan match and has no gap guard (`gemini_resolve.py` never
reads a gap), so it is unaffected. The gap rules drive `auto_sort`,
`auto_gap_only` and `auto_slogan_gap`: 187 correct rows with a read since
09-04, current #1-to-#2 gap p10 0.090 and median 0.163 against
`GAP_ONLY` 0.15 and `GREEN_GAP` 0.12. An un-folded same-year #2 sits at an
overall gap of about half the text margin, so every un-folded row fails
both gap rules and is withheld to the card:

| M_UNFOLD | correct gap-rule autos withheld |
|---|---|
| 0.05 | 1 / 187 |
| 0.10 | 4 / 187 (2.1%) |
| 0.15 | 13 / 187 (7.0%) |
| 0.20 | 35 / 187 (18.7%) |

For scale, across all 2,182 correct autos with a read, 12.3% have a
same-year sibling within 0.10; the un-fold puts that sibling on the card
and in the agreement pool on those rows and changes nothing else about
them.

**The gate, set from these numbers:** `M_UNFOLD = 0.10`, cap K = 4 rows per
year (winner plus up to three siblings within M). Expected on this corpus:
64 to 68 of the 96 rank-1-usurper rows become visible, at the price of 4 of
187 gap-rule autos going to the card instead. The 53 taken rows whose
usurper sat at rank 2 to 10 have no within-year read (the column covers #1's
year only), which is what the row-key shadow in §4.1 exists to measure; the
20 "deeper than top 5" truths sit in the largest years and are C1's and the
centered-text shadow's to recover, not the cap's.

## 4. The plan: un-fold the board, with a cap

The lever is not A7's withhold (the document's §6.4 is right that a withhold
relabels the 364, it does not reduce them) and not `within_year.top[5]`
becoming rows (that only covers #1's year). It is a bounded change to the
fold itself, in one shared function and the two live scorers:

> Inside each year, emit the text-argmax slogan AND its runner-up when the
> runner-up's normalized text score is within `M_UNFOLD` of the winner's.
> Both rows carry the year's image score and their own text score through
> the unchanged formula. K = 2 per year; the cap keeps the board shape.

Everything downstream then sees the sibling with no other change:

- **Review card:** the runner-up becomes a clickable option (the fourth-option
  precedent from the winter-sports augment already exists), so the human-lane
  rows stop needing typed entry.
- **Agreement pool** (`_deep_candidates`): Gemini's read matches a row that
  CLIP actually scored, so the crop resolves with CLIP corroboration instead
  of through `db_direct`, and the `_staging` exclusion no longer applies to
  it. This is the C7 fix.
- **Auto-confirm gap rules:** #2 can now be a same-year sibling with a tiny
  gap. That IS A7's withhold, arriving as a side effect rather than as a
  separate rule, and it is the one live-behaviour change that needs pricing.

### 4.1 Step 1: shadow it (no live change, no new column)

Stamp each `restricted_top` / `shadow_top` row with
`runner_up: {phrase, text_norm, margin}` at build time. It is a key on a row
of an existing JSON cell, the same mechanism `visual_shadow` and `ref_sim`
use, so it is not a new column under the plan §7 freeze, and it turns the
§3.3 measurement from "rank-1 usurpers only" into "every board row". Cost:
one dict per row, from sims already in hand.

### 4.2 Step 2: replay, three numbers, from the next export

- **Rescue:** share of off-board truths that become on-board (present as
  `runner_up` of their year's row) at each `M_UNFOLD` in
  {0.05, 0.10, 0.15, 0.20}. A7's own data brackets the range: the
  disagreement-median margin is 0.124, the agree median 0.243.
- **Cost:** correct autos that would be withheld because the un-folded #2
  now sits inside the gap rule's margin. A7 measured this at M=0.005 as 2 of
  856; the same sweep, on the same rows, prices the un-fold.
- **Card:** how often the runner-up would displace the third year on the
  card, and how often the confirmed answer was that third year (the
  operator's lost click).

Gate to ship: rescue on the human-lane and `det_db_direct=1` rows is a
majority at the chosen M, and correct autos lost is no worse than A7's
withhold at the same rows. No threshold moves; `AUTO_RESOLVE_THRESHOLD` and
the gap margins stay where they are.

### 4.3 Step 3: ship behind a kill switch, both repos, one PR

`BUTTONMATCHER_UNFOLD` / `EBAYSCOUT_UNFOLD`, default on once the gate is met,
`0` restores the fold. `build_leaderboard` is shared and syncs byte-identical;
`score_slogans` (buttonmatcher) and `_score_slogans` (ebayscout) change the
same way. `tests/test_buttonmatcher_parity.py` gets the un-fold case.
Confirm-log grading afterwards is the same `eval_reference_value.py` run: the
off-board share is the before/after number.

### 4.4 What this does to the fronts and the order

- **A7** keeps its quorum for the withhold. The un-fold is a second lever on
  the same front, not a new front, and it is not gated by the 250 human rows:
  it adds candidates, it withholds nothing on its own, and its cost is priced
  by the replay in 4.2. The quorum was not under-priced; it priced a
  different lever.
- **C7** is the front this actually serves. Its part (b), whether rescued
  slogans ever acquire a reference by another route, is answered by the
  un-fold making them corroborated in the first place.
- **C8** stays open for the redundancy question only; `sibling_cos` accrues
  for free. Do not re-run it until §3 shows a "year absent" slice large
  enough for reference variety to matter; on the "year taken" slice it cannot,
  by construction.
- **Order:** §3 run first (today). Then 4.1 in the next code PR. RS-06 and
  RS-05 continue in parallel; they are small and already in motion, and
  nothing here changes their acceptance. WS2 `correction` rows are untouched
  and still gate Stage D.

## 5. Answers to the document's §6, in its order

1. **Reading 2, on the code.** Both boards fold identically inside a year, so
   the production board has the same hole for same-type collisions. Run the
   diff for the record; it settles only the non-football slice.
2. **No.** A7's quorum prices a withhold on human clicks and stays. The
   un-fold is a different lever with a different cost and a replay-based
   gate.
3. **Partly.** The reference work is correctly scoped and was never going to
   move this number; nothing in it is demoted. The un-fold goes ahead of
   RS-05 in priority once §3 is run, and it is C7's fix as much as A7's.
4. **Correct: a withhold does not reduce the 364.** The second lever is the
   capped un-fold in §4, not `within_year.top[5]` promoted to rows.
5. **Keep accruing, do not re-run yet.** The corpus so far says the library
   is mostly silent, not misleading; the only slice where variety can help is
   "year absent", and §3 sizes it.
