# Watcher contract — routing eBay-Scout lots through the Gemini pipeline

`/crawl10` no longer calls Gemini directly. It now pushes each lot's **primary
photo** into the same Drive → Gem → GCS pipeline buttonmatcher uses, and consumes
the result asynchronously. The watcher (separate repo, **not** in this codebase)
must be updated to route ebayscout's lots back to ebayscout. This is the one
piece that lives outside ebayscout — until it's done, the end-to-end flow won't
complete (but `/internal/pipelinetest` works without it; see VERIFY below).

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

## Config the operator must set
| Where | Key | Value |
|---|---|---|
| ebayscout Cloud Run env | `PIPELINE_SHARED_SECRET` | shared secret the watcher presents (map from Secret Manager) |
| watcher config | `gcs_input_prefix` | `pipeline/input` (default; where ebayscout drops photos) |
| watcher config | ebayscout notify URL | `https://<ebayscout-service>/pipeline/notify` |
| watcher config | ebayscout pipeline secret | same value as `PIPELINE_SHARED_SECRET` |

ebayscout reaches GCS with its runtime (compute) SA — no Drive folder/SA config
is needed anymore. `DRIVE_FOLDER_ID` / `DRIVE_SA_JSON` are no longer used by the
upload path.

Stay scale-to-zero: no `--no-cpu-throttling`, no `--min-instances`; keep
`--max-instances=1`. The notify→internal kick is the same proven pattern as
`/internal/crawl10`.

## Verify (no watcher needed for step 1)
1. `GET /internal/pipelinetest?object=pipeline/output/ebayscout__<key>.png.response.json`
   (use an object the Gem already produced) → watch `#ebay-checker` for the lot
   post + the auto-confirmed buttons + the Yes/No prompt.
2. Full path: run `/crawl10` → photos upload to Drive → Gem → watcher →
   `/pipeline/notify` → `#ebay-checker`. Click **Yes** (crops land under
   `reference/_staging/<entry_id>/`) and **No** (temp crops deleted from GCS).
