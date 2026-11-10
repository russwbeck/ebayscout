# ebayscout — reference

Lookup tables for people (and Claude sessions) new to the repo. Built 2026-11-10 by reading
the code; where this page and the code disagree, the code wins. Current state of the service is
"Where things stand" at the top of `ebayscout/HANDOFF.md`. buttonmatcher's `REFERENCE.md` has the
longer tables both services share (the `confirm_log` source values, the `price_log` columns,
its switches, the tools that grade the matcher); this page points there instead of copying
them, except the contract, which is the same text in both.

Contents: [Glossary](#glossary) · [Shared schemas](#shared-schemas) ·
[The contract](#the-contract-between-the-two-services) · [Module map](#module-map) ·
[Switches](#switches) · [Bucket layout](#bucket-layout) · [Live configuration](#live-configuration) ·
[Tools](#tools) · [ID key](#key-to-the-id-systems)

## Glossary

| Term | Meaning |
|---|---|
| **lot** | One photo of buttons that a person (or ebayscout) hands the bot, with its count, mode and Slack thread. One `job_id` in the logs. |
| **crop** | One button cut from a lot's photo (`crop_num` is its number in the lot). Everything is matched, logged and confirmed per crop. |
| **card** | The Slack review message for one crop: the crop's picture, the top candidate slogans to tap, and Skip / type-it options. A crop an auto rule confirms gets no card. |
| **picker** | A card that asks the operator to choose among candidates. Both pickers are built by `_edition_picker_blocks`. The *edition picker* chooses among the editions of one twin slogan (a tap logs `edition_pick`); the *slogan picker* chooses among the different slogans of a look-alike group, where an auto-confirm was demoted by the look-alike guard (a tap logs `confusable_pick`). The typed-search result list is a separate card (`typed_search`).
| **edition twin** | One slogan printed on more than one catalog button, differing in year, sport or bank (e.g. *Plaster Pitt* 1973/79). CLIP cannot tell them apart from the slogan, so `edition_twins.py` flags them. A *year-only family* is a twin that differs only in the printed year. |
| **look-alike (confusable)** | Different slogans that look or read alike (`confusable_slogans.py`, curated groups), so a score cannot choose between them. buttonmatcher demotes an auto-confirm in a group to a human card; ebayscout only flags the deal alert. |
| **copycat** | In the catalog, every button that is not a bank-issued gameday button: the predecessor and copycat series (Student Bookstore, Rah Rah, Adlanco …), each non-bank bowl game (one subcategory per game, e.g. "1987 Fiesta Bowl"; a bank-issued bowl button stays under its bank), the blood-drive buttons and the merchant sets (Lions Pride, Downtown Merchants) (`button_category.py`). The catalog files every button as **Bank** (the Central Counties / Mellon / Citizens program) or **Copycats**, with a subcategory. The count form's *Category* is All / Bank (default) / Copycats; "Bank / Copycats category" in the docs means this filter. |
| **shadow** | A rule or measurement that runs and is logged on every crop, but changes nothing the operator sees. Shadows are graded offline against the operator's answers and promoted only if they earn it. |
| **front** | One question the Logger is measuring, with a status, a gate and a standing reading: an entry in `LOGGER_FRONTS.md` (A1, B31, C4 …). |
| **audit sample** | One lot in ten (under 25 buttons) whose auto-confirms are *held* and shown as ordinary cards, then graded against what the operator answers (`audit_shadow` → `audit_hit` / `audit_miss` rows). Since 2026-11-10 (PR #282) a sampled lot holds an auto only when a *new rule* made it (the edition ladder named the edition, or the visual veto passed its own best-photo candidate); every other auto on the lot is written (`_audit_holds`). It measures the precision of those new rules without selection bias; `BUTTONMATCHER_AUDIT_NEW_ONLY=0` holds every auto again. |
| **auto rule** | A condition under which a crop is written with no click: score/gap, gap-only, slogan-gap, `auto_ocr`, `auto_ocr_led`. Every auto passes the gate (`auto_gate.py`) first. |
| **the gate** | `auto_gate.py`: the one place every auto-confirm is checked (removal lot, edition twin, look-alike, visual veto, grid fill). |
| **the detector** | The learned YOLO model (`learned_detector.py`, weights at `models/detector/button_yolo.onnx`) that finds the buttons in a photo. The Hough circle detector (`detect.py`) is the fallback when the model counts none. |
| **grid fill** | A weak box the detector adds where the other boxes' lattice says a button should be but none was found; its crop always gets a card. |
| **pre-match** | Matching a lot's crops in the upload's spare time, before the operator finishes the count form, so the cards are ready (`_prematch_upload`). |
| **handoff** | ebayscout sending a lot to buttonmatcher (`/internal/ebay_scout_lot`), which runs it as a Scout lot. Also the `handoff` row ebayscout writes to `price_log` for each one. |
| **relist** | An eBay lot a seller listed again under a new item number. ebayscout recognises it (same photo, or same seller and title) and does not hand it over again (`relists.py`). |
| **Scout lot** | A lot in `scout` mode: sorted and valued against an asking price (`buy_rules.py`, `SCOUT.md`). |
| **board / leaderboard** | The ranked candidate list for a crop: `restricted_top_json` (matched under the lot's filters) and `shadow_top_json` (unrestricted), the top 10 each. |
| **bank** | Three meanings. (a) The *issuing bank* of a button: Central Counties, Mellon or Citizens (`button_category.BANKS`); the **Bank** category is the bank-issued gameday program. (b) The `bank` column of `match_log`, which records the lot's filter, e.g. `all;category=Copycats` (`_logged_bank`). (c) The CLIP vector banks: `vectors.pt` (reference photos) and `text_features.pt` (slogan text), with their `.npz` twins (`bank_cache.py`). |
| **SloganID / entry_id** | The catalog key of one button (`text_db.json`); reference photos are named for it. |
| **reference photos** | The labelled photos of each catalog button that crops are compared with (`reference/sloganID/<id>.<n>.jpg`). |

## Shared schemas

- **`confirm_log` `source` values** (what a row's answer means, and which are a human's):
  buttonmatcher's `REFERENCE.md`. The daily feed and `/crawl` write no confirmations here:
  buttonmatcher writes them when the operator answers in `#inventory-bot`.
- **`price_log`** (columns A to V and the `source` values `scan`, `scout`, `scout_sold`,
  `seller`, `handoff`, `relist`): buttonmatcher's `REFERENCE.md`; the module is the shared
  `price_log.py`. This service writes `handoff` (every lot handed over), `relist`, and
  `seller` (`/crawl seller`) rows; the frozen CLIP scan writes `scan` rows.
- **`match_log` columns and which are still written:** buttonmatcher's `LOGGING.md`.

## The contract between the two services

*Same text in both repos' `REFERENCE.md`; checked against `ebayscout/yolo_test.py`,
`ebayscout/main.py` (`_handoff_lot_to_buttonmatcher`) and buttonmatcher's
`ebay_handoff.py` and `main.py` (`internal_ebay_scout_lot`) on 2026-11-10.*

**One call goes from ebayscout to buttonmatcher:** `POST <BUTTONMATCHER_URL>/internal/ebay_scout_lot`
(`BUTTONMATCHER_URL` defaults to buttonmatcher's Cloud Run URL, `yolo_test.DEFAULT_BUTTONMATCHER_URL`).
Timeout 90 s on ebayscout's side.

- **Auth:** header `X-Pipeline-Secret`, equal to the `PIPELINE_SHARED_SECRET` both services
  hold (the name is the retired Gemini watcher's). Wrong or missing: `403 Forbidden`.
- **Body (JSON):**

  | Field | Meaning |
  |---|---|
  | `image_url` | The lot's first photo. Required; must be `https://i.ebayimg.com/...` (`ebay_handoff.image_url_ok`), else `400 bad image_url` |
  | `item_id` | eBay item id (Browse form `v1|<number>|0`, or the number) |
  | `title`, `url` | Listing title and link, shown above the photo in `#inventory-bot` |
  | `asking`, `shipping` | Asking price and shipping (shipping may be null) |
  | `seller`, `condition` | The seller's eBay username and the listing's condition (since 2026-11-08); they ride into the finished Scout rows |
  | `buying_options`, `bid_count` | Let `price_log` keep auctions out of the averages |

- **What buttonmatcher does:** downloads the photo (≤ 25 MB, ≤ 3 redirects), posts it with the
  listing to `#inventory-bot`, starts the pre-match, and posts the count prompt, as an upload in
  Scout mode would. It then runs the learned detector, CLIP and OCR, asks about what it cannot
  settle, and values the lot against `asking`.
- **Returns:** `200` with `{"job_id": "<uuid>"}` once the lot is posted. `job_id` is the key of that
  lot's `match_log` and `confirm_log` rows. Errors: `400` (bad image URL, image over 25 MB),
  `403`, `502` (photo download or Slack post failed), `503` (no channel configured).
- **What ebayscout does with the answer:** only a `200` marks the item seen (so a refused lot comes
  back on the next run); it writes a `handoff` row to `price_log` with `run_id`
  `handoff-<daily|crawl>-<job_id>`.

**`/internal/ebay_lot_label` is not part of the contract.** It is buttonmatcher's own call to
itself (the ⏭️ "Button, skip" and 🚫 "Not a button" answers on an eBay lot's count prompt), takes the
per-process `X-Internal-Secret`, and ebayscout never calls it.

**Nothing goes the other way.** buttonmatcher does not call ebayscout. The two meet in the Logger
workbook: `price_log` (ebayscout writes `seller`, `handoff` and `relist` rows, and `scan` rows
only from its frozen CLIP scan; buttonmatcher writes the `scan` rows of a handed-over lot once the
operator finishes it, `scout` rows for other Scout lots, and `scout_sold` rows) and `match_log` /
`confirm_log`.

## Module map

Files in `ebayscout/ebayscout/` (flat modules inside the package). "Shared" files are
byte-identical in buttonmatcher (`CLAUDE.md`).

| File | What it is |
|---|---|
| `main.py` | Flask + Slack Bolt service: `/run-scan`, `/crawl` and its internal routes, the handoff loop (`_run_crawl`, `_handoff_lot_to_buttonmatcher`), startup, and the frozen CLIP scan |
| `gunicorn.conf.py` | Gunicorn settings: one worker, 8 threads, no timeout, `preload_app = True`, CLIP hydration after fork |
| `yolo_test.py` | The handoff to buttonmatcher: the endpoint, the payload (`handoff_payload`) and the `price_log` handoff record (named for the YOLO week, 2026-10-15) |
| `relists.py` | Recognises a relisted lot (same photo, or same seller and title) so it is not handed over again |
| `config.py` | All tuneable constants: search terms, excluded sellers and keywords, bucket blob names, caps (`CRAWL_MAX_LOTS_CAP`, `DAILY_PIPELINE_N`, `DAILY_HUNT_BUDGET`) |
| `ebay_client.py` | eBay Browse API: OAuth app token, searches, item pictures |
| `etsy_client.py` | Etsy API v3 listing search (used only by the frozen scan) |
| `seen_items.py` | The GCS-backed dedup store (`seen_items.json`, `hunt_ids.json`, the scan-log blobs) |
| `seller_listings.py` | `/crawl seller`: one seller's Penn State listings into the Logger |
| `shipping_backfill.py` | `/crawl shipping`: fills shipping on existing `price_log` rows |
| `purchases.py` | `/crawl purchases`: the operator's own eBay purchases into the Logger |
| `price_log.py` | Shared. The `price_log` tab |
| `match_logging.py` | Shared. `match_log` / `confirm_log` schemas and records |
| `sheet_retry.py` | Shared. Surviving the Sheets per-minute write quota |
| `sheets_client.py` | Google Sheets client for loading the buy rules |
| `scan_log.py` | Where a scan-log record belongs (monthly partitions) and what a lot's per-button price is (frozen scan's log) |
| `notifier.py` | Formats and sends the Slack alerts and warnings |
| `pipeline_classify.py` | Pure helpers for the frozen scan's deal alerts (`lot_value_and_deal`, `lookalike_note`) |
| `clip_matcher.py` | CLIP matching (frozen scan, `/test-clip`): loads the banks, text DB and reference vectors |
| `image_proc.py` | Downloads listing photos and cuts button crops (frozen scan, `/test-clip`) |
| `scoring.py` | Pure scoring helpers for the matcher; the constants buttonmatcher's parity test pins |
| `rerank.py` | Two-level reference re-ranking (off by default; weights pinned to buttonmatcher's) |
| `normalize.py` | The single slogan-string normalisation policy |
| `utils.py` | Pure helpers (title keyword exclusion, era queries, hunt-id ordering) |
| `bank_cache.py`, `confusable_slogans.py`, `detect_mask.py`, `detect_scale.py`, `edition_twins.py` | Shared with buttonmatcher (see its table) |
| `check_base_pins.py` | The Docker build check that the shared base image has every pinned package |
| `__init__.py` | Marks the package |
| `listing_uploads/` | The operator's eBay bulk-listing CSVs (`EBAY_BULK_LISTING.md`) |

**Where things are in `main.py`:** `_get_secret` and the Slack app (top); `_operator_request_ok`,
`_scheduler_token_ok`, `_run_scan_authorized` (auth); `run_scan` (the daily entry);
`handle_crawl_command` (the slash command) and `/internal/crawl`; `_run_crawl` (search, seen/relist
filter, handoff loop, summary); `_handoff_lot_to_buttonmatcher`; `_log_handoff_rows`,
`_relist_index`; `_start_seller_pull` / `_start_shipping_backfill` / `_start_purchases_pull` and
their `/internal/*` routes; `startup`; and, frozen, `_run_daily_scan`, `_evaluate_listing`.

## Switches

Every environment variable this service's own code reads (grep of `os.environ` / `os.getenv`,
2026-11-10). On/off flags: `0` turns a default-on one off.

| Variable | Default | What it does |
|---|---|---|
| `EBAYSCOUT_SKIP_RELISTS` | on | Skip lots recognised as relists. `0`: hand them over again |
| `DAILY_PIPELINE_FEED` | on | The daily run hands lots to buttonmatcher. `0`: the frozen CLIP scan |
| `BUTTONMATCHER_URL` | buttonmatcher's Cloud Run URL (`yolo_test.py`) | Where the handoff goes |
| `PIPELINE_SHARED_SECRET` | empty | The secret sent as `X-Pipeline-Secret` to buttonmatcher, and accepted from the operator on `/run-scan` and `/test-clip` |
| `SERVICE_URL` | empty (localhost) | This service's own URL, for `/crawl`'s self-POST to `/internal/crawl` |
| `SERVICE_BASE_URL` | the current Cloud Run URL | Builds the eBay account-deletion endpoint URL |
| `SCHEDULER_SA_EMAIL`, `SCHEDULER_AUDIENCE` | the live job's account / this service's URL | Who may call `/run-scan` with an OIDC token, and the token's audience |
| `CHANNEL_ID_EBAY`, `SPREADSHEET_ID`, `LOGGER_ID` | unset | Slack channel and sheet keys: plain env var when set, else Secret Manager |
| `EBAYSCOUT_LOOKALIKE_FLAG` | on | The look-alike line on the frozen scan's deal alerts. `0`: off |
| `BUTTONMATCHER_TWIN_GUARD` | on | Twin-family winners go to review in the frozen scan. `0`: off (shared name) |
| `EBAYSCOUT_BG_DIFF`, `EBAYSCOUT_TINY_GUARD`, `EBAYSCOUT_BLOB_BUSTER` | on | Detector refinements in `image_proc.py`. `0` turns each off |
| `EBAYSCOUT_RERANK`, `BUTTONMATCHER_RERANK` | off | `1`: reference re-ranking (either name) |
| `BUTTONMATCHER_SHADOW_PASS`, `BUTTONMATCHER_UNFOLD`, `BUTTONMATCHER_GAME_DATE_YEAR` | on | Read by the shared `match_logging.py` / `edition_twins.py`; same meaning as in buttonmatcher's `REFERENCE.md` |
| `OMP_NUM_THREADS` | 2 | CPU threads for CLIP |

`BUTTONMATCHER_OCR_AUTO_MIN`, `BUTTONMATCHER_TEXT_VARIANTS` and `BUTTONMATCHER_LABEL_HARVEST` appear in
this repo's shared files only in comments: setting them here does nothing. `DAILY_HUNT_BUDGET`
(50) and `CRAWL_MAX_LOTS_CAP` (1000) are constants in `config.py`, not variables. The `EBAY_*` names
(`EBAY_APP_ID`, `EBAY_CERT_ID`, `EBAY_BOT_TOKEN`, `EBAY_USER_TOKEN`,
`EBAY_DELETION_VERIFICATION_TOKEN`) are **Secret Manager secrets**, as are `SIGNING_SECRET_ES`,
`GOOGLE_SHEETS_JSON` and `ETSY_API_KEY` (`DEPLOY.md`).

**Named in docs, absent from the code:** `EBAYSCOUT_YOLO_TEST` (removed 2026-10-25: the handoff
is unconditional), and the Gemini-pipeline switches `EBAYSCOUT_ANCHOR_RECOVERY`,
`EBAYSCOUT_FRAME_FIT`, `EBAYSCOUT_GRID_HOLE_FILL`, `EBAYSCOUT_MATCH_FULLRES_SHADOW`,
`EBAYSCOUT_UNFOLD` (the last is spelled `BUTTONMATCHER_UNFOLD` in the code). The buttonmatcher-only
dead names are listed in its `REFERENCE.md`. `PIPELINE_SHARED_SECRET`'s name is the retired
watcher's; it is live.

## Bucket layout

The same bucket as buttonmatcher, `60d488c5-9c8e-4acc-aac-button-data` (`config.BUCKET_NAME`).
buttonmatcher's `REFERENCE.md` has the full table; this service's part:

| Path | What | Access |
|---|---|---|
| `ebay_scout/seen_items.json` | Item ids already handed over (dedup) | R/W |
| `ebay_scout/hunt_ids.json` | Item ids for `?hunt_ids` (frozen scan) | R |
| `ebay_scout/ondemand2_state.json` | Whether `/crawl` has made its first run | R/W |
| `ebay_scout/scan_log/YYYY-MM.jsonl` (and `ebay_scout/scan_log.jsonl`) | The frozen scan's per-lot records; **the daily feed and `/crawl` do not write it** (since 2026-11-08 their lots are recorded in `price_log`) | W (frozen scan only) |
| `text_db.json`, `vectors.pt`, `text_features.pt` and their `.npz` twins | buttonmatcher's catalog and CLIP banks | R |

## Live configuration

What is set on the running service is not in the repo, and a web session cannot read it. The
operator reads it in Cloud Shell:

```
gcloud run services describe ebay-scout --region=us-east1 --format=export
```

(buttonmatcher's service is `button-inventory`, same region.) It lists the env vars and mounted
secret *names*, not secret values. Do not guess values.

## Tools

`ebayscout/ebayscout/tools/`, all offline and read-only against exported data; run times are not
stated in the scripts. Most read the frozen scan's `scan_log` and so go quiet as the daily feed
no longer writes it. The tools that grade the matcher live in buttonmatcher.

| Tool | What it does |
|---|---|
| `market_report.py` | Market report over the scan log (pure Python) |
| `build_master_dataset.py` | Consolidates the scan/market data sources into one master dataset |
| `merge_scan_log.py` | One-time seed: folds recovered records into the monthly scan log |
| `import_stdout_log.py` | Recovers scan-log-shaped records from a Cloud Run stdout export |
| `audit_reference_coverage.py` | Crosses the sheet's needed (year, slogan) rows with the reference embeddings |
| `build_goal_trackers.py` | Turns `LOGGER_FRONTS.md` into the Progress Trackers workbook |

## Key to the ID systems

buttonmatcher's `REFERENCE.md` has the full key (fronts `A1`/`B31`/`C4`, `SR-nn`, `WS1`, `RS-nn`,
`IC-nn`, § numbers). Owned here: **`DECISIONS.md` #1-#34** (`ebayscout/DECISIONS.md`), the numbered design
decisions of this service (#12 CLIP scores collapsing to 0.00, the quantised-query shift among the causes; #17 manual-upload state held in one container, why `--max-instances=1`;
#29 `/crawl <N>`; #33 SR-07/08 and the frozen legacy scan; #34 the shared base image). **`SR-11`** in `.github/workflows/tests.yml` is a
strategic-review item (`STRATEGIC_REVIEW_2026-09.md`).
