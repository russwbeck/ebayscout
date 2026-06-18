# Watcher contract — routing eBay-Scout lots through the Gemini pipeline

`/crawl10` no longer calls Gemini directly. It now pushes each lot's **primary
photo** into the same Drive → Gem → GCS pipeline buttonmatcher uses, and consumes
the result asynchronously. The watcher (separate repo, **not** in this codebase)
must be updated to route ebayscout's lots back to ebayscout. This is the one
piece that lives outside ebayscout — until it's done, the end-to-end flow won't
complete (but `/internal/pipelinetest` works without it; see VERIFY below).

## What ebayscout does (already implemented here)
- Uploads each primary photo to the shared Drive folder (`config.DRIVE_FOLDER_ID`)
  as **`ebayscout__<key>.png`** (`<key>` = a random 12-hex correlation token).
- Persists the listing context to GCS at `ebay_scout/pending/<key>.json` so the
  async result is correlated even after a scale-to-zero cold start.
- Exposes `POST /pipeline/notify` (fast 204) → `POST /internal/pipeline` (CPU-hot)
  → detect + reconcile + CLIP + resolve → posts per-lot results to `#ebay-checker`.

## What the watcher must do
1. **Route by filename prefix.** The Gem runs identically and writes
   `pipeline/output/<f>.png` + `pipeline/output/<f>.png.response.json` (shared with
   buttonmatcher). When `<f>` starts with **`ebayscout__`**, POST the finished
   `.response.json` object name to **ebayscout's** notify URL; otherwise keep
   POSTing to buttonmatcher as today. Routing must be exclusive (each object goes
   to exactly one service) so nothing is double-processed.
2. **Notify ebayscout.** `POST https://<ebayscout-service>/pipeline/notify`
   with header `X-Pipeline-Secret: <PIPELINE_SHARED_SECRET>` and body
   `{"object": "pipeline/output/ebayscout__<key>.png.response.json"}`.

## Config the operator must set
| Where | Key | Value |
|---|---|---|
| ebayscout Cloud Run env | `PIPELINE_SHARED_SECRET` | shared secret the watcher presents (map from Secret Manager) |
| ebayscout Cloud Run env | `DRIVE_FOLDER_ID` | the shared Drive folder id the watcher polls |
| ebayscout Secret Manager | `DRIVE_SA_JSON` | service-account JSON key (scope `drive.file`); the folder must be shared with the SA's email |
| watcher config | ebayscout notify URL | `https://<ebayscout-service>/pipeline/notify` |
| watcher config | ebayscout pipeline secret | same value as `PIPELINE_SHARED_SECRET` |

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
