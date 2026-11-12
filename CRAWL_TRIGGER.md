# Manual crawl trigger — reference

Invocation for a manual, seen-ignoring hand-off run against the live
Cloud Run service. Run from **Google Cloud Console → Cloud Shell**. The auth
line changed on 2026-09-26 (the security pass); it matches DEPLOY.md's and
has not been re-run live from this doc since.

> **Cost:** `?ignore_seen=1` hands every fetched lot (bypasses seen-dedup) to
> buttonmatcher, which runs each as a Scout lot in `#inventory-bot` — real
> eBay-API + CPU spend here and a count prompt per lot for the operator. (The
> Gemini→GCS pipeline this once fed was retired 2026-10-25.) Always run the
> **dry-run preview first** to see how many lots would feed. The passive daily
> feed collects the same detection telemetry at zero marginal cost
> (`AUTOMATION_ROADMAP.md`, "Data-collection cheat sheet"); use this only when
> you want fresh data *now*.

```bash
# 1. Service URL (exact service name is ebay-scout; region us-east1)
URL=$(gcloud run services describe ebay-scout --region=us-east1 --format='value(status.url)')

# 2. Auth: the watcher's shared secret.  /run-scan accepts only Cloud
#    Scheduler's token or this header since 2026-09-26; a personal
#    `gcloud auth print-identity-token` is refused (403).  See DEPLOY.md.
TOK="X-Pipeline-Secret: $(gcloud secrets versions access latest --secret=PIPELINE_SHARED_SECRET)"

# 3. PREVIEW — posts nothing, writes nothing; reports how many lots WOULD be handed over
curl -sS -X POST -H "$TOK" \
  "$URL/run-scan?ignore_seen=1&limit=200&dry_run=1"

# 4. REAL RUN — hands up to 200 lots to buttonmatcher
curl -sS -X POST -H "$TOK" \
  "$URL/run-scan?ignore_seen=1&limit=200"
```

## What this actually runs

- Hits `/run-scan` (`main.py`) → with `DAILY_PIPELINE_FEED=1` (the default) this
  calls `_run_crawl(n, source="daily", ignore_seen=True)`. It **fetches** each
  lot, skips relists (`relists.py`), and **POSTs** it to buttonmatcher's
  `/internal/ebay_scout_lot` (`yolo_test.py`), which does the detection, CLIP,
  OCR and `match_log` rows in its own request.
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

- Watch **#ebay-checker** in Slack for the day's summary ("handed N/N lot(s)
  to buttonmatcher") and **#inventory-bot** for the lots themselves.
- The grading text that follows is from the Gemini pipeline (retired
  2026-10-25: `ni_scale_path`, `gemini_button_count` and the Phase-5 / Stage-B
  gate no longer get new rows). Kept as it was written:

- Watch **#ebay-checker** in Slack for pipeline progress + the summary (the
  container has no external console output otherwise).
- Export the Logger (`match_log`) and grade: `gate=auto` should fire **only** on
  `ni_scale_path=scale_first`; measure `auto`+`scale_first` agreement vs
  `gemini_button_count` (Phase-5 / Stage-B entry gate, needs ≥98%) and the
  `scale_first` share of volume. See `AUTOMATION_ROADMAP.md` Phase 5.
