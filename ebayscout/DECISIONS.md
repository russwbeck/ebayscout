# eBay Scout — Development Decisions & History

A record of the problems we hit, why we made the choices we made, and what
broke along the way.  Read this before touching the deployment or the Cloud
Run / gunicorn configuration.

---

## 1. Cloud Run Service, not a Job

The bot started life as a **Cloud Run Job** — a one-shot container that runs,
finishes, and exits.  It was converted to a **Cloud Run Service** (long-running
gunicorn server) for two reasons:

1. **Slack event handling.**  Slack sends file-upload events over HTTP.  A Job
   can't receive inbound HTTP traffic; only a Service can.
2. **CLIP model cost.**  Loading ViT-B/32 + quantization takes 30–60 s on CPU.
   A Job pays that cost on every run.  A Service pays it once on cold start, then
   reuses the loaded model for every subsequent request.

---

## 2. CLIP weights baked into the Docker image

On first deploy the container downloaded the CLIP ViT-B/32 weights (~338 MB)
from OpenAI's CDN at runtime.  This added 30–60 s to every cold start and
occasionally failed under CDN throttling.

Fix: add one line to the Dockerfile that runs `clip.load()` at **build time**
so the weights land in a Docker layer that is cached by Cloud Build:

```dockerfile
RUN python -c "import clip; clip.load('ViT-B/32', device='cpu'); print('CLIP ViT-B/32 cached.')"
```

The layer is rebuilt only when `requirements.txt` changes (which forces a new
`pip install` layer upstream).  All other builds skip it entirely.

---

## 3. Gunicorn preload_app + lazy ML imports

Without `preload_app = True`, gunicorn forks N workers and each one imports the
entire module — meaning N × Secret Manager calls, N × torch imports.  With a
single worker, this is just one extra import, but `preload_app` also makes
startup errors appear in logs immediately rather than only when the first request
arrives.

`torch`, `clip`, and `cv2` are **lazily imported** (inside `startup()` and the
scan functions).  They are slow to import and not needed to answer a `/health`
probe or a Slack event acknowledgement.  Lazy imports let the worker start
accepting connections in < 1 s even though CLIP hydration takes 30–60 s.

---

## 4. The scan runs synchronously inside the HTTP request

Cloud Run's default CPU allocation mode gives you full CPU **only while an HTTP
request is being handled**.  Between requests, CPU is throttled to near zero.

The daily scan was originally kicked off in a background thread spawned inside
`/run-scan`.  The endpoint returned 200 immediately, the request ended, CPU
dropped to ~0%, and the scan crawled or stalled.

