# Manual crawl trigger — reference

Verified invocation for a manual, seen-ignoring pipeline run against the live
Cloud Run service. Run from **Google Cloud Console → Cloud Shell**.

> **Cost:** `?ignore_seen=1` feeds every fetched lot (bypasses seen-dedup) into
> the Gemini→GCS pipeline — real eBay-API + Gemini + CPU spend. Always run the
> **dry-run preview first** to see how many lots would feed. The passive daily
> feed collects the same detection telemetry at zero marginal cost
> (`AUTOMATION_ROADMAP.md`, "Data-collection cheat sheet"); use this only when
> you want fresh data *now*.

```bash
# 1. Service URL (exact service name is ebay-scout; region us-east1)
URL=$(gcloud run services describe ebay-scout --region=us-east1 --format='value(status.url)')

# 2. Auth token (Cloud Run requires an identity token)
TOK=$(gcloud auth print-identity-token)

# 3. PREVIEW — posts nothing, writes nothing; reports how many lots WOULD feed
curl -sS -X POST -H "Authorization: Bearer $TOK" \
  "$URL/run-scan?ignore_seen=1&limit=200&dry_run=1"

# 4. REAL RUN — feeds up to 200 lots into the pipeline
curl -sS -X POST -H "Authorization: Bearer $TOK" \
  "$URL/run-scan?ignore_seen=1&limit=200"
```

## What this actually runs

- Hits `/run-scan` (`main.py`) → with `DAILY_PIPELINE_FEED=1` (the default) this
  calls `_run_crawl(n, source="daily", ignore_seen=True)`. It **fetches +
  enqueues** each lot's primary photo to the async Gemini→GCS pipeline and
  returns the count fed; detection/Gemini/CLIP happen in the pipeline workers,
  which write the `match_log` rows (the `ni_*` unguided telemetry graded in
  `log_analysis.md` / `tested_hypothesis.md`).
- **Search caveat:** `/run-scan` uses the **daily** search
  (`EBAY_SEARCH_QUERIES`), *not* the on-demand `/crawl` CRAWL500 search. The
  `/internal/crawl` endpoint runs CRAWL500 but has **no `ignore_seen`** param
  today, so this daily-search path is the only ignore-seen route. For Layer-1
  detection telemetry the two searches are equivalent.

## Params (`/run-scan`, one-shot; Cloud Scheduler sends none of these)

| Param | Effect |
|---|---|
| `ignore_seen=1` | process every fetched lot regardless of `seen_items` (still checkpoints seen, so forward-only after) |
| `limit=N` | feed at most N lots this call (`N=200` here); omit → `DAILY_PIPELINE_N` (1000) |
| `dry_run=1` | report the count only — post nothing, write nothing (preview) |

## After the run

- Watch **#ebay-checker** in Slack for pipeline progress + the summary (the
  container has no external console output otherwise).
- Export the Logger (`match_log`) and grade: `gate=auto` should fire **only** on
  `ni_scale_path=scale_first`; measure `auto`+`scale_first` agreement vs
  `gemini_button_count` (Phase-5 / Stage-B entry gate, needs ≥98%) and the
  `scale_first` share of volume. See `AUTOMATION_ROADMAP.md` Phase 5.
