# Hough Detection & Logging Updates

> **HISTORICAL (frozen 2026-07-09).** This is the design record of the
> original multi-pass Hough phases and the early logging schema. Everything
> here shipped and was later superseded as the frontier moved to the mask and
> the radius estimate — see `tested_hypothesis.md` Part III for the verdicts,
> `AUTOMATION_VISION.md`/`AUTOMATION_ROADMAP.md` for live strategy. The
> schema below (54 columns) is outdated — `match_logging.py` is the canonical
> schema (79 columns as of 2026-07-09); buybot was decommissioned 2026-07-05;
> the file names `main__3_.py`/`main__4_.py` are pre-refactor. The deployment
> checklist at the bottom was executed long ago — do not re-run it.

**Goal:** automate eBay button identification by measuring and improving every stage
of the detection and matching pipeline, with no changes to the user-facing flow.

---

## Architecture overview *(as of writing — buybot since decommissioned)*

Three bots share the same core detection and logging infrastructure:

| Bot | Command | Detection path |
|---|---|---|
| buttonmatcher | `/inventory`, `/sort` | `detect_buttons()` in `main__3_.py` — user supplies count/grid |
| ebayscout | `/scout`, `/crawl <N>` | `image_proc.detect_and_crop()` — fully automated, no user input |
| buybot | `/buy` | Single-image match; count = 1 |

`match_logging.py` is shared byte-for-byte across all three. Every detection event
writes one row per crop to `match_log`; every human confirmation writes one row to
`confirm_log`.

---

## Hough changes

### Phase 1 — Multi-pass scoring (`count_circles_unguided` / scan mode)

**File:** `main__3_.py` (shadow path), `image_proc.py` (scan mode)

**Problem:** A single `param2=24` Hough pass over-detects by 2× on average.
There is no way to know which circles are real without a count to cap against.

**Solution:** Run three passes in parallel at different accumulator thresholds
and score each on internal quality:

| Pass | `param2` | Character |
|---|---|---|
| conservative | 40 | strict — few false positives |
| standard | 24 | original behaviour |
| loose | 15 | permissive — catches faint/partial circles |

Each pass sweeps five radius scales. After fill-ratio filter and overlap dedup,
the set is scored on four criteria (weighted sum):

| Criterion | Weight | Signal |
|---|---|---|
| fill_mean | 0.40 | Fraction of each circle that is blue — noise circles score near 0 |
| spacing_cv | 0.30 | 1 − CV of nearest-neighbour distances — real grids are uniform |
| radius_cv | 0.20 | 1 − CV of radii — genuine buttons are all the same size |
| coverage | 0.10 | Tent function peaking at 40 % image coverage |

The highest-scoring pass wins. In buttonmatcher this runs inside
`count_circles_unguided` (shadow path only — never shown to the user). In
ebayscout it runs inside `detect_and_crop` scan mode (replaces the old single
`param2=24` sweep, total Hough calls: 5 → 15).

**Logged:** `ni_conservative`, `ni_standard`, `ni_aggressive`, `ni_pass_winner`,
`ni_confidence`

---

### Phase 2 — Contour fallback

**File:** `main__3_.py` (inside `count_circles_unguided`)

**Trigger:** winning pass confidence < 0.65 after Phase 1.

`cv2.findContours` runs on the HSV mask. Proposals are filtered by circularity
(`4π·area / perimeter²`) and merged with the Phase 1 circles using the standard
`0.7·min(r1,r2)` overlap dedup rule. The merged set is re-scored; adopted only if
it beats the Phase 1 result.

**Logged:** `ni_contour_count`, `ni_merged_count`, `ni_source`
(`hough_only` | `hough+contour`)

---

### Phase 3 — CLAHE/LAB preprocessing variant

**File:** `main__3_.py` (inside `count_circles_unguided`)

**Trigger:** confidence still < 0.65 after Phase 1 + 2.

The image is converted to LAB colour space and CLAHE (`clipLimit=2.0,
tileGridSize=8×8`) is applied to the L channel. The enhanced image is run through
the standard HSV masking pipeline, and all three Hough passes are re-run on the
new mask. Adopted only if its best score beats the Phase 1+2 winner. Specifically
helps when buttons blend into a similarly-coloured background (blue on dark blue).

