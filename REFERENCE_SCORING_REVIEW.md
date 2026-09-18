# Reference library scoring — review and plan (2026-09-15)

*Byte-identical in buttonmatcher and ebayscout: the review flow lives in
buttonmatcher, but ebayscout is the larger source of staged crops. No code was
changed. Tickets are `RS-xx`; each names the file and function, the fix shape,
and what "done" looks like.*

**Evidence used.** The curation code (`reference_review.py`,
`reference_quality.py`, `reference_dedup.py`, `main.py` `_stage_confirmed_crop`
→ `_reference_begin` → `_ref_auto_replace_pass` → the typed review;
ebayscout `pipeline_classify.staging_candidates` and
`seen_items.promote_crops_to_reference_staging`), `REFERENCE_CURATION.md`, the
`/reference check` and `/reference dedup` posts in `#inventory-bot-debug`, and
— the part that matters — **57 review decisions read back from two sessions**
(`#inventory-bot-debug` 2026-09-06 23:04, slogans 26–54 of 54, and 2026-09-12
10:43, slogans 1–29 of 73), each with the quality table the bot showed and the
reply you typed. No GCS object was read; no image was seen. Everything below
about *why* you chose what you chose is inferred from the numbers and is
labelled as such.

---

## 1. The verdict

1. **The composite quality score is not measuring what you optimize.** In
   the 2006–2010 session you replaced the reference the score ranked
   *best or second-best* in **20 of 29** slogans — almost always reference
   #2, typically Q 94–95 with sharpness 95–99 — while the flagged "weakest"
   stayed. In the 1978–1998 session the same happened 5 of 28 times. The
   score has no notion of *what a shelf needs* (variety of appearance, a
   real-world photo rather than a clean one, one crop per physical button); it
   only rates each image on its own. Section 3 says what I think you are
   doing; Section 7 asks you to confirm, because the fix depends on it.
2. **Where the score and you agree, you are far more decisive than the
   automation is allowed to be.** Every new crop that beat the weakest by
   **3 or more points** was swapped in (19 of 19). Every crop at or below the
   weakest was dropped (4 of 4). The auto-decide rule needs **10** points to
   swap and **5** to discard, so the whole band you resolve by hand — which
   is nearly all of it — is routed to you. Recalibrating those two margins
   from your own decisions removes most of the typing.
3. **The auto-replace pass is not firing even when it should.** Five shelves
   held a glare-blown or off-slogan reference (exposure 18–36, on-slogan
   63–78) and the new crop beat it by 10–21 points, comfortably past the
   10-point rule — yet every one reached you, and both session headers read
   "auto-replaced 0". Something is disqualifying the candidates before the
   margin test. My best reading of the code is the 50,000-pixel floor
   (`MP_FLOOR_PIXELS`): a crop cut from a dense sweep photo can be under
   224×224 and is then ineligible for any automatic swap, however good it
   is. That is a defect to confirm with one log line, not a tuning question.
4. **Duplicates keep coming back because dedup runs after the fact.**
   `/reference dedup` has run at least twelve times since June and found 2–30
   duplicates every time; `No Sugar Here` has lost duplicates in five separate
   runs. Nothing checks a crop against the shelf or the staging queue *when
   it is staged*, and ebayscout's staged crops carry no lot id, so the
   same-lot collapse cannot see 92% of the volume.
5. **None of your decisions are recorded anywhere but Slack.** The typed
   reply is printed to stdout and the replaced reference is deleted. The 57
   decisions I tabulated by hand are the only labelled data the curation
   flow has ever produced, and they cannot be re-read once the thread
   scrolls. Logging them, and keeping the retired reference for 30 days, is
   the cheapest change in this document and the one every other change
   should be calibrated on.

---

## 2. How the flywheel works today

```
confirm (inventory / scout / buy)        pipeline lot (ebayscout, unattended)
  _stage_confirmed_crop                    staging_candidates: real Hough +
    gates: flywheel on, not /sort,           Gemini-agreed + not db_direct +
    has jpg, not STOPPED (at-cap OK)         overall ≥ 0.50, then STOP list
    name: <ms>__lot-<chan-thread>.jpg        name: <ms>.jpg   (NO lot id)
            └──────────────┬───────────────────────┘
                 reference/_staging/<entry_id>/*.jpg
                            │  /reference (human starts it)
   _ref_rank_staged: score the 12 newest, best-first; collapse_same_lot
   plan_session: below cap → auto-fill best to 4; at cap → review
   _ref_auto_replace_pass (at cap): swap if ≥ +10 or ref is junk;
                                    discard if ≤ −5; else show to human
   combined view: refs 1..R + new crops R+1.. with Q table → you type
   `A B` / `add` / `del` / `next` / `stop`; stop → _staging_policy.json
```

The composite (`reference_quality.score`) is
`0.45·on-slogan + 0.25·sharpness + 0.20·exposure + 0.10·contrast`, capped by
on-slogan agreement, where sharpness is Laplacian variance on a 256-px
short-side copy, exposure is 1 − clipped-pixel share, and on-slogan is the
CLIP cosine to the mean of the shelf's other references (leave-one-out), or
to the slogan text for an empty shelf. Pixels only break ties and set the
junk floor. Junk = under 50,000 px or composite under 0.40.

