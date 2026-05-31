# Advanced Match / Detection Logging

**Goal:** stop guessing and start measuring, so button identification can be
*automated* with less and less human input over time. Cloud Run's log viewer is
fine for debugging but useless for analysis; this system writes **structured
rows to a Google Sheet** that you can sort, pivot, and chart.

The same `match_logging.py` module is copied **byte-for-byte** into all three
bots (`buttonmatcher`, `buybot`, `ebayscout`) so the `/inventory`, `/sort`,
`/buy`, and `/scout` commands all log identically — *no delta between the slash
commands*. Run `python tests/run_match_logging_tests.py` to verify it (20 tests,
no pytest needed).

---

## Where the data goes

Logs are written to a **dedicated logging spreadsheet**, separate from the
inventory / buy-rules workbook, identified by the **`LOGGER_ID`** secret (the
spreadsheet key). Two tabs are auto-created there on first run:

| Tab | One row per | Written when |
|---|---|---|
| `match_log` | crop (button) | at detection/match time, batched one append per image |
| `confirm_log` | human confirmation | when the user picks/confirms an answer |

> The service account must have edit access to the `LOGGER_ID` spreadsheet.
> If you change the column schema later, delete the old tab so it is recreated
> with the new header (headers are only written when a tab is first created or
> is empty).

### Verifying the write path (do this before relying on logs)

After deploy, hit **`GET /internal/logtest`** once. It writes one synthetic row
(tagged `command=/logtest`) to each tab in the `LOGGER_ID` workbook and returns
a plain-text result. Confirm both rows appear, then delete them by hand. If it
returns `match logging is DISABLED`, the `LOGGER_ID` secret or the
service-account share is the problem.

Writes are **batched** (all of an image's crop rows in a single `append_rows`)
and **fail-open** — a logging error is printed and swallowed, never raised into
the bot. If the log tabs can't be opened, logging is silently disabled and the
bot runs exactly as before.

Toggle the heavy parts off with the env var `BUTTONMATCHER_SHADOW_PASS=0`
(disables the unguided detection count and the counterfactual pass; plain match
logging keeps working).

---

## The two automation questions this answers

### 1. Detection: can we drop the human-supplied button count?

eBayScout already learned the hard way that the **user-supplied count is
critical** to Hough segmentation (it calibrates the expected circle radius).
Every `match_log` row records both:

- `det_count_user` — crops found *with* the user's count/grid (drives the real
  pipeline, shown to the user)
- `det_count_noinput` — crops an **unguided** multi-scale Hough sweep finds with
  **no user input at all** (logged only, never used to crop)

The gap between these two columns, sliced by `det_bg_is_white`,
`det_bg_brightness`, and `det_mask_path`, tells us *when* unguided detection
already matches the human and *what* (e.g. background colour) makes it fail. That
is the roadmap to automating the count away.

We also log `det_bg_brightness`, `det_bg_is_white`, and `det_mask_path`
(`blue_only` on white/paper backgrounds vs `blue_or_white` on wood) so we can
correlate **background colour → Hough recall** directly.

### 2. Matching ("bulk slogan detection"): can the right slogan reach the front?

Today we boost accuracy by restricting the candidate pool (bank era, Football
first, only known years). None of that scales to full automation. So alongside
the **restricted** result the user sees, every crop is also scored against the
**full unrestricted universe** — all reference images and all slogans, all
years, all sports — the "shadow" pass. This reuses the already-encoded crop
vector, so it costs only a couple of extra matmuls (the crops are *not*
re-detected — we only re-rank the matches we already have).

Both `restricted_top_json` and `shadow_top_json` log the **top 10** candidates
per crop. Over time this surfaces *which reference images / slogans get
over-promoted* (consistently near the top for buttons that aren't them) and
whether the correct slogan ever reaches the front of the line — the levers for
getting the right button to #1 without human input.

When the human confirms an answer, `confirm_log` records:

- `rank_restricted` — where the confirmed year ranked in the restricted view
- `rank_shadow` — where it ranked in the **unrestricted** view
- `shadow_leaderboard_size` — how many years competed unrestricted

**If `rank_shadow` is consistently 1**, the limitations are no longer doing
real work and we can automate them away. Where it isn't, the rows show exactly
which button classes still need the guardrails.

---

## Column reference

### `match_log`
`ts, service, command, mode, job_id, thread_ts, channel_id, user_id, crop_num,
check_id, det_h, det_w, det_bg_brightness, det_bg_saturation, det_bg_is_white,
det_mask_path, det_hough_pass1, det_hough_retry, det_count_user,
det_count_noinput, det_user_count, det_detector_used, det_n_crops, bank,
restricted_top_json, shadow_enabled, shadow_top_json`

`det_bg_brightness` / `det_bg_saturation` are what the border sampler saw (HSV
mean V / S); `det_bg_is_white` and `det_mask_path` are the resulting decisions.
`restricted_top_json` / `shadow_top_json` hold the **top 10** candidates (year,
phrase, overall, image_score, text_score, type) as JSON in a single cell.

### `confirm_log`
`ts, service, command, job_id, thread_ts, crop_num, check_id, user_id,
chosen_year, chosen_phrase, chosen_type, typed_slogan, source, rank_restricted,
rank_shadow, shadow_leaderboard_size`

`chosen_phrase` is the database slogan the user confirmed; `typed_slogan` is the
raw text the user *typed* when they corrected a bad/missing match (empty for
direct picks). Comparing the two — and `rank_*` (which is empty for
Hough-missed buttons, since they never produced a leaderboard) — shows where the
matcher fell short and what humans actually type.

`source` ∈ `pick | direct | manual | other_sports | typed_search |
missed_button | skip | skip_after_type | missed_button_skip | dussellbot |
dussellbot_invoke | dussellbot_skip` — which path produced the row.

**Dussellbot (Haiku) is logged.** When the user clicks "Ask Dussellbot", a
`dussellbot_invoke` row records that Haiku was called, what it transcribed
(`typed_slogan`) and the top match it proposed — even if the user abandons. If
the user then confirms one of Haiku's suggestions, a separate `dussellbot` row
is written; if they exhaust everything, a `dussellbot_skip` row. (On `/buy`,
Dussellbot returns free text, so only a `dussellbot_invoke` row is written, with
the user's query in `typed_slogan` and Haiku's answer snippet in
`chosen_phrase`.)

---

## Per-command notes

- **`/inventory`, `/sort`** (buttonmatcher): full per-crop detection +
  restricted + shadow leaderboards; confirmation joined by
  `(channel_id, thread_ts, crop_num)`. Both modes now offer **"📝 Type slogan
  instead"** on a no-match button: the user types a slogan, the bot runs another
  matching round, and the typed text is logged (`typed_slogan`) whether they
  then pick a result (`source=typed_search`) or skip (`source=skip_after_type`).
  Hough-missed buttons reported via "Type Missed Button" log the typed text too
  (`source=missed_button` / `missed_button_skip`).
- **`/buy`** (buybot): matches the whole uploaded image (no grid/count), so
  `det_count_user = 1`, `det_count_noinput` is the unguided circle count as an
  automation baseline, and the shadow leaderboard is the full universe.
- **`/scout`** (ebayscout): the restricted result drives the lot valuation; the
  shadow pass re-runs the same crops with `restrict_years=None`, and
  `det_count_noinput` comes from a count-free segmentation sweep.

> Scale note: per-crop rows can be high-volume. We batch one append per image
> and fail open. If Sheets write-rate ever bites, the record-builders in
> `match_logging.py` can be repointed at GCS/BigQuery without touching any call
> site.