**Logged:** `ni_variant` (`hsv` | `clahe_lab`)

---

### Varying param2 in image_proc (ebayscout live path)

**File:** `image_proc.py`

Previous: 5 Hough calls per image, all at `param2=24`.
After: 15 calls (3 param2 values × 5 radius scales), scored by `_score_solution`,
winner feeds the existing cap + row-grouping downstream. Count mode unchanged.

---

## Logging changes

### match_log — schema evolution

The `match_log` tab grew from its original schema to 54 columns. **Delete the old
tab before deploying** — `_ensure_tab` recreates it with the full header on the
first write.

#### Detection block — all columns

| Column | Source | Description |
|---|---|---|
| `det_h` / `det_w` | `detect_buttons` | Image dimensions after internal resize |
| `det_bg_brightness` | border sample | Mean V of 8 % border strip |
| `det_bg_saturation` | border sample | Mean S of 8 % border strip |
| `det_bg_is_white` | border sample | `True` when border is bright + low-sat (paper/white) |
| `det_mask_path` | `detect_buttons` | `blue_only` (white bg) or `blue_or_white` (wood/other) |
| `det_mask_components` | `detect_buttons` | Connected-component count in the HSV mask |
| `det_hough_pass1` | `detect_buttons` | Raw circle count from guided Hough pass 1 |
| `det_hough_retry` | `detect_buttons` | Raw circle count from strict retry pass (telemetry only) |
| `det_count_user` | `detect_buttons` | Crops returned using user-supplied count (= cap) |
| `det_count_noinput` | `detect_buttons` | Circles surviving fill + dedup before the user-count cap |
| `det_user_count` | caller | The count the user typed into the modal |
| `det_detector_used` | `detect_buttons` | `hough` or `grid` (projection fallback) |
| `det_n_crops` | caller | Final crop count fed to CLIP matching |
| `det_raw_hough` | `detect_buttons` | Total raw Hough output before any filter |
| `det_circles_rejected` | `detect_buttons` | Total circles removed across all filter stages |
| `det_rejection_rate` | `detect_buttons` | `rejected / raw_hough` |
| `det_border_removed` | `detect_buttons` | **New (Priority 5)** — circles outside margin |
| `det_fill_removed` | `detect_buttons` | **New (Priority 5)** — circles failing fill-ratio check |
| `det_overlap_removed` | `detect_buttons` | **New (Priority 5)** — circles removed by overlap dedup |
| `det_radius_min/max/mean/std` | `detect_buttons` | Radius stats from the cleaned circle set |
| `det_expected_radius` | `detect_buttons` | Expected radius derived from count + image area |
| `det_buttons_per_megapixel` | `detect_buttons` | Layout density: `user_count / (W×H/1M)` |
| `det_edge_density` | `detect_buttons` | **New (Priority 4)** — fraction of Canny edge pixels (whole image) |
| `det_brightness_std` | `detect_buttons` | **New (Priority 4)** — std of V channel across whole image |

#### Unguided / shadow block — ni_* columns

| Column | Description |
|---|---|
| `ni_conservative` | Circle count from Phase 1 conservative pass (param2=40) |
| `ni_standard` | Circle count from Phase 1 standard pass (param2=24) |
| `ni_aggressive` | Circle count from Phase 1 aggressive pass (param2=15) |
| `ni_selected` | Final count after layout demotion — the automation estimate |
| `ni_confidence` | Composite quality score of the winning pass (0–1) |
| `ni_layout_conf` | Fraction of selected circles fitting the inferred grid |
| `ni_outliers` | Circles that didn't fit any row or column |
| `ni_pass_winner` | Which pass won: `conservative` / `standard` / `aggressive` |
| `ni_contour_count` | Contour proposals generated in Phase 2 (None if not triggered) |
| `ni_merged_count` | Size of the merged set evaluated in Phase 2 |
| `ni_source` | `hough_only` or `hough+contour` |
| `ni_variant` | `hsv` or `clahe_lab` — which preprocessing variant won |