---

## 3. What the sessions show

### 3.1 The 57 decisions

| | 2026-09-06 (2006–2010 shelves, ids 421–473) | 2026-09-12 (1978–1998 shelves) | total |
|---|---|---|---|
| decisions read | 29 | 28 | 57 |
| swapped the flagged weakest (alone or with another) | 6 | 20 | 26 |
| swapped a reference that was **not** the weakest | **20** | 5 | 25 |
| no change (`next`/`stop` alone) | 3 | 3 | 6 |
| ended with `stop` | 4 | 22 | 26 |

In the 20 non-weakest swaps of 09-06, the replaced reference scored Q ≥ 92 in
19 cases and was reference #2 in 14 of them. The crop that replaced it scored
*lower* than it in 17 of the 20. The score would have called every one of
those swaps a downgrade.

### 3.2 Where you and the score agree, the margin you use

Swaps of the flagged weakest, by how far the new crop beat it:

| new − weakest | accepted | rejected |
|---|---|---|
| ≤ 0 | 0 | 4 |
| +1 | 1 | 1 |
| +2 | 5 | 1 |
| **≥ +3** | **19** | **0** |

The shipped rule swaps only at ≥ +10 and discards only at ≤ −5. Of the 19
accepted swaps at ≥ +3, eight were at ≥ +10 — the auto pass should have taken
them and did not (§3.4).

### 3.3 Every damaged reference went to a human

Shelves whose weakest reference was visibly broken by one component, and
what happened:

| slogan | weakest | defect | new crop | you |
|---|---|---|---|---|
| Shooting for the Top | Q78 | expo 20 | Q92 | swapped |
| Lion Pride Proud | Q77 | expo 18 | Q90 | swapped |
| Lions Surf the Net | Q78 | expo 36 | Q92 | swapped |
| Boot the Hoot | Q80 | expo 28 | Q90 | swapped |
| No Appeal in Lion's Court | Q79 | expo 23 | Q92 | (page ended) |
| Good Knight, Scarlet | Q87 | expo 67 | Q95 | swapped |
| Skin-ya, West Virginia | Q73 | on-slogan 63 | Q94 | swapped |
| Hoop It Up | Q81 | on-slogan 78 | Q88 | swapped |
| Slam Dunk Navy | Q83 | on-slogan 88 | Q94 | swapped |
| We Try Harter | Q79 | on-slogan 78 | Q93 | swapped |

Ten of ten. A reference with exposure under ~0.4 or on-slogan under ~0.8 is
junk to you, but the code's junk test is composite < 0.40, which a glare-blown
crop at 0.78 never trips. Per-component junk floors would have automated all
ten, provided the candidate was allowed to qualify (§3.4).

### 3.4 The auto-replace pass did not fire

Both session headers: "auto-filled 0 crop(s)", no auto-replaced line — i.e.
`_ref_auto_replace_pass` returned zero swaps and zero discards across 54 and
73 slogans, while §3.3's +10 to +21 candidates sat in the queue. In
`plan_at_cap_decisions` a swap needs `clears_floor(cand)`: **≥ 50,000 pixels**
and composite ≥ 0.40. The sessions followed the whole-collection `/scout`
sweep of 6 Sep, whose photos hold 30–80 buttons each; at the ≤2200-px working
size a button in such a photo is ~150–250 px across, i.e. 22k–62k pixels. My
reading is that most of those candidates failed the pixel floor and were
routed to review regardless of quality. The table you see does not print
pixels, so this cannot be confirmed from Slack. Either way the header says
the pass is dormant on exactly the sessions it exists for.

### 3.5 The score's dynamic range is used up

Across all 57 tables: exposure reads 96–100 on almost every crop, on-slogan
reads 97–100 on almost every crop, and composite spans 83–96 for anything
that is not broken. Only sharpness varies (49–99). So among the crops that
reach review the composite is effectively a sharpness ranking, and sharpness
is exactly the axis on which you overrode it 20 times (§3.1: replacing the
sharpest reference). The score cannot get better at agreeing with you by
re-weighting these four terms; it needs a term it does not have.

### 3.6 The likely missing term (inference — confirm in §7)

Three readings fit "replace the sharpest, keep the flagged weakest":

