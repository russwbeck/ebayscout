# eBay Scout — Session Handoff (2026-05-29)

Purpose: orient a fresh session fast. Read **`CLAUDE.md`** (hard constraints) and
**`ebayscout/DECISIONS.md`** (full rationale, sections #1–#27) first; this file is
the "what we did today + where it stands + what's next" layer on top.

---

## 2026-06-19 — `/crawl10` + `/crawl500` consolidated into `/crawl <N>`; two-worker Gem pipeline live

Branch `claude/buttonmatcher-gemini-ebayscout-vs0f83` (PR #32).

**`/crawl <N>` replaces both `/crawl10` and `/crawl500`.** One Slack slash command
now takes the lot count instead of two fixed-size endpoints:
- `@app.command("/crawl")` parses `N` from the command text, validates
  `1 … CRAWL_MAX_LOTS_CAP` (=`1000`; junk / out-of-range gets a usage message and
  no run), and kicks `/internal/crawl?n=N`.
- `_run_crawl(n)` (was `_run_crawl500`) runs the same on-demand2 **seen-aware**
  search (Citizens/Mellon/Central-Counties), capped at the caller's `N`; the
  first-run marker still lets the first run include already-seen lots, later runs
  feed only unseen. `_run_crawl10` and the `/crawl10`/`/crawl500` handlers +
  `/internal/crawl10`/`/internal/crawl500` routes were removed; `CRAWL10_*` config
  dropped (`CRAWL500_QUERIES/BANKS` kept as the search list).
- `/crawl 10` is the small validation run (seen-aware now, unlike the old
  repeatable `/crawl10`); `/crawl 800` (≤1000) is the big paid run.
- Operator step: register the `/crawl` slash command in Slack (Request URL =
  existing `/slack/events`); the old `/crawl10`/`/crawl500` commands can be removed.

**Two-worker Gem pipeline is live.** Run one `watcher.py` per Gemini account
(crc32 sharding by `worker_index`/`worker_count`), each with its own Chrome profile
(`user_data_dir`) pre-authenticated to that account. Full schema + the Google
"browser not secure" login workaround + the Gem JSON-validity rule (single quotes
inside slogans, never raw `"`) are in **`PIPELINE_WATCHER_CONTRACT.md`**.

Related buttonmatcher work this session: `/reference` 404-race fix (concurrency
guard + tolerant promotion) and the revert to manual review (`REF_FLOOR=2`).

---

## 2026-06-05 — buttonmatcher scoring/logging + green/auto gate + `/crawl500`

Branch `claude/ebay-scout-auto-ondemand-I8AOS`. Major convergence onto
buttonmatcher's pipeline plus a new on-demand search (full rationale: **DECISIONS
#28**).

Shipped:

1. **Exact scoring parity** with buttonmatcher (`ALPHA=BETA=0.5`, single `>0.9`
   boost, `<0.3` penalty, capped rarity tiebreaker, dual-signal year selection).
   New pure-python `scoring.py` (tiers + rarity); `clip_matcher.init()` builds the
   `word_freq` table.
2. **Green/auto-only confirmation.** A crop counts only when AUTO (`≥0.85`) or
   GREEN (`≥0.82` / gap `≥0.12`). Needed = a confirmed button with
   `amount_needed>0`. Daily tallies now `alerted / confirmed_not_needed / rejected`.
   Decision logic refactored into shared `_evaluate_listing` (used by the daily
   scan and `/crawl500`).
3. **buttonmatcher logging, per event.** `match_logging.py` copied verbatim
   (+ a 429 retry); writes `match_log`/`confirm_log` to the shared `LOGGER_ID`
   workbook as `service="ebayscout"`. One row per crop + one per auto/green
   confirmation, **flushed per image / per confirmation** so a throttle/crash
   never loses work. `image_proc.detect_and_crop(return_diag=True)` feeds the
   detection diag; `clip_matcher.match_crops_with_diagnostics` produces the
   restricted + shadow leaderboards.
4. **`/crawl500`** Slack slash command → new `/slack/events` Bolt route → kicks
   `/internal/crawl500` via `SERVICE_URL` + `X-Internal-Secret`. Fixed
   Citizens/Mellon/Central-Counties × button/pin/badge/pinback search (12
   OR-expanded queries), **cap 500**, **no seller exclusion**, first-run re-scans
   seen (`ONDEMAND2_STATE_BLOB`). Secrets reused: `SIGNING_SECRET_ES`,
   `CHANNEL_ID_EBAY`, `LOGGER_ID` (all already in Secret Manager).

Tests run in-session (no torch/slack/gcs here): `pytest ebayscout/tests/` minus
the 4 modules needing those libs → **178 passed, 3 skipped** (the skips are the
GCS-mocked `/crawl500` state tests, CI-only). New: `test_scoring.py`,
`test_match_logging.py`, `test_crawl500.py`. All modules `py_compile`-clean.
Matcher/CLIP/notifier/sheets/seen tests still need CI.

**Next / verify in prod:**
- Deploy (`cloudbuild.yaml` now sets `SERVICE_URL`); confirm `SIGNING_SECRET_ES`
  is live and point the `/crawl500` Slack command Request URL at
  `https://ebay-scout-404960106109.us-east1.run.app/slack/events`.
- Smoke: run `/crawl500`, confirm rows land in the `LOGGER_ID` workbook's
  `match_log`/`confirm_log` (tagged `ebayscout` / `/crawl500`), the summary posts,
  `ondemand2_state.json` flips after run 1, and run 2 skips seen lots.
- Watch the first daily `/run-scan` under the stricter green/auto gate (expect
  fewer, higher-precision alerts).
- **Cost:** `/crawl500` is a heavy paid run (≤500 eBay+CLIP lots; first run also
  re-scans seen) — treat like `?year_crawl=1`.

## 2026-05-30 — Backfill log analysis + chunk/dedup/audit

Analyzed the May 29–30 dry-run backfill (rev 00031, **pre-#12**) from the Cloud
Run stdout export + the 72-record daily `scan_log.jsonl`. Key findings:

- **Headline was ~20% inflated by cross-pass duplicates**: 1024 rows were only
  **814 unique listings → 405 needed** (not 469). `ignore_seen` skipped dedup.
- **15.5h runtime = a CPU-starvation cliff**: 750 listings processed in ~25 min
  at full CPU, then ~274 over 15h. `/run-scan` is synchronous but
  `gunicorn timeout=0` lets the worker outlive Cloud Run's request window; past
  it, CPU is throttled and the tail crawls. **Big backfills must be chunked.**
- **~48% of needed hits are < 0.60** (ride the title-year corroboration floor) —
  left as-is; user chose to **stay recall-biased** (no threshold change).
- **Reference over-matching**: "Penn State Pins To Win" tops crops in 17/72
  listings; 7 listings fully degenerate; some 0.00 (coverage gaps, CCB era).
- Placeholder "Slogan Unknown" was 49/405 needed hits — **#12 fixes this**; ship it.

Shipped this session (branch `claude/scan-logs-analysis-4U2Q9`):

1. **`utils.dedup_listings`** + applied in `_run_daily_scan` before the loop
   (drops cross-pass dup item_ids; logs the count).
2. **Chunk mode `/run-scan?limit=N`** — resumable, forward-only; processes ≤N
   unseen per call so each stays inside the CPU window. Returns `remaining` in
   the JSON (re-issue until 0). Must run **live** (dry-run writes no seen cursor).
   This is the unblocker for the live shopping-list backfill (next step #3).
3. **`ebayscout/tools/audit_reference_coverage.py`** — `--scan-log` (pure-Python
   attractor/degenerate/zero-score audit; runs anywhere) and `--coverage`
   (needed-vs-reference gap list; needs torch+GCS+Sheets → Cloud/CI).

Tests: `python -m pytest ebayscout/tests/test_main.py ebayscout/tests/test_audit.py -q`
→ **85 passing** in-session (added dedup + audit-core tests). Matcher/CLIP tests
still need CI.

---

## TL;DR — current state

- The bot was reframed from a (broken) **auto lot-valuer** into a recall-biased
  **"needed-button detector"**: the scan flags eBay listings that plausibly
  contain a button still needed (`amount_needed > 0` in the Google Sheet) and
  says *"review with `/scout`"*; the human values it manually.
- **Manual `/scout`** works now (was stuck in a "still loading" loop): a slash
  command wakes CLIP, uploads self-heal, the heavy analysis runs **inside an
  in-flight HTTP request** (Cloud Run throttles background threads to ~0%), and
  there's a confirm-count+era step plus a "was the era right? ✅/❌" feedback button.
- **Matching is year/era/decade-aware**: when the year is known (title, search
  query, or confirmed era) matching is restricted to that year/era → far less
  year-confusion.
- **On-demand crawls** beyond the daily scan: `?year_crawl=1` (exact needed
  years), `?era_crawl=1` (Mellon+Citizens banks), plus `?ignore_seen=1` /
  `?dry_run=1` switches. Central Counties stays in the always-on daily scan
  (rarest era).
- A full **dry-run backlog crawl finished**: **1,024 listings → 469 needed
  candidates** at threshold 0.60. Digest posted to Slack `#ebay-checker`.

## PRs (this session)

| PR | Content | State |
|----|---------|-------|
| #7  | `/scout` wake + upload self-heal; reframe scan as needed-button detector; multi-photo, top-K, title-year, scan-log groundwork | merged |
| #8  | `?ignore_seen` backfill switch + `?dry_run` + Slack preview digest | merged |
| #9  | Year-aware matching (`restrict_years`) + `?year_crawl=1` needed-year crawl | merged |
| #10 | Manual analysis moved into an in-flight request (fix ~19-min throttle); `CLAUDE.md` budget constraints; throttle-guide hardening | merged |
| #11 | `/scout` confirm count+era + era feedback buttons; era-tagged searches + `?era_crawl=1` | merged |
| **#12** | Demote "Slogan Unknown" from scan alerts; remove the 12-button cap (multi-scale Hough, sub-batched encode); `scan_log` checkpoint every 50; decade-aware restriction; **this handoff** | **open — merge to ship** |

> Always re-query the GitHub API for PR state before acting on it; don't trust
> this table blindly.

## How it works now (quick map)

- `main.py` — Flask + Slack Bolt. Routes: `/slack/events` (events, slash
  commands, interactivity), `/run-scan` (daily + crawls; params `dry_run`,
  `ignore_seen`, `year_crawl`, `era_crawl`), `/internal/manual-analysis`
  (in-flight `/scout` work), `/health`, `/test-clip`, `/ebay/account-deletion`.
- `_run_daily_scan` — fetch listings (general / year-crawl / era-crawl), per
  listing detect→match→needed-check→alert; checkpoints `seen_items.json` **and**
  `scan_log.jsonl` every 50.
- `clip_matcher.py` — CLIP ViT-B/32 (full-precision eager CPU); `match_crops_batch`
  (top_k, `restrict_years`, sub-batched); `guess_lot_era`.
- `image_proc.py` — `detect_and_crop`: scan mode = multi-scale Hough, uncapped
  (safety ceiling `MAX_CROPS_PER_PHOTO`, raised by title count); count mode
  (manual) unchanged.
- `utils.py` — pure, unit-tested helpers (`extract_years`, `extract_decades`,
  `needed_years`, `build_year_queries`, `build_era_queries`, `era_year_set`,
  `parse_confirmation`, `is_non_alerting_slogan`, `extract_lot_count`, …).
- `config.py` — all tunables. `sheets_client.py` (buy rules), `ebay_client.py`
  (Browse API), `notifier.py` (Slack), `seen_items.py` (GCS dedup + scan log).

## Hard constraints (see CLAUDE.md — do not violate)

- **Scale-to-zero. `--no-cpu-throttling` and `--min-instances=1` are OFF
  BUDGET.** Keep CPU for heavy work by running it inside an in-flight request.
- Keep `--max-instances=1` (single-container `pending_scans`).
- Never re-introduce `quantize_dynamic` on CLIP (#12 in DECISIONS — zeroed scores).
- Remote session has **no GCP access** (can't read Cloud Run logs / GCS). Slack
  `#ebay-checker` is the shared output surface. Full ML stack isn't installed —
  only pure-Python tests run in-session.
- Re-query GitHub API before stating PR state. Don't suggest deploy/merge actions
  while a scan is running (a redeploy kills the in-flight run).

## Known issues / gaps

- **Reference-data coverage gaps**: some real CCB/CCNB pins score `0.00` →
  rejected (e.g. 1983/86/87 "We Won The War", "No Bull") — those (year, slogan)
  pairs likely aren't in `vectors.pt`/`text_features`. Audit reference coverage,
  especially the rarest CCB era.
- **Cross-pass duplicates**: `all_listings` isn't deduped across the non-PSU and
  PSU passes (each dedups internally), and `ignore_seen` bypasses the loop dedup
  → some listings processed/listed twice (inflates the 1,024 / 469 counts).
  Cheap fix: dedup `all_listings` by `item_id` before processing.
- **Threshold tuning**: 469/1,024 candidates at 0.60 is high. ~51% of needed
  hits were < 0.60 (let through by title-year corroboration dropping the bar to
  0.45) — consider raising that corroboration floor (~0.55). Tune from the digest
  scores.
- **Dense-lot recall**: PR #12 uncaps + goes multi-scale, but truly tiny buttons
  in 100+ photos may still be missed; `HOUGH_RADIUS_SCALES` / `IMAGE_MAX_DIM` can
  be pushed if logs show misses.
- **`/scout` era classifier is unvalidated** — heavily logged (`ERA:` lines) so
  it can be evaluated from real use; it's an overridable suggestion only.

## Next steps (ordered)

1. **Merge #12 and let it deploy** (only after the running crawl is fully done —
   a redeploy kills in-flight runs).
2. **Slack one-time setup** (DEPLOY.md): register the `/scout` slash command and
   enable **Interactivity** (Request URL `…/slack/events`), then reinstall — the
   era feedback buttons need Interactivity.
3. **Produce the shopping-list product**: run a **live** backlog
   (`?ignore_seen=1`, no dry-run) on the new code so `scan_log.jsonl` is written
   (every 50, durable) → export a deduped, ranked CSV/sheet: score · year ·
   needed slogan(s) · asking price · seller · eBay link. (The just-finished run
   was dry-run on old code, so no `scan_log.jsonl` exists; the digest shows only
   the top 25 of 469.)
4. **Tune `NEEDED_MATCH_THRESHOLD`** from the digest/scan-log score distribution;
   consider the title-year corroboration floor.
5. **Dedup `all_listings`** by `item_id` (the cross-pass duplicate fix).
6. **Audit reference-data coverage** for missing CCB/CCNB (and other) slogans
   that score 0.00.
7. Going forward: **daily scans only** (cheap, forward-only) unless intentionally
   running another big crawl. Daily scans use the better criteria once #12 is in.

## Verifying / running (operator)

```bash
SERVICE_URL=$(gcloud run services describe ebay-scout --region=us-east1 --format='value(status.url)')
TOKEN="Authorization: Bearer $(gcloud auth print-identity-token)"

# Preview a crawl (posts a Slack digest; with #12, also persists scan_log every 50)
curl -X POST "${SERVICE_URL}/run-scan?year_crawl=1&dry_run=1&ignore_seen=1" -H "$TOKEN"
# Live backlog for the shopping list (writes scan_log.jsonl)
curl -X POST "${SERVICE_URL}/run-scan?ignore_seen=1" -H "$TOKEN"            # daily-set live backfill
curl -X POST "${SERVICE_URL}/run-scan?year_crawl=1&ignore_seen=1" -H "$TOKEN"

# Read progress/needed-year facts (read-only; safe during a run)
gcloud logging read 'resource.type=cloud_run_revision AND resource.labels.service_name="ebay-scout" AND (textPayload:"Year crawl over" OR textPayload:"YEAR-CRAWL" OR textPayload:"scan complete")' --freshness=2d --order=asc --format='value(timestamp,textPayload)'
```

Pure tests (run in-session): `cd ebayscout && python -m pytest tests/test_main.py -q`
(74 passing). Matcher/notifier/clip tests need the full stack (CI).
