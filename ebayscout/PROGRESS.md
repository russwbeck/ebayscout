# Consolidation & Automation-Logging — Progress

Tracking the multi-repo effort to (1) instrument the button-matching pipeline
for automation, then (2) merge buybot and ebayscout's `/scout` into buttonmatcher.

Branch (all repos): `claude/buttonmatcher-logging-consolidation-QQM8N`

## The end goal

Automate button identification so the human does less over time. We do **not**
change the user interaction in buttonmatcher. In the background we collect the
data needed to (a) drop the human-supplied button count, and (b) get the correct
slogan to #1 without the era/sport/year limitations.

## Status

| Step | Description | State |
|---|---|---|
| 1 | Advanced detection + match logging in buttonmatcher | ✅ code complete, in PR |
| 2 | Same logging in `/buy` (buybot) and `/scout` (ebayscout) | ✅ code complete, in PR |
| — | **Verify writes to the `LOGGER_ID` spreadsheet** | ⏳ pending live `/internal/logtest` |
| 3 | Merge buybot (`/buy`, `/suggest`) into buttonmatcher | ⬜ not started (awaiting log verification) |
| 4 | Move `/scout` into buttonmatcher; decommission standalone | ⬜ not started |

eBay crawling/scan code in ebayscout is **left fully intact** — paused, not
deleted, per direction.

## What the logging captures (background only; UX unchanged)

- **Unguided Hough count** — how many circles detection would see with *no user
  input*. Count only; the circles are **not** analyzed (that would waste
  compute). Logged next to the user-guided count to measure the gap.
- **What the background sampler saw** — `det_bg_brightness`, `det_bg_saturation`,
  `det_bg_is_white`, `det_mask_path` — to correlate background → Hough recall.
- **Bulk slogan detection (top 10)** — the user-guided + detected crops are
  scored across *all* reference images and slogans; the top 10 (restricted and
  unrestricted) are logged, plus the user-selected answer's rank, to learn what
  gets over-promoted and whether the correct slogan reaches #1.

See `LOGGING.md` for the schema, the `LOGGER_ID` setup, and the
`/internal/logtest` verification step.

## Verification checklist (before Step 3)

1. Deploy this branch to buttonmatcher.
2. `GET /internal/logtest` → confirm one row in `match_log` + one in
   `confirm_log` (command=`/logtest`) in the `LOGGER_ID` workbook. Delete them.
3. Run one real `/inventory` and one real `/sort`; confirm per-crop rows, and a
   `confirm_log` row after you confirm a button. Sanity-check
   `det_count_user` vs `det_count_noinput` and `rank_shadow`.
4. Repeat the live check for `/buy` (buybot) and `/scout` (ebayscout) once their
   PRs are merged/deployed.
5. Only then start Step 3.

## Notes / things to watch

- All log writes are **fail-open**: a logging error is swallowed, never breaks
  the bot. If `LOGGER_ID` is missing or unshared, logging silently disables.
- Toggle the heavy parts off with `BUTTONMATCHER_SHADOW_PASS=0`.
- buybot now applies a rarity tiebreaker it didn't have before (so its
  leaderboard scores match buttonmatcher) — watch the first live `/buy`.
- ebayscout `/scout` logs the single best restricted/shadow match per crop;
  full top-10 there would require exposing a ranked list from `clip_matcher`
  (deferred — larger change).
- ML/Slack/GCP could not be run in the dev container; wiring is verified by
  `py_compile` + 23 pure-logic unit tests, not a live run.