- **A same-original duplicate.** Two of a shelf's references are crops of one
  photograph (the 2006–2010 shelves were seeded in bulk; `REF_CAP`'s own
  comment calls slots 1–2 "curated" and 3–4 "wild"). Both score identically
  high; one is worthless. dHash at ≤ 2 bits only catches recompressed copies,
  and the reference Gem audit was built precisely because it "catches
  duplicates that dedup misses". `/reference dedup` removed 2 from `No Greece
  Lightning Here` and 2 from `ROAR Beats Meow` in earlier runs — both shelves
  you edited again on 09-06.
- **A clean image that does not look like the feed.** Sharpness 95–99 and
  exposure 100 describe a scan or studio shot. For CLIP matching against
  eBay lot photos, a slightly soft real-world crop on a real background is a
  better reference than a perfect one, and you may be trading the perfect
  one out for variety.
- **A framing or identity problem the score cannot see** — the crop includes
  a neighbouring button, is cut off, or is a different edition/year of the
  same slogan (the twin registry's domain).

All three point the same way: the value of a reference is its **marginal
contribution to the shelf** — how different it is from what the shelf already
holds and how much it looks like what the matcher will meet — not its
standalone quality. That is the term to add (RS-06), and its weight has to
be fitted on logged decisions (RS-01), not guessed.

### 3.7 Is the score measuring what the process needs? — No (added 2026-09-15)

The composite rates a crop as a *photograph*. The process needs references
that make CLIP rank the right slogan first on real lot photos. Three
mismatches:

- **The heaviest term rewards sameness.** "On-slogan" (45%) is the cosine to
  the mean of the shelf's other references, so a near-duplicate scores 100
  and a genuinely different view scores lower — the opposite of what a
  retrieval shelf needs, and consistent with the 20 "replace the sharpest"
  swaps in §3.1.
- **Sharpness is mostly invisible to the matcher.** CLIP sees a 224-px crop;
  Laplacian variance above that rewards detail the model never gets (25% of
  the score). Exposure, which does move the embedding, is 20%.
- **There is no outcome term.** The logs show references cut both ways:
  "Stuck in a Rut" went rank 31 → 1 from staged references (Logger_12), and
  the C1 attractors ("Happy 125th Penn State", "Penn State and Proud of it",
  "Never Badger A Lion") pull wrong crops to #1 at image 0.90–0.98. Those are
  almost certainly crisp, well-exposed, high-composite images — the score's
  best, the process's worst.

**The measure that is aimed at the process, computable today from
`vectors.pt` alone (3,259 labelled crops, one matrix multiply, the same pass
`/reference reindex` already runs):** hold each reference out and match it
against the rest of the library.

1. *Typicality* — does the held-out reference retrieve its own slogan at #1?
2. *Usefulness* — with it removed, how many shelf-mates stop retrieving
   correctly, or by how much does their margin over the runner-up shrink?
   Duplicates contribute ~0; a distinct view contributes a lot.
3. *Attractor risk* — how many other slogans' references now retrieve this
   one at #1 or #2? (C1's sticky-attractor list for every reference.)

Rank by usefulness − attractor risk; keep the composite only as the junk
filter (its exposure / on-slogan floors correctly caught all ten broken
references in §3.3). Check the new ranking against the 57 decisions before it
decides anything — if it recovers the §3.1 swaps, §7 question 1 is answered.

Caveat: within-library retrieval tests references against references, not
against feed crops, which are lower-quality and cluttered. Confirmed feed
crops would be the better test set; they are not stored today except as
staged crops, so the proxy is what is available now. This is folded into
RS-06 as its first step.

---

## 4. Answers to the three questions

### 4.1 Ignoring bad crops more often

Today nothing quality-based happens at intake; every confirmed crop is staged
and judged only when you run `/reference`. Move the judgement to intake and
tighten the at-cap margins to what you actually do:

1. **Score at staging time, in the request that already has the crop and the
   model.** Both services have cv2 and CLIP loaded when a crop is confirmed.
   Compute the same `QualityScore` there, plus cosine to the shelf's existing
   references and to the crops already staged for that entry. Then:
   - shelf **at cap** and composite ≤ weakest + 1 → do not stage (log
     `STAGE_SKIP … below shelf`);
   - cosine ≥ 0.985 to any existing reference or staged crop → do not stage
     (`STAGE_SKIP … duplicate`), see 4.2;
   - per-component junk (exposure < 0.4, on-slogan < 0.8, short side
     < 160 px) → do not stage unless the shelf is below cap or its weakest is
     itself junk on that component;
   - otherwise stage, and write the score into the blob's metadata so
     `/reference` does not recompute it.
   The review queue then holds only contenders. (`STAGE_SKIP` already exists
   as the logging shape.)
2. **Recalibrate the at-cap margins to your own decisions:** swap at ≥ +3
   over the weakest, discard at ≤ 0, review only +1..+2. From §3.2 that is
   19/19 correct on the swaps and 4/4 on the discards, and would have turned
   most of the 09-12 session into a header line. Keep the margins as
   constants in `reference_quality.py`, but put the values in
   `HYPOTHESES_IN_PROGRESS`-style provenance: "fitted 2026-09 on 57
   decisions; re-fit when the decision log (RS-01) reaches 250".
3. **Per-component junk floors** (§3.3): `is_junk` gains `exposure < 0.4 or
   clip < 0.8 or short_side < 160`. A junk reference is replaced by any
   candidate that clears the floors, no margin — which is what you did ten
   times out of ten.
4. **Auto-stop the shelves you would stop.** In the 09-12 session you typed
   `stop` on 22 of 28; in the 09-06 session on 4 of 29. The difference is
   not the shelves, it is that you had decided to stop things that day.
   Propose a definition so the system can do it: a shelf is *finished* when
   it holds 4 references, all with composite ≥ 0.88 and no component junk,
   from at least 2 distinct source lots, with max pairwise cosine ≤ 0.97
   (no duplicates). A finished shelf goes on the stop list automatically and
   is announced in the `/reference` header ("auto-stopped 31 finished
   shelves"); `unstop` still works; the human `stop` still overrides in
   both directions. Whether an *exceptional* crop (≥ +10 over the weakest)
   should be allowed through a stop is your call — today stop is absolute
   and I would keep it so until the log says otherwise.

### 4.2 Making deduplication actually hold

Three layers exist; the gap is that the cheap ones run too late and the
accurate one is manual.

1. **Dedup at intake, against the shelf and the staging queue** (RS-03). The
   same cosine screen `/reference dedup` uses (≥ 0.99, with the dHash
   confirm) applied when a crop is about to be staged. This is what stops `No
   Sugar Here` losing a duplicate in every run: the duplicate never lands.
   Relisted eBay items (the same photo under a new item id, seen twice in
   `#ebay-checker` this month) are caught by the same check.
