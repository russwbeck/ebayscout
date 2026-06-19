# Watcher contract — routing eBay-Scout lots through the Gemini pipeline

`/crawl <N>` pushes each lot's **primary photo** into the same
Drive → Gem → GCS pipeline buttonmatcher uses, and consumes the result
asynchronously. The watcher (separate repo, **not** in this codebase) routes
ebayscout's lots back to ebayscout. This is the one piece that lives outside
ebayscout — until it's done, the end-to-end flow won't complete (but
`/internal/pipelinetest` works without it; see VERIFY below).

`/crawl <N>` feeds up to N lots (1–1000) in one run; the watcher drains them one
at a time, so a big run is multi-hour (~1–2 min/Gem). On each confirmed result
ebayscout posts only **deals** (a needed button, or matched value > asking),
**auto-stages** the surest crops into `reference/_staging/`, and marks the lot
**seen** — so a lot is never re-run.

## What ebayscout does (already implemented here)
- Uploads each primary photo to **GCS** at
  `pipeline/input/ebayscout__<key>.png` (`<key>` = a random 12-hex correlation
  token). It uses GCS, **not** Drive, because a service account has no Drive
  storage quota on a personal Google account ("storageQuotaExceeded"); GCS uses
  the project quota, so the compute SA can write freely.
- Persists the listing context to GCS at `ebay_scout/pending/<key>.json` so the
  async result is correlated even after a scale-to-zero cold start.
- Exposes `POST /pipeline/notify` (fast 204) → `POST /internal/pipeline` (CPU-hot)
  → detect + reconcile + CLIP + resolve → posts per-lot results to `#ebay-checker`.

## What the watcher must do
1. **Poll the GCS input prefix** `pipeline/input/` (alongside the existing Drive
   poll). For each new object, download it, run it through the Gem exactly as a
   Drive file, write `pipeline/output/<f>.png` + `.response.json`, then **delete
   the input object** so it isn't reprocessed. (buttonmatcher's Drive flow is
   unchanged.)
2. **Route the notify by filename prefix.** The Gem writes
   `pipeline/output/<f>.png` + `.response.json` (shared with buttonmatcher). When
   `<f>` starts with **`ebayscout__`**, POST the finished `.response.json` object
   name to **ebayscout's** notify URL; otherwise POST to buttonmatcher as today.
   Exclusive — each object goes to exactly one service.
3. **Notify ebayscout.** `POST https://<ebayscout-service>/pipeline/notify`
   with header `X-Pipeline-Secret: <PIPELINE_SHARED_SECRET>` and body
   `{"object": "pipeline/output/ebayscout__<key>.png.response.json"}`.

(The provided `watcher.py` already implements all three — `process_gcs_input` +
the `gcs_input_prefix` poller + `notify_pipeline` routing.)

## Gem-exhaustion failsafe (watcher.py)
For a big `/crawl <N>` run, the watcher self-halts if the Gem runs out of
tokens, so it doesn't churn the whole queue into empties:
- Failure is detected by **JSON-parseability** — a healthy Gem returns the
  prompt's JSON object; anything that doesn't parse (or the response-timeout
  sentinel) is a failure. A valid JSON with **0 buttons is NOT** a failure (just
  an empty lot).
- Consecutive failures are counted across **both** input paths; any success
  resets the count.
- On a failure the watcher does **not** upload/notify and does **not** consume
  the input (the GCS object stays in `pipeline/input/`; a Drive file is not moved
  to `Done/`), so a restart after refilling the Gem resumes draining the queue —
  **no lots lost**.
- At `gem_empty_limit` (default **5**) consecutive failures it logs a halt line
  and exits; if `slack_webhook_url` is set it also posts the halt to Slack.

## Multiple workers (one Gemini account per worker → N× throughput)
A big `/crawl <N>` run is ~1–2 min/Gem, so N lots ≈ N×(1–2 min) on a single Gem.
With several Gemini accounts (each its own token budget — e.g. a family plan), run
**one `watcher.py` process per account** ("worker") to cut wall-clock roughly N×.
Every worker polls the **same** input prefixes but processes only its
deterministic shard (`crc32(filename) % worker_count == worker_index`), so lots
split with **no overlap, no locking, and no feed change**. `worker_count=1` /
`worker_index=0` (defaults) = a single worker owns everything.

**Per-worker config (`workerN.json`).** The files are identical except the three
★ fields, which must be **unique per worker**:

