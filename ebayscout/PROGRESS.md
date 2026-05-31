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
| 1 | Advanced detection + match logging in buttonmatcher | ✅ merged (PR #22) |
| 2 | Same logging in `/buy` (buybot) and `/scout` (ebayscout) | ✅ in PR |
| — | **Verify writes to the `LOGGER_ID` spreadsheet** | ⚠️ first live run wrote NOTHING — diagnosing (see below) |
| 3 | Merge buybot (`/buy`, `/suggest`) into buttonmatcher | ⬜ blocked on log verification |
| 4 | Move `/scout` into buttonmatcher; decommission standalone | ⬜ not started |

## Round 2 — logging wrote nothing (diagnosis + fix)

First live `/sort` run (2026-05-31): the new code ran (the unguided-count log
line appeared) but **no `>>> MATCH_LOG:` line appeared at all** — meaning the
logger was disabled at startup (`open_log_sheets` threw and was swallowed) and
then skipped silently on every write.

Almost certainly the new logging spreadsheet is **not shared with the bot's
service-account email** (gspread `open_by_key` raises on an inaccessible sheet),
or `LOGGER_ID` holds a full URL / trailing newline instead of the bare key.

Fix (branch `claude/logging-diagnostics-fix`, NOT the merged PR branch):
- `open_log_sheets` now prints a loud, actionable line on success AND failure
  (names the workbook, the key prefix, and "share with the service account").
- `LOGGER_ID` may now be a full Sheets **URL or** a bare key — the key is
  extracted (`_extract_spreadsheet_key`), and whitespace/newlines are trimmed.
- `SheetLogger` warns ONCE when a write is skipped because logging is disabled,
  and prints a one-time "✅ first match write OK" when it succeeds — so silence
  is no longer ambiguous.
- `init_sheets`/`startup` now print the **service-account email** so it's
  obvious which address to share the logging sheet with (Editor).

### Action for the operator
1. Deploy this branch.
2. Read startup logs for `service account = …@….iam.gserviceaccount.com`.
3. Share the `LOGGER_ID` spreadsheet with that email as **Editor**.
4. Confirm startup prints `MATCH_LOG: opened logging workbook '<name>' …`.
5. `GET /internal/logtest`, or run a real `/sort`, and confirm rows appear.

## Round 3 — capture user-typed slogans (on the diagnostics branch)

Added to PR #23 (branch `claude/logging-diagnostics-fix`):
- **`/sort` no-match buttons** now include **"📝 Type slogan instead"** (was
  Skip-only). Typing a slogan runs another matching round, mirroring
  `/inventory`; `mode` is threaded through `type_slogan_first` →
  `first_type_modal` so a `/sort` typed search confirms back into the sort
  section and falls back to Skip (not Dussellbot).
- **New `typed_slogan` column** in `confirm_log` records the raw text the user
  typed, kept separate from the DB slogan they confirmed.
- Typed text is now logged on every typed path: typed-search picks
  (`typed_search`), missed-button picks (`missed_button`), and both skips after
  typing (`skip_after_type`, `missed_button_skip`). Hough-missed buttons log
  `rank_*` as empty (no leaderboard existed), which is itself a useful signal.
- Shared `match_logging.py` stays byte-identical across all three bots; 27
  unit tests pass.

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

## Round 4 — log Dussellbot (Haiku) interactions (diagnostics branch)

Dussellbot was previously invisible to the sheet. Now logged (no new Dussellbot
functionality added — only logging):
- **buttonmatcher `/inventory`**: when Haiku is invoked, a `dussellbot_invoke`
  row records it ran, what it transcribed (`typed_slogan`, with year+confidence),
  and the top match it proposed. Confirming a Haiku suggestion writes a separate
  `dussellbot` row; exhausting all options writes `dussellbot_skip`.
- **buybot `/buy`**: Haiku returns free text, so one `dussellbot_invoke` row per
  call — user's query in `typed_slogan`, Haiku's answer snippet in
  `chosen_phrase`.
- Shared `match_logging.py` unchanged (main.py-only edits); still byte-identical
  across all bots; 26 tests pass; both mains compile.

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