#### Key automation metric

**`ni_selected` vs `det_count_user`** is the primary automation tracking column.
Cross-reference against `det_bg_is_white`, `det_edge_density`, and `ni_confidence`
to identify which image conditions still require the human count input.

---

### confirm_log — new source values

The `source` column now accepts these additional values from ebayscout:

| Source | Written by | Meaning |
|---|---|---|
| `human_verify_yes` | `scout_verify_yes` action | User confirmed yellow button is in the lot |
| `human_verify_no` | `scout_verify_no` action | User confirmed yellow button is NOT in the lot |
| `user_count` | `scout_count_*` action | User's visual button count for the lot (bucket in `chosen_phrase`) |

`human_verify_yes` and `human_verify_no` together constitute the ground-truth
signal for matching quality on uncertain (yellow) buttons. `user_count` provides
ground truth for unguided count estimation — join `chosen_phrase` (the bucket
string, e.g. `"11-20"`) against `det_count_noinput` on `job_id` to measure
how close the automated count comes to what a human sees.

---

## ebayscout — /crawl person-in-the-loop

**File:** `main__4_.py`

### Yellow button review

After `_evaluate_listing` completes, any crop where
`RED_THRESHOLD ≤ overall < GREEN_THRESHOLD` (0.65–0.82) that did not auto-confirm
is collected into a `yellow` dict and posted to Slack as a compact review message:

- **Step 1** — "How many buttons do you see?" — five quick-select count buttons
  (1–5, 6–10, 11–20, 21–30, 30+). One tap logs a `user_count` confirm_log row.
- **Step 2** — One section per yellow button with ✅ Yes / ❌ No buttons inline.
  No crop image upload, no modals.

The crawl does not wait for responses — it posts and continues immediately.
Responses are logged whenever the user taps, asynchronously.

### Minimum 50 posts per crawl

Every crawl guarantees at least 50 Slack posts:

- `slack_posts_count` is incremented for every full post (yellow review or
  needed-button alert).
- At lot index ≥ `total − 50` (the final 50 lots): if `slack_posts_count < 50`,
  lots that would normally produce no Slack output are posted as plain text
  logging-only messages (no interactive buttons) until the deficit is filled.
- Natural posts (real yellow candidates or needed buttons) are never suppressed.
  Going over 50 is fine — the crawl only enforces a floor, not a ceiling.

### Incorrect auto-match guard

Data analysis revealed one auto-resolve with `rank_shadow=2` and `gap=NaN`
(only one candidate in the restricted leaderboard — the year restriction had
eliminated every alternative, leaving an uncontested option to auto-resolve on
image signal alone).

Fix in `_evaluate_listing`:

```python
MIN_AUTO_GAP = 0.05
if gap is not None and gap < MIN_AUTO_GAP and overall < AUTO_RESOLVE_THRESHOLD + 0.05:
    _cm_confirmed = False   # hold for yellow review
```

The three data rows with `gap < 0.05` (the highest-risk set) now fall to yellow
and get human verification instead of silently auto-confirming.

---

## Cloud Run structured log lines (unchanged)

`DETECT_TELEMETRY` and `CONFIRM_PICK` log lines are emitted to Cloud Run logs as
before. The Sheet logging is additive — nothing in the existing print-based
telemetry was removed.

---

## Deployment checklist

1. Deploy `image_proc.py` (ebayscout multi-pass Hough).
2. Deploy `main__3_.py` (buttonmatcher — Phase 1–3 already deployed; this adds
   Priority 4/5 metrics to `detect_buttons`).
3. Deploy `main__4_.py` (ebayscout — person-in-the-loop, min-50 posts, gap guard).
4. Deploy `match_logging.py` (54-column schema).
5. **Delete the `match_log` tab** in the LOGGER_ID spreadsheet — `_ensure_tab`
   recreates it with the full 54-column header on the first write. Old rows
   (pre-deploy) will have blank cells in the new columns, which is correct.
6. Confirm with `GET /internal/logtest` — verify both tabs get a synthetic row.
