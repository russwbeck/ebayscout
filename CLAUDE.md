# eBay Scout — project background for Claude

Flask + Slack Bolt service on Google Cloud Run (`ebayscout/main.py`). It (1) runs
a daily eBay/Etsy scan that flags listings likely to contain a **needed** Penn
State gameday button (`amount_needed > 0`) for human review, and (2) serves a
manual `/scout` mode where a user uploads a photo and gets a CLIP-based lot
valuation. Full design history and rationale live in `ebayscout/DECISIONS.md` —
read it before changing deploy/gunicorn/CPU behavior. For the latest status and
next steps, start with `ebayscout/HANDOFF.md`.

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
  `seen_items.json`, `scan_log.jsonl`, `hunt_ids.json`) explicitly.
- **Before telling the user to run repo code, confirm it's on the branch they'll
  actually run** (usually `main` via a fresh clone). Tooling stranded on an
  unmerged branch won't exist in their checkout.
- **Don't sprawl.** Do the one thing asked, cleanly; don't generate extra tools
  or files that muddy what to do next.
