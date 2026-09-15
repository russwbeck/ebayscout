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
| RS-06 | buttonmatcher | M | marginal-value scoring: novelty term, redundant-first replace target, intake gate on cosine ≥ 0.97 (§4.2.3, 4.3.3) — **shadow first**: log what it would do beside what the composite does, for two sessions | agreement with typed decisions ≥ the composite's; then flip |
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
4. **What size are the staged crops from the 6 Sep sweep?** One `gsutil ls -l`
   on `reference/_staging/` or the `pixels` column from RS-02 settles §3.4.

---

## 8. Not verified

No image was viewed and no GCS object read; every quality number is as the
bot printed it in Slack. The pixel-floor explanation in §3.4 is a reading of
`plan_at_cap_decisions` against the sweep's likely crop sizes, not a
measurement. The 57 decisions are two sessions and one reviewer; the margins
in §4.1.2 are a fit to them and should be re-fitted once RS-01 has a few
hundred rows.