2. **Give ebayscout's staged crops a lot id** (RS-04). `_stage_confirmed_crop`
   names crops `<ms>__lot-<channel>-<thread>.jpg` so `collapse_same_lot` can
   show you one crop per photo; `promote_crops_to_reference_staging` names
   them `<ms>.jpg`. Use `__lot-<job_id>` (or the eBay item id) and the "+N
   more from this lot" collapse starts working on the pipeline's share of the
   queue. Trivial and overdue.
3. **Treat the 0.95–0.99 band as redundancy, not identity** (RS-06). The
   docs measured that same-lot copies and different designs can sit one dHash
   bit apart, so no hash threshold is safe for deletion. But the shelf does
   not need deletion to benefit: a reference whose nearest sibling cosine is
   ≥ 0.97 is the *first candidate to replace*, and a new crop whose nearest
   shelf cosine is ≥ 0.97 adds nothing and should rank below a slightly
   softer but distinct one. That is the marginal-value term, and it is
   exactly what §3.1 suggests you are applying by eye.
4. **Retire, don't delete** (RS-01). Every reference removed by a swap,
   `del`, dedup or audit moves to `reference/_retired/<entry_id>/<ts>.jpg`
   with a 30-day lifecycle rule instead of being deleted. Undo becomes
   possible, and the retired set is the labelled "what a human rejected"
   corpus the scoring change needs.
5. **Re-run the Gem audit only on shelves that changed** — it already does
   (`_audit_state.json`), and the 27 June run covered 797 slogans; nothing in
   Slack shows its findings were acted on. If the audit's duplicate groups
   are still sitting unresolved, resolving them is one session and is the
   best available ground truth for the 0.97 threshold above.

### 4.3 Filling in clearly better images automatically

1. **Fix the dormant auto-replace pass first** (RS-02). Print, per slogan,
   the swap/discard/review counts and the reason each candidate was refused
   (`below_pixel_floor`, `below_quality_floor`, `within_margin`), and add
   `pixels` to the quality table. If the pixel floor is the cause, replace
   the absolute 50,000 with `short_side ≥ 160 px` **or** `pixels ≥ 0.6 × the
   shelf's median` — a reference should not be refused for being the same
   size as the ones it would join.
2. **Then lower the swap margin to +3 and the discard to 0** (RS-05), the
   §3.2 fit.
3. **Then replace "weakest by composite" with "lowest marginal value"**
   (RS-06): `value = quality × novelty`, where novelty is
   `1 − max cosine to the other references`. The junkiest reference and the
   most redundant reference both become the natural swap target, and a new
   crop's gain is measured against the shelf it would join. Fit the weight
   on the decision log; the 57 decisions here are enough to see whether the
   term recovers the 20 "replace the sharpest" swaps at all — if it does not,
   the answer to §7 will say what does.
4. **Below cap, keep auto-fill but stop it filling with near-duplicates.**
   `pick_autofill` takes the best `need` crops after `collapse_same_lot`;
   add the cosine screen so two crops of one appearance from *different*
   lots do not both fill a shelf.
5. **The 41 empty shelves and 8 `_year_*` folders** are not a scoring problem;
   they are a photography task (the list is in `#inventory-bot-debug`,
   2026-09-12 13:58). Nothing above helps a slogan with no photo.

---

## 5. Instrument the human first

Every number in §3 came from reading Slack by hand. The flow should write its
own record:

- **`reference_log`** — one row per decision, written where `_ref_apply_typed_plan`
  and `_ref_typed_advance` apply it: entry id, session, action
  (`swap`/`add`/`del`/`next`/`stop`/`auto_swap`/`auto_discard`/`auto_fill`),
  each reference's components + pixels + nearest-sibling cosine, each
  candidate's the same, which indices were chosen, and the source lot ids.
  A Sheets tab next to `Bot Writes` or a GCS JSONL; either is fine. This is
  the training set for every threshold above.