| Key | Value |
|---|---|
| `worker_count` | total parallel workers (e.g. `2`) — **same** in every config |
| `worker_index` ★ | this worker's index, `0 … worker_count-1` |
| `user_data_dir` ★ | absolute path to this worker's **own** Chrome profile dir (no `~`); Chrome locks a profile to one process, so it must not be shared |
| `gem_url` ★ | this account's Gem URL |
| `gcs_bucket` | `60d488c5-9c8e-4acc-aac-button-data` |
| `gcs_input_prefix` / `gcs_prefix` | `pipeline/input` / `pipeline/output` |
| `ebayscout_notify_url` | `https://<ebayscout-service>/pipeline/notify` |
| `pipeline_shared_secret` | the shared `PIPELINE_SHARED_SECRET` (both services pull the same secret; the optional `ebayscout_pipeline_shared_secret` falls back to it, so it can be omitted) |
| `service_account` | full SA JSON (identical in every config) |
| `gem_empty_limit` | consecutive Gem failures before this worker self-halts (default `5`) |

Two-worker example: `worker0.json` → `worker_index 0`, `…/gem-profile0`;
`worker1.json` → `worker_index 1`, `…/gem-profile1`; both `worker_count 2`. The
profile-folder name need not match the index — only **uniqueness** and the config
pointing at the **right logged-in folder** matter.

**One-time login per profile.** Google blocks sign-in inside the
automation-controlled browser ("this browser may not be secure"), so
pre-authenticate each profile with real Chrome and let the watcher reuse the saved
session: with all other Chrome closed, run
`google-chrome --user-data-dir=<that worker's user_data_dir>`, sign into **that
worker's account**, open its Gem once, fully quit. Then launch the workers as
separate processes — `python3 watcher.py --config worker0.json` and
`… --config worker1.json` (not `--login`).

**Gem output must be valid JSON.** A reply that doesn't parse leaves the lot
queued for retry and counts toward the halt limit. Slogans that contain quotation
marks must use single quotes (`'`) inside the value — a raw `"` inside a string
(e.g. `PSU Dots The "I" In Win`) makes the whole object invalid — so the Gem
prompt must enforce single-quotes-inside.

Each worker's Gem-exhaustion failsafe is independent: if one account runs dry it
halts and its shard pauses (queued + un-`seen`) while the others keep going;
restart it after the quota resets. ebayscout (single Cloud Run instance, 8
gunicorn threads) handles the concurrent results — `/pipeline/notify` dedups by
object name and the scan_log/seen writes are lock-guarded.

## Config the operator must set
| Where | Key | Value |
|---|---|---|
| ebayscout Cloud Run env | `PIPELINE_SHARED_SECRET` | shared secret the watcher presents (map from Secret Manager) |
| watcher config | `gcs_input_prefix` | `pipeline/input` (default; where ebayscout drops photos) |
| watcher config | ebayscout notify URL | `https://<ebayscout-service>/pipeline/notify` |
| watcher config | ebayscout pipeline secret | same value as `PIPELINE_SHARED_SECRET` |
| watcher config | `gem_empty_limit` | *(optional)* consecutive Gem failures before halt (default `5`) |
| watcher config | `slack_webhook_url` | *(optional)* Slack incoming-webhook URL for the halt ping |
| watcher config | `worker_count` | *(optional)* total number of parallel watchers (default `1`) |
| watcher config | `worker_index` | *(optional)* this watcher's index, `0..worker_count-1` (default `0`) |

ebayscout reaches GCS with its runtime (compute) SA — no Drive folder/SA config
is needed anymore. `DRIVE_FOLDER_ID` / `DRIVE_SA_JSON` are no longer used by the
upload path.

Stay scale-to-zero: no `--no-cpu-throttling`, no `--min-instances`; keep
`--max-instances=1`. The notify→internal kick is the same proven pattern as
`/internal/crawl`.

## Verify (no watcher needed for step 1)
1. `GET /internal/pipelinetest?object=pipeline/output/ebayscout__<key>.png.response.json`
   (use an object the Gem already produced) → `#ebay-checker` posts only if the
   lot is a deal (needed button / undervalued); the surest crops auto-stage under
   `reference/_staging/<entry_id>/`; `scan_log.jsonl` + `seen_items.json` grow.
2. Full path: run `/crawl 10` (small validation of the exact full-run output
   path) → photos upload to GCS → Gem → watcher → `/pipeline/notify` →
   `#ebay-checker`. Then `/crawl 800` (or up to 1000) for a big run; a re-run
   skips already-confirmed lots.
