# eBay Scout — project background for Claude

Flask + Slack Bolt service on Google Cloud Run (`ebayscout/main.py`). It (1) runs
a daily scan that feeds the day's unseen eBay lots through the two-worker
Gemini→GCS pipeline (see `ebayscout/PIPELINE_WATCHER_CONTRACT.md`) and posts
**deals only** to `#ebay-checker` — a lot containing a **needed** Penn State
gameday button (`amount_needed > 0`), or one worth more than its asking price —
and (2) runs `/crawl <N>`, the same pipeline on demand over an on-demand,
seen-aware eBay search of up to N (≤1000) lots.

There is **no human review lane here and no manual upload**: `/scout` was
removed in PR #17 (2026-06-03) and lives in buttonmatcher. Nothing in this
service asks a person about a crop, which is why a look-alike slogan flags the
alert rather than demoting the match (`pipeline_classify.lookalike_note`).

The legacy CLIP-only scan (`_run_daily_scan` / `_evaluate_listing`) is FROZEN,
not live: reachable only via `?year_crawl` / `?era_crawl` / `?hunt_ids` and
`DAILY_PIPELINE_FEED=0`. See DECISIONS.md #33.

Full design history and rationale live in `ebayscout/DECISIONS.md` — read it
before changing deploy/gunicorn/CPU behavior. For the latest status and next
steps, start with `ebayscout/HANDOFF.md`; the standing plan is
`STRATEGIC_REVIEW_2026-09.md`.

## Hard constraints (do not violate)

- **Budget: stay scale-to-zero. `--no-cpu-throttling` is OFF BUDGET — never
  propose or add it.** Likewise do **not** add `--min-instances=1` (an always-on
  warm instance is also off budget). The user has stated this explicitly.
- **Keep CPU for heavy work by running it inside an in-flight HTTP request**
  (the `/run-scan` pattern), not via infra flags. Cloud Run throttles CPU to ~0%
  between requests; a background thread relying only on the `_keep_cpu_hot`
  spinner gets starved (this is the cause of slow manual analysis). The proven
  fix is to do the work synchronously inside a live request, mirroring the
  `buttonmatcher` worker's `/internal/match` endpoint.
- **Keep `--max-instances=1`** — manual `pending_scans` state lives in one
  container's memory (DECISIONS.md #17).
- **Do not re-introduce `torch.quantization.quantize_dynamic`** on CLIP — it
  shifts the output space and collapsed all scores to ~0 (DECISIONS.md #12).

## Process expectations

- **buybot is decommissioned (2026-07-05).** Shared files (`detect.py`/
  `detect_pipeline.py`, `detect_gate.py`, `match_logging.py`, `sheet_retry.py`,
  `confusable_slogans.py`, `pipeline_ingest.py`, `gemini_geometry.py`,
  `gemini_resolve.py`, the shared docs) sync across buttonmatcher + ebayscout
  only; ignore older docs that name buybot as a third sync target.
  `sheet_retry.py` joined the set on 2026-09-09, when `match_logging` started
  retrying the Sheets write quota through it; `match_logging.py` imports it
  relative-first, plain-second so the one file works in ebayscout's package
  layout and buttonmatcher's flat one. `confusable_slogans.py` joined on
  2026-09-10 — the curated look-alike groups are one list for both services,
  but the two USE it differently: buttonmatcher demotes an auto-confirm to a
  human picker, ebayscout has no human lane and instead flags the deal alert
  (see `pipeline_classify.lookalike_note`). The Gemini-pipeline trio was
  ALREADY shared in practice, and `STRATEGIC_REVIEW_2026-09.md` says so — it was
  just missing from THIS list, which is the one a session reads first. Noticed
  2026-09-18 while fixing the per-axis coordinate-scale bug, which was therefore
  live in BOTH pipelines. `pipeline_ingest.py` and
  `gemini_geometry.py` are byte-identical; `gemini_resolve.py` differs by
  exactly one hunk — the `edition_twins` import, wrapped flat-first /
  package-second the same way `match_logging.py` wraps `sheet_retry` — so port
  changes to it by hand and leave that hunk alone. `tested_hypothesis.md` is
  byte-shared too. `diff` before and after touching any of them.
- **`rerank.py` weights are calibrated, not guessed.** `YEAR_WEIGHT` and
  `SID_WEIGHT` must stay equal across both repos —
  `tests/test_buttonmatcher_parity.py` pins them. Raising them needs a fresh
  `tools/calibrate_from_logs.py rerank` replay, in both repos, in one PR.
- Develop on the designated feature branch; commit + push; open a PR only when
  asked. **Always re-query the GitHub API for PR state before reporting it** —
  never assert merged/mergeable from memory.
- The remote container is ephemeral and has **no GCP access** (can't read Cloud
  Run logs or GCS). Slack (`#ebay-checker`) is the one place to surface output
  that both the running service and Claude can see.
- The full Python stack (torch/clip/flask/google-cloud) is **not installed** in
  web sessions — only pure-Python tests (e.g. `utils`) run here; matcher/notifier
  tests need CI. State honestly what was and wasn't actually run.

## Process discipline (hard-learned — these cost the user CPU $$ and trust)

Real mistakes from a prior session. Do not repeat them:

- **Never commit to a branch after its PR has merged.** A merged PR is frozen at
  its merge commit. Once a PR merges, cut a NEW branch for further work — do not
  pile more commits onto the merged branch, and never tell the user a commit
  "landed in" a PR. (This happened: commits were pushed onto an
  already-merged branch and falsely described as part of the merged PR.)
- **Re-query GitHub before stating ANY PR/commit fact** — merged or not, which
  commits it contains, what's on `main` vs the branch. From the API, never from
  memory. (The rule above already existed and was still violated — treat it as
  non-negotiable.)
- **Flag cost and scope before suggesting any command that triggers heavy
  compute.** `?year_crawl=1`, `?ignore_seen=1`, and the ID hunt launch large
  eBay-API + CPU runs that cost real money. Lead with the *minimal* command for
  the user's stated goal; state exactly what a flag does and its cost before
  offering it. (A suggested `?year_crawl=1` kicked off a full unwanted crawl.)
- **Be precise about what each file/artifact IS.** Never hand the user a
  throwaway analysis snapshot as if it were the live data path. Name the
  canonical source of truth (the GCS blobs the service reads/writes:
  `seen_items.json`, `scan_log/YYYY-MM.jsonl`, `hunt_ids.json`) explicitly.
- **Before telling the user to run repo code, confirm it's on the branch they'll
  actually run** (usually `main` via a fresh clone). Tooling stranded on an
  unmerged branch won't exist in their checkout.
- **Don't sprawl.** Do the one thing asked, cleanly; don't generate extra tools
  or files that muddy what to do next.