- **`reference/_retired/`** as in 4.2.4.
- **A `/reference stats` line** in the session header: shelves touched,
  auto-resolved vs typed, mean typed replies per slogan — so "did the change
  reduce the typing" is a number, not an impression. Today's baseline from
  these two sessions: **one typed reply per slogan, 57 of 57**.

---

## 6. Tickets, in order

| ID | Repo | Size | What | Done when |
|---|---|---|---|---|
| RS-01 | buttonmatcher | S | decision log + retired-reference prefix (§5) | a typed `2 5 stop` produces one row with both images' scores; the replaced blob exists under `_retired/` |
| RS-02 | buttonmatcher | S | auto-decide refusal reasons + pixels in the table; fix the pixel floor if it is the cause (§4.3.1) | the next `/reference` header reports non-zero auto-replacements on shelves like §3.3, or the log names another cause |
| RS-03 | both | M | intake dedup: cosine ≥ 0.99 + dHash confirm against shelf and staging queue at stage time; `STAGE_SKIP duplicate` (§4.2.1) | `/reference dedup` finds 0 on a fresh week; a relisted eBay photo is skipped with the reason logged |
| RS-04 | ebayscout | S | `__lot-<job_id>` in staged names (§4.2.2) | the "+N more from this lot" note appears on pipeline-sourced entries |
| RS-05 | buttonmatcher | S | margins +3 / 0, per-component junk floors (§4.1.2, 4.1.3) | replaying the 57 decisions: ≥ 19/19 swaps and 4/4 discards reproduced; a unit test pins the replay |
| RS-06 | buttonmatcher | M | marginal-value scoring: first the leave-one-out retrieval value per reference (§3.7), then the novelty term, redundant-first replace target, intake gate on cosine ≥ 0.97 (§4.2.3, 4.3.3) — **shadow first**: log what it would do beside what the composite does, for two sessions | agreement with typed decisions ≥ the composite's; then flip |
| RS-07 | buttonmatcher | S | intake-time scoring and at-cap gate (§4.1.1) | review queue per session halves at equal shelf quality (spot-audit 20 shelves) |
| RS-08 | buttonmatcher | S | auto-stop finished shelves, announced in the header (§4.1.4) | the stop list grows without typing; `unstop` unchanged |

RS-01 and RS-02 are a day together and should ship before anything else: one
gives the data, the other likely restores the automation that already
exists. RS-05 is the biggest typing reduction per line of code. RS-06 is the
only real research item and must run as a shadow against RS-01's log.

---

## 7. Questions only you can answer

1. **In the 09-06 session you replaced reference #2 on 14 of 29 shelves, and it
   was the top- or second-scored reference almost every time. What was wrong
   with it?** (same photo as #1 · a scan/stock image · wrong edition · bad
   framing · something else). The answer chooses between the three readings
   in §3.6 and decides what RS-06 measures.
2. **Should `stop` stay absolute**, or may a crop that beats a finished shelf's
   weakest by a wide margin still get through? (§4.1.4.)
3. **Were the 27 June reference-audit findings (797 slogans) acted on?** If not,
   they are the ground truth for the redundancy threshold.
4. **What size are the staged crops from the 6 Sep sweep?** One
   `gcloud storage ls -l` on `reference/_staging/` or the `pixels` column from
   RS-02 settles §3.4.

---

## 8. Not verified

No image was viewed and no GCS object read; every quality number is as the
bot printed it in Slack. The pixel-floor explanation in §3.4 is a reading of
`plan_at_cap_decisions` against the sweep's likely crop sizes, not a
measurement. The 57 decisions are two sessions and one reviewer; the margins
in §4.1.2 are a fit to them and should be re-fitted once RS-01 has a few
hundred rows.

---

## 9. Implementation check — RS-01 / RS-02 (2026-09-15)

*Branch `claude/buttonmatcher-strategic-review-bev1z4`, commit `66a9786`, not
yet merged. CI green on the branch; 1,132 pure tests pass locally. This is
good work and it found two things the review did not.*

**RS-02 — closed, and the diagnosis is better than the ticket's.** The size
floor was on *area* (50,000 px), which refuses a 200×200 crop; it is now a
160 px short-side floor, and the nine §3.3 shelves replay as 7 of 9
auto-swaps (the other two are the +7/+8 cases that belong to RS-05). The
second finding is the important one: the auto pass scored incumbents
against a mean they were **part of** while candidates were scored against a
mean they were not, worth up to 18–27 composite points on a varied shelf —
so the pass was defending the status quo hardest on exactly the shelves the
library wants. The numbers you saw in the Slack table were always
leave-one-out; the pass's were not. One definition now. Refusal reasons are
counted per session and printed in the header, and the tables show each
crop's size. **Watch the first `/reference` after deploy:** the header's
`left for you:` line names the cause — "too small" means the floor was it
and is fixed; "within the margin" means RS-05 is next.

**RS-01 — closed.** `reference_log/YYYY-MM.jsonl`, one row per decision,
carrying every image on screen with components, size, nearest-sibling cosine
and source lot; automatic decisions logged too; `next` logged before the
discard. Removed references now go to `reference/_retired/<entry_id>/`
rather than being deleted, from swaps, `del` and dedup alike.