Fix: run the entire scan **synchronously** inside `/run-scan`.  The request
stays open (Cloud Scheduler's `attempt-deadline` is set to 1800 s to match),
Cloud Run sees an active request, and the full 2 CPUs stay allocated for the
duration of the scan.

The `/run-scan` handler holds `_scan_lock` for the full duration.  A duplicate
trigger from the scheduler (e.g. a retry) returns 409 and does nothing.

---

## 5. Keep-warm spinner for background threads

Ported from `buttonmatcher/main.py`.  Cloud Run's CPU throttling happens at the
**request boundary** — as soon as Flask returns an HTTP response (e.g., the
3-second Slack ack), Cloud Run considers the request done and is free to throttle
the container's vCPUs to near zero.  Any work still happening in a background
thread — CLIP hydration, manual image analysis — gets severely starved.

Fix: a `_keep_cpu_hot()` context manager spins a cheap arithmetic loop in a
daemon thread for the duration of the background work.  One spinning core is
enough to signal "busy" to Cloud Run's scheduler while leaving the remaining
CPU fully available to PyTorch.

```python
@contextlib.contextmanager
def _keep_cpu_hot():
    _stop = threading.Event()
    def _spin():
        _x = 1.0
        while not _stop.is_set():
            for _ in range(10_000):
                _x = (_x * 1.0000001 + 0.0000001) % 1.0
    _t = threading.Thread(target=_spin, daemon=True)
    _t.start()
    try:
        yield
    finally:
        _stop.set()
        _t.join(timeout=1)
```

Applied to:
- `_hydrate()` in `startup()` — CLIP loading in post-fork background thread
- `_run_manual_analysis()` — CLIP inference after the Slack ack returns

Not needed for `_run_daily_scan()` — that runs synchronously inside the
`/run-scan` request, so CPU is guaranteed allocated for its entire duration.

---

## 6. Cold-start CLIP loading (the last throttling bug)

Even with the synchronous scan fix above, there was still a failure mode on cold
starts:

1. Container starts.  `post_fork` calls `startup()`.
2. `startup()` spawns a **daemon thread** (`_hydrate`) to load CLIP.
3. Before any HTTP request arrives, Cloud Run has no reason to keep CPU
   allocated.  The daemon thread is throttled.
4. Cloud Scheduler fires `POST /run-scan`.
5. The old code checked `if not vectors_loaded: return 503`.
6. Scheduler received 503, logged a failure, and gave up (or retried once the
   container was cold again — same result).

Fix: instead of returning 503, `/run-scan` now calls `cm.init()` **synchronously
within the request handler** when `vectors_loaded` is False.  The request
itself provides the CPU budget.  The same fallback reloads `buy_rules` if the
Sheets fetch in `startup()` was also throttled before completing.

The daemon thread in `startup()` is kept.  On a warm container (CLIP already
loaded from a previous scan), it's a no-op.  On a cold start it races with the
`/run-scan` synchronous load — which is why `clip_matcher.init()` is now
protected by a `threading.Lock()`: the first caller loads, the second caller
finds `_initialized = True` inside the lock and returns immediately.

---

## 6. eBay API migration: Finding/Shopping → Browse

eBay decommissioned the Finding API and Shopping API on **2025-02-05**.  Any
call to those endpoints returns an error.

The bot was rewritten to use the **Browse API** (`/buy/browse/v1/item_summary/search`).
Key differences:

- Browse requires an **OAuth application token** (client-credentials grant) built
  from App ID + Cert ID.  The token is short-lived; `ebay_client.py` fetches a
  fresh one on each scan.
- Browse's `item_summary/search` returns up to 200 results per page, but deep
  pagination (`offset > ~9900`) returns HTTP 400.  Pagination was dropped;
  we rely on 13 targeted queries × 100 results each instead.
- `itemSummary.categories` contains the full ancestry, so a single check against
  `EXCLUDED_CATEGORY_IDS` catches all apparel regardless of sub-category depth.

---

## 7. Cloud Build trigger — one deploy path only

There was briefly a **GitHub Actions** workflow (`deploy-ebayscout.yml`) that
also built and pushed the Docker image on pushes to `main`.  It raced with the
Cloud Build trigger, producing two concurrent builds and occasionally two
simultaneous `gcloud run deploy` calls.

The GitHub Actions workflow was removed.  `cloudbuild.yaml` is now the single
source of deploys.  Cloud Build is triggered automatically on push to `main`
with an `ebayscout/**` file filter.

---

## 8. Image processing: grid crops replaced by whole-image fallback

The original button-detection code fell back to a 3×3 grid of crops when
OpenCV's Hough circle detector found no circles.  The grid crops were arbitrary
rectangular tiles that rarely corresponded to actual buttons, producing
consistent CLIP mismatches.

Fix: when circle detection finds nothing, use the **entire image** as a single
crop.  For listings that are genuinely a single button photographed straight-on,
this gives CLIP the best possible input.  For multi-button lots where circles
weren't detected (blurry, poor contrast), a whole-image crop still gives CLIP
something to work with rather than a meaningless quadrant.

---

## 9. eBay Marketplace Account Deletion endpoint

eBay's Developer Program requires all production Browse API applications to
either subscribe to account-deletion notifications or opt out.  Failure to
configure the endpoint causes eBay to revoke the application's API access.

The `/ebay/account-deletion` endpoint handles the two-leg protocol:
- `GET` with `?challenge_code=` — SHA-256 challenge handshake performed once
  when the endpoint URL is saved in the developer portal.
- `POST` — deletion notification.  The bot stores no eBay user PII (only public
  listing IDs in `seen_items.json`), so no deletion action is needed; we
  acknowledge with 200.

eBay sends a high, constant volume of POST notifications.  Logging them drowned
out scan logs and was removed.

---

## 10. PROJECT_NUMBER history

The bot was originally configured with the wrong GCP project number.  It went
through several corrections:

| Commit | Number used | Why it was wrong |
|--------|------------|-----------------|
| Initial | `497602...` (`ebay-scout-497602`) | Wrong project entirely |
| Fix | `5194730759` | Still the wrong project |
| Final fix | `404960106109` | Correct — the `buybot` project that owns the GCS bucket, Secret Manager secrets, and Cloud Run service |

The project number is used only for Secret Manager path construction
(`projects/{number}/secrets/...`).  If secrets stop being accessible, check
this value in `config.py` first.

---

## 11. Noise filtering evolution

The search queries (`Penn State button`, `PSU pin`, etc.) surface a lot of
apparel.  Filters were added incrementally as noise patterns were identified:

| What | Where | Why |
|------|-------|-----|
| Seller blocklist (`EXCLUDED_SELLERS`) | `config.py` | Specific shops that sell only apparel or reprints |
| Keyword filter (`EXCLUDED_KEYWORDS`) | `config.py` | Title words that reliably indicate non-button items (hoodie, embroidered, enamel, etc.) |
| Category filter (`EXCLUDED_CATEGORY_IDS`) | `config.py` | eBay category 11450 (Clothing, Shoes & Accessories) catches ~20% of noise before any image download |
| Title logging (`>>> TITLE: [rejected ...]`) | `main.py` | Greppable log lines for tuning; filter Cloud Logging for `TITLE: [rejected` to find new noise patterns |

---

## 12. CLIP scores were 0.00 for every listing (two root causes)

After the first full scan ran (~600 listings, ~3 hours), every single listing
scored exactly 0.00.  Real CCB/Mellon/Citizens buttons were being rejected.

**Root cause 1: integer labels in `vectors.pt`**

`_ref_labels` (loaded from `vectors.pt`) contains plain integers (years), not
`"YEAR SLOGAN"` strings.  `_score_best_match` called `label.split()[0]` on each
label, which raises `AttributeError` on an int.  The `except` clause only caught
`ValueError` and `IndexError` — not `AttributeError` — so every label was
silently skipped, `year_image_scores` was always empty, and every crop scored
0.00.  Fix: `int(label) if isinstance(label, (int, float)) else int(str(label).split()[0])`.

**Root cause 2: `quantize_dynamic` shifts the output space**

`clip_matcher.init()` called `torch.quantization.quantize_dynamic()` at runtime
to speed up CPU inference.  The reference vectors in `vectors.pt` were built with
a **full-precision** model.  Re-quantizing at inference time changes the model's
output distribution enough that all cosine similarities collapse toward zero.
Fix: remove `quantize_dynamic` entirely.  Inference is slower but scores are now
in the expected 0.60–0.80 range for real buttons.

Both bugs existed simultaneously, so fixing either one alone would have left
scores at 0.00.

---

## 13. Batch CLIP encoding

The original scan loop called `match_crop(crop)` once per crop — one CLIP
forward pass per button circle detected per photo.  With no `quantize_dynamic`,
each forward pass on CPU takes ~15–30 seconds.  A listing with 4 crops would
take 1–2 minutes by itself.

Fix: `match_crops_batch(crops)` stacks all crops for a listing into a single
tensor and runs one forward pass, then scores each crop from the resulting
embedding matrix.  Cost is roughly constant regardless of crop count.  Applied
to both the daily scan and manual analysis.

---

## 14. PSU queries restricted to Sports Memorabilia

`"PSU button"`, `"PSU pin"`, etc. flood results with Power Supply Unit electronics
(cables, GPU accessories).  Log analysis showed 65+ PSU electronics titles in a
single scan.

Fix: `PSU_SEARCH_QUERIES` are passed with `category_ids=SPORTS_MEMO_CATEGORY_ID`
(`"64482"` = Sports Mem, Cards & Fan Shop) so "PSU" matches Penn State University
memorabilia rather than PC power supplies.

---

## 15. Dedup checkpoint every 50 listings

The original code wrote `seen_items.json` only at the very end of the scan.
With a 1800-second timeout and ~600 listings taking close to that limit, a timeout
would cause the next scan to re-process every listing.

Fix: write `seen_items.json` to GCS every 50 listings mid-scan.  The final write
still happens at the end.  If the container times out, at most 49 listings are
re-processed on the next run.

---

## 16. Slack Event Subscriptions were not configured

The manual upload flow (user uploads photo → bot analyses lot) depends on two
Slack event subscriptions:

- `file_shared` — fires when a user uploads a file to the channel
- `message.channels` — fires when a user sends a message (needed to receive the
  price/source reply)

Neither was enabled.  Slack Events API was not turned on at all on first deploy.
The bot was receiving zero Slack events despite the endpoints existing.

Fix: in api.slack.com → App → Event Subscriptions → enable, set Request URL to
`https://ebay-scout-404960106109.us-east1.run.app/slack/events`, add both
subscriptions.

---

## 17. Manual upload reply never processed (three bugs)

After enabling event subscriptions the bot saw the file upload but ignored the
user's price/source reply.  Three bugs stacked:

**Bug 1 — `file_share` subtype triggering `handle_message`.**
When a user uploads a file, Slack fires both a `file_shared` event AND a
`message` event with subtype `file_share`.  The message handler ran on the file
upload message itself (which contains no `$XX | Source` text), produced
"Couldn't parse that", and consumed no pending scan state.  Fix: early-return
on `event.get("subtype") == "file_share"`.

**Bug 2 — `event_ts` ≠ `thread_ts`.**
`handle_file_shared` stored `event_ts` from the `file_shared` event as the
expected `thread_ts`.  When the user replied, Slack sent the actual message
`thread_ts` (the parent message's `ts`), which did not match.  Fix: remove the
`thread_ts` comparison entirely; keying on `user_id` alone is sufficient since
`pending_scans[user_id]` is deleted immediately after the analysis thread starts.

**Bug 3 — multiple Cloud Run instances.**
With no instance limit, Cloud Run could spin up a second container for a
concurrent request.  The first container stored `pending_scans[user_id]`; the
second had an empty dict and dropped the reply silently.  Fix: `--max-instances=1`
in `cloudbuild.yaml` so all requests always reach the same container.

---

## 18. Hough circle detection distribution (eBay daily scan)

Analysis of 147 eBay listing photos from the first full scan:

| Circles detected | Listings | % |
|-----------------|----------|---|
| 0 (whole-image fallback) | 55 | 37% |
| 1 | 19 | 13% |
| 2 | 32 | 22% |
| 3 | 11 | 8% |
| 4–12 | 30 | 20% |
| Max | 12 | — |

This distribution is expected for eBay.  Most sellers photograph one button at a
time; 0-circle fallback is common for cluttered backgrounds.  Maximum detected
was 12, meaning no large multi-button lots were surfaced by eBay in this scan.

The **manual upload** case is different: a collector uploading a lot photo may
have 20–35 buttons.  The default 4×3 grid radius is too large to detect small
buttons in a dense lot.  Two fixes were added:

1. **Two-pass Hough**: if the first pass finds fewer than 4 circles, a second
   pass runs at half the expected radius.
2. **User-supplied count**: the reply format now accepts an optional third field —
   `$25.00 | Facebook Marketplace | 35` — which sets `expected`, `rows`, and
   `cols` so the radius scales correctly for the actual lot size.

---

## 19. Noise keyword expansion

Log analysis of 480+ titles from the first scan identified recurring non-button
listing types.  `EXCLUDED_KEYWORDS` was expanded from 14 to 27 terms:

Added: `shirt`, `jersey`, `vest`, `brooch`, `lanyard`, `strap`, `ornament`,
`christmas`, `wooden`, `cable`, `badge reel`, `map`, `sticker`, `decal`

`book` was explicitly kept: Penn State football button books are often sold
bundled with actual buttons and are desirable lots.

`supply`, `monitor`, `parts` were left out — largely handled by the PSU category
restriction, and risky to exclude broadly.

The terminology "bank button" was replaced with "gameday button" throughout the
codebase.  The scanner targets Penn State Gameday buttons across all eras (CCB
1972–1983, Mellon Bank, Citizens Bank), not just Central Counties Bank.

---

## Remaining known issues

| Issue | Status | Notes |
|-------|--------|-------|
| CLIP accuracy on multi-button photos | Open | 2003 "Gopher Broke" matched 2002/2006 at 0.697 — just below 0.72 threshold. Likely the Hough crop didn't isolate the right button. Needs a clean single-button eBay photo to validate. |
| kling24toys seller filter | Open | Listed in `EXCLUDED_SELLERS` but need to confirm actual eBay username matches after new scan logs show seller names in brackets. |
| Manual upload button count UX | Newly deployed | Merged in PR #5, not yet validated in production. |
| First scan with correct CLIP | Pending | 9am Cloud Scheduler trigger tomorrow will be the first scan with working scores. Need to confirm alerts fire for real buttons. |


```
gunicorn master starts
  → preload_app=True: imports ebayscout.main in master
      → fetches EBAY_BOT_TOKEN, SIGNING_SECRET_ES, CHANNEL_ID_EBAY
      → creates Slack Bolt app
  → forks worker
      → post_fork hook: calls main.startup()
          → fetches GOOGLE_SHEETS_JSON, SPREADSHEET_ID
          → loads buy_rules synchronously
          → spawns _hydrate daemon thread (loads CLIP — may be throttled)
worker starts accepting requests
  → /health → 200 immediately ("hydrating" or "ready")
  → /run-scan (from Cloud Scheduler):
      → if not vectors_loaded: loads CLIP synchronously HERE (CPU guaranteed)
      → if not buy_rules: reloads synchronously HERE
      → acquires _scan_lock
      → runs _run_daily_scan() inline (keeps CPU allocated)
      → releases lock
      → returns 200
```
