# eBay Scout (ebayscout)

Flask + Slack Bolt service on Google Cloud Run (service `ebay-scout`, us-east1,
`ebayscout/main.py`). Every day it searches eBay for Penn State gameday pinback
button lots, skips the ones it has seen and the ones that are relists, and hands
every other lot to buttonmatcher, which runs it as an automatic Scout lot in
`#inventory-bot` and values it against the asking price. `/crawl <N>` does the same
on demand. It records each lot it hands over in the Logger's `price_log` tab.
Scale-to-zero, `--max-instances=1`.

**Gemini is retired (2026-10-25).** The Gemini→GCS pipeline this service once fed
is gone; the learned detector, CLIP and OCR in buttonmatcher are the whole process.
The older CLIP-only scan is **frozen** (reachable only with `DAILY_PIPELINE_FEED=0`
or `?year_crawl` / `?era_crawl` / `?hunt_ids`; `ebayscout/DECISIONS.md` #33).
Current state: the top of `ebayscout/HANDOFF.md`.

## Entry points

- **Daily scan:** Cloud Scheduler `POST /run-scan` at 9 AM ET, authenticated by its
  OIDC token (or the operator's `X-Pipeline-Secret` header).
- **Slash command** `/crawl` (Slack): `/crawl <N>` (1-1000 lots),
  `/crawl seller <username>` (one seller's listings into `price_log`),
  `/crawl shipping` (fill shipping on existing rows), `/crawl purchases [days]` (the
  operator's own eBay purchases), `/crawl titles` (look up the titles of lots
  already handed over; lookups only).
- **Handoff:** each lot is `POST`ed to buttonmatcher's `/internal/ebay_scout_lot`;
  the contract is in `REFERENCE.md`.
- Also `GET /health`, `GET /test-clip` (secret-protected debug) and eBay's
  `/ebay/account-deletion` compliance endpoint. Routes: `ebayscout/HANDOFF.md`.

## Docs

| File | What |
|---|---|
| `ebayscout/HANDOFF.md` | **Start here.** "Where things stand" at the top, then the session log, newest first |
| `CLAUDE.md` | Hard constraints and process rules for anyone (or any Claude session) changing this repo |
| `REFERENCE.md` | Glossary, the contract with buttonmatcher, module map, switches, bucket layout, tools, ID key |
| `ebayscout/DECISIONS.md` | The numbered design decisions (#1-#34) and their rationale |
| `ebayscout/DEPLOY.md` | Deploying: secrets, service account, Scheduler, Cloud Build (parts for the removed manual-upload flow and the frozen scan are marked) |
| `CRAWL_TRIGGER.md` | The manual `ignore_seen` hand-off run against the live service, with its cost |
| `ebayscout/SEARCH_TERMS_AUTO_VS_CRAWL.md` | Why the daily and `/crawl` search terms are kept separate |
| `ebayscout/EBAY_BULK_LISTING.md` | What worked in the 2026-10-03 bulk listing upload (the operator's own sales listings) |
| `AUTOMATION_ROADMAP.md` / `AUTOMATION_VISION.md` / `STRATEGIC_REVIEW_2026-09.md` / `MANAGEMENT_BRIEF.md` | The automation plan, its rationale and the September review (shared with buttonmatcher) |
| `LOGGER_FRONTS.md` / `tested_hypothesis.md` / `REFERENCE_SCORING_REVIEW.md` | The Logger front register, settled verdicts and the reference-scoring review (shared) |
| `docs/archive/` | Finished reviews and the retired Gemini-pipeline docs |

The sibling repo is `buttonmatcher` (the Slack bot that does the identifying); its
`HANDOFF.md` tracks the matching work.

## Tests

CI (`.github/workflows/tests.yml`) runs `python -m pytest ebayscout/tests` with
numpy, OpenCV and Pillow at production's pins, ignoring `test_clip_matcher.py` and
`test_seen_items.py` (they need torch and google-cloud), plus pyflakes via
`test_no_unbound_names.py`. Locally the same command, or one suite with
`python ebayscout/tests/run_<name>_tests.py`. The torch/CLIP/Slack/GCP path runs
only on the live service.