Three follow-ups, none blocking merge:

1. **Exclude `_retired` from the no-cache hydration listing.** `hydrate_data`
   lists `reference/` and excludes only `_staging` (`main.py` ~1651). A cold
   start with no `vectors.pt` will download every retired blob and then log
   `!!! HYDRATION: reference folder '_retired' is neither a year nor a
   text_db entry id — skipped`. Nothing is encoded wrongly (the folder holds
   sub-folders, not images), but it is wasted download and a false alarm.
   Add `_retired` next to `_staging` in both the listing filter and
   `ref_folders`.
2. **Operator: add a 30-day lifecycle rule on `reference/_retired/`** (the
   review asked for it; the commit does not mention it). Without it the
   prefix grows one image per swap forever.
3. **RS-05's gate needs the 57 decisions in machine-readable form.** The
   commit says so. They exist only as the tables in §3 of this document; a
   JSON fixture of them (entry id, ref scores, candidate scores, chosen
   indices, action) would let the margin re-fit be a unit test on day one
   rather than wait for `reference_log` to accrue.

---

## 10. The finish line (2026-09-18, rewritten the same day)

**The first version of this section was wrong, and wrong in the way §3.7
warned against.** It read C8's null result ("shelf redundancy does not
predict outcome, p = 0.12") as "variety is not the gap" and set the finish
line on how many shelves a session queues. But the shelves C8 measured were
all built by the sameness score: 87% of their references sit at a saturated
100 on the term that rewards looking like what is already there. A
correlation cannot be read off a sample that has no range. The library was
selected for similar crops, so the only thing the library can show is that
similar crops all behave the same. The operator's objection is the correct
one: **the finish line is a library of crops that make matching better, and
nothing in the curation loop has ever measured that.** This section replaces
the queue-size framing entirely. Queue size is a consequence of getting the
criterion right, not the goal.

### 10.1 What "makes matching better" means, exactly

A reference photo has one job: when a real lot photo of that button arrives,
its year and its slogan should win. So the value of a reference is measured
on real confirmed crops, never on other references:

- **Held-out set per shelf.** Every confirmation is a labeled crop. For entry
  *e*, H_e is its confirmed crops. The export today has 723 confirmed
  (slogan, year) keys; 560 have at least 3 crops, 372 have at least 5
  (2,523 crops), 40 have at least 10. That is enough to score most active
  shelves now and every active shelf within weeks at the current 200
  confirmations a day.
- **Hard negatives per shelf.** confirm_log also records who beat whom: 402
  distinct (truth, winner) pairs, 40 seen three times or more, 429 keys
  involved. N_e is the confirmed crops of the entries *e* is confused with,
  in either direction.
- **Value of a reference set R for shelf e:**
  the count of H_e crops on which *e* ranks #1 at slogan level under R,
  minus the count of N_e crops on which *e* wrongly ranks #1 under R.
  Computed with the live formula (year image score = max over R, text
  unchanged, un-folded board), so the number IS the matcher's answer.
- **Marginal value of one reference** = Value(R) − Value(R without it).
  Zero means the photo changes no outcome: dead weight, first to be
  replaced. **Marginal value of a candidate** = Value(R with it, minus the
  weakest-by-marginal-value) − Value(R). Positive means stage and swap; zero
  or negative means discard. No composite, no margin, no click.

This is §3.7's leave-one-out retrieval value, and RS-06's first half, made
the whole criterion instead of a term inside the old one.

### 10.2 Why it is cheap: the image side of the matcher is a dot product

Everything the value function needs is an embedding. At match time the
service already computes each crop's L2-normed ViT-B/32 vector (`vecs`,
`main.py` 1775) and multiplies it against `ref_vectors` and the text bank;
then it throws the vector away. `vectors.pt` (every reference's vector) and
`text_features.pt` (every slogan's) are already blobs in the bucket. So:

1. **Persist crop vectors at match time.** One small array per job
   (`pipeline/embeddings/<job_id>.npy`, 512 floats per crop, keyed
   `crop_num`), written next to the `pipeline/labels/` sidecar that has
   existed since 2026-07-11. Slash-flow crops the same, keyed on
   `thread_ts` + `crop_num`. Fail-open, kill switch, a few lines at the
   three call sites. **This is the prerequisite for everything below and
   it is the first PR.**
2. **Backfill.** 3,944 of the 4,620 usable confirmations are pipeline rows
   with a `job_id`, all since the sidecar shipped, so their crops can be
   re-cut from the stored detection image and circles and embedded once, in
   one `/internal/` request per batch (CPU inside a request, per the
   standing constraint). The 676 slash-flow rows without a sidecar are lost
   as held-out data; their crops that were staged still exist as JPEGs.
3. **The value function runs offline** on the vectors and the two `.pt`
   blobs: pure numpy, no CLIP, no Cloud Run, runs in Cloud Shell or here.
   It replays the matcher's image side for any hypothetical shelf in
   milliseconds. `tools/eval_reference_value.py` grows a `--embeddings`
   mode; the replay of the live formula is what `match_logging.
   build_leaderboard` already is.
4. **Candidates arrive with their vector.** A staged crop is a confirmed
   crop, so its vector was computed when it was matched; the staged name
   must carry `job_id` and `crop_num` to join it. That is RS-04, and it is
   now a prerequisite, not a nicety.

### 10.3 The curation rule that replaces the composite

- **Intake (RS-07, redefined):** a candidate for shelf *e* is scored by
  marginal value against H_e and N_e. Positive → stage as a swap for the
  reference with the lowest marginal value, applied by the auto pass. Zero
  or negative → discard, logged with both numbers. A shelf with fewer than
  3 held-out crops falls back to the current composite and is marked
  `cold` in the header; it warms itself as confirmations arrive.
- **Rebuild of what exists:** one pass over every shelf, marginal value per
  reference. A reference at zero on a shelf with 5+ held-out crops is
  retired to `_retired/` the moment a positive-value candidate exists; the
  shelf never drops below its current count. This is how a library built
  for sameness turns into one built for retrieval without a rebuild day.
- **The attractors fall out of the same rule.** `Penn State and Proud of it`
  1992 holds 28 stolen year slots; on its own hard negatives its references
  have strongly negative marginal value, so the rule retires them without
  anyone naming the shelf. C1 stops being a hand-curated list.
- **The composite survives as a floor only:** `MIN_SHORT_SIDE`, exposure
  clipping, blur. It decides what may not enter, never what stays.
- **STOP means "do not ask me", never "do not add a clearly better crop".**
  Operator decision, 2026-09-18. A stopped shelf is skipped by the human
  queue only; the auto pass still stages, scores and swaps on it when the
  value function is positive, and announces the swap in the header. The
  `stage_skip_reason` gate that currently refuses staging on a stopped
  shelf (`main.py` `_stage_confirmed_crop`) is therefore wrong under this
  definition and changes with step 5 of §10.5: `stopped` moves from the
  stage gate to the review-queue filter.
- **Human review** is reserved for cold shelves and for the case the rule
  cannot see: a candidate whose confirmation was itself wrong. Wrong
  confirmations are the `correction` rows of WS2 (SR-05), which is the
  other reason that lane matters.

### 10.4 Conditions for done

| | today | done |
|---|---|---|
| slogan-level confirmed-#1 on held-out crops, all shelves | 83.0% | rises each month and is reported per shelf; no fixed target, the trend is the gate |
| references with zero marginal value on shelves with 5+ held-out crops | unmeasured | 0 |
| shelves whose value went DOWN after a swap | unmeasured | 0 (the rule cannot produce one; a non-zero count is a bug) |
| confirmed slogans with no reference | 9 of 723 | 0 after 30 days of feed |
| `/reference` human queue | 122 shelves | cold shelves only |
| duplicates at intake | not gated | a candidate at cosine ≥ 0.99 to a shelf-mate has marginal value ≤ 0 by construction; RS-03's dHash remains for the identical-photo case |

### 10.5 Order of work

1. Persist crop vectors at match time (both repos; the write is a few
   lines, the join key is `job_id` + `crop_num`). RS-04 in the same PR.
2. Backfill: re-cut and embed the pipeline confirmations from
   `pipeline/labels/`, one `/internal/` batch. Export `vectors.pt` and
   `text_features.pt` once for offline use.
3. `eval_reference_value.py --embeddings`: marginal value per reference and
   per staged candidate, offline. Run it on the current library and publish
   the per-shelf table. **This is the first honest measurement of the
   library and it needs no live change.** Expect the 87% saturated
   references to split into a few that carry a shelf and many at zero.
4. Shadow the rule for one session: log what marginal value would decide
   beside what the composite decides, on every candidate.
5. Flip: intake and the auto pass use marginal value; composite becomes the
   floor. Kill switch.
6. RS-05 and RS-06 as previously specified are **closed by this**: the +3/0
   margin and the novelty term were both proxies for value, and value is
   now measured directly. C8 is answered the same way: the question was
   never whether redundancy correlates with outcome in a library that has
   no variety, it was whether each photo changes an outcome, and that is
   now a column.

### 10.6 What this does not fix, said plainly

The 176 year-absent misses in `OFF_BOARD_MISSES_PLAN_2026-09-18.md` §3a are
the slice where reference photos are the lever, and this is what moves
them: those shelves have references (166 of 176) that do not recognise the
real crops, which is exactly a zero-or-negative marginal value. The 183
year-taken misses are the un-fold's. Ranking misses with the truth on-board
(217) are the rerank's. None of the three is the curation queue's.

### 10.7 Implementation state (2026-09-18, the implementers)

Steps 1-4 are built and on `claude/reference-image-db-status-ggg2jm` in both
repos; step 5 is a switch, with nothing left to build for it. **None of it has
run against GCS, CLIP, Slack or Cloud Run** — no web session can — so every
number below is a test result or a count of code, never a measurement of the
library. The first real measurement is the operator's, in the order under
"What the operator runs".

| step | state | where |
|---|---|---|
| 1. persist crop vectors + RS-04 | **built** | `crop_vectors.py` (shared, byte-identical), `vec_sink` in `match_all_crops` / `match_crops_with_diagnostics`, both buttonmatcher lanes + the ebayscout pipeline, `seen_items.promote_crops_to_reference_staging` |
| 2. backfill + bank export | **built** | `reference_backfill.py`, `/reference backfill [N]`, `/reference export` |
| 3. marginal value per reference | **built, never run** | `reference_value.py` (pure), `tools/value_replay.py`, `tools/eval_reference_value.py --embeddings --out --out-cases` |
| 4. shadow one session | **built** | `BUTTONMATCHER_REFERENCE_VALUE=shadow` (default), `value_shadow` on every `reference_log` decision row, a header line |
| 5. flip | **a switch** | `BUTTONMATCHER_REFERENCE_VALUE=live`; the composite stays as the intake floor |
| 6. RS-05 / RS-06 closed by this | **honoured** | neither margin was moved; `COMPOSITE_MARGIN` is still 0.10 and `DISCARD_MARGIN` 0.05, and they stop mattering the moment step 5 flips |

**What the data contract is**, so nothing downstream has to guess:

- `pipeline/embeddings/<job_id>.npz` — `crop_num` int32, `vec` float32 [N, 512],
  `meta` (json: `lot_key`, `job_id`, `service`, `source` = `match`|`backfill`).
  float32 and not float16 because the value function ranks thousands of
  near-identical cosines and half precision can reorder a shelf.
- `reference/_staging/<entry>/<ms>__lot-<job_id>__crop-<n>.jpg` — RS-04, in both
  repos. `source_lot` stops at the next `__`, so adding the crop number cannot
  silently turn every crop of one photo into its own "lot".
- `reference_value/ref_vectors.npz`, `text_features.npz` (with the exported
  `word_freq` table), `entries.npz` — written by `/reference export`.
- `reference_value/shelf_value.json` (the published table) and
  `shelf_cases.npz` (the evidence, with each crop's vector) — written by the
  tool, read by `/reference` for the shadow.

**Three things the implementation had to decide, which §10 left open.** Each is
a property of the live board rather than a choice, and each is pinned by a test:

1. **The image score is the YEAR's max, over every reference of that year — not
   the shelf's.** §10.1 says "year image score = max over R"; the live scorer's
   `year_scores` is the max over the year's whole reference pool. So one entry's
   photos lift every slogan sharing its year (which is the mechanism behind C1's
   attractors), and a reference is worth nothing on a crop a year-mate already
   carries. The implementation uses the live definition and carries the
   year-mates' best as `pool_sim`; ignoring it would credit a shelf for outcomes
   it had no part in.
2. **A reference photo cannot make a slogan beat a same-year sibling.** The
   board's #1 row is always its year's text-argmax, so for a crop whose year is
   taken by another slogan NO reference set can rank it #1. Those crops are
   counted in their own `unwinnable` column and never as misses — which is what
   keeps this measure from claiming the un-fold's work, and from sending the
   operator to re-shoot a shelf that is losing on text. §10.6 already says those
   183 rows are A7's; this is that statement made structural.
3. **The blend is never restated.** A row's `overall` is affine in the year image
   score, so the threshold is exact — but the slope and intercept are solved by
   probing `match_logging.build_leaderboard` at two image scores rather than
   re-deriving the 0.5/0.5 weights, the near-certain-text boost, the weak-text
   penalty and the rarity tiebreaker. A slope the live formula cannot produce is
   refused, so a scorer that gains a term fails loudly instead of being quietly
   mis-described.

**What the operator runs, in order.** Each step is one Slack command or one
Cloud Shell command, and each is safe to repeat:

1. `/reference export` — the three banks. Cheap; re-run after any text_db edit.
2. `/reference backfill` — 200 lots a batch, resumable, skips lots already done.
   Repeat until the header says 0 left. This is the only expensive step (one
   download + CLIP encode per lot) and it is once per lot, ever.
3. In Cloud Shell:
   `gcloud storage cp gs://<bucket>/reference_value/*.npz .` ·
   `gcloud storage cp -r gs://<bucket>/pipeline/embeddings .` ·
   `python tools/eval_reference_value.py --confirm-log confirm_log.csv
   --embeddings embeddings --out shelf_value.json --out-cases shelf_cases.npz`
   — **the first honest measurement of the library.** §10.5 expects the 87%
   saturated references to split into a few that carry a shelf and many at zero.
4. Upload both files back under `reference_value/`.
5. Run `/reference` once and read the `value shadow:` header line. The number
   that decides the flip is not the agreement rate — a rule that agreed
   everywhere would change nothing — it is whether the disagreements are ones the
   value rule can defend, plus `target_disagree` (both rules saying "swap" and
   disagreeing about what goes, which is IC-10's warning).
6. `BUTTONMATCHER_REFERENCE_VALUE=live` when they do.

Operator actions still outstanding from earlier tickets, unchanged by this work:
the 30-day lifecycle rule on `reference/_retired/` (IC-07), and `C1`'s attractor
shelves — though §10.3 predicts the value rule retires those without anyone
naming them, and step 3's table is where to check that before re-shooting
anything.
