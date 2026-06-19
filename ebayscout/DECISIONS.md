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

## 20. Manual mode looped "still loading" forever — `/scout` wake + self-heal

The biggest user-facing bug: you'd upload a photo and the bot just kept saying
*"⏳ Still loading — try again in about 30 seconds"* on every attempt.

Root cause: CLIP hydrates in a **background daemon thread** at startup (#5, #6).
Cloud Run throttles CPU to ~0% between requests, so on a cold/idle container
that thread is starved and `vectors_loaded` never flips to `True`. The old
`handle_file_shared` branch saw `not vectors_loaded`, posted "still loading",
**deleted the pending scan, and gave up.** Nothing forced a load except the
once-a-day `/run-scan`, so manual uploads were dead until the daily scan
happened to warm the container.

Fix (two prongs, both reusing the synchronous-load-within-request idea from #6):

1. **`/scout` slash command.** Bolt's command handler `ack()`s within Slack's
   3-second window with "waking up ~60s", then loads CLIP in a background thread
   wrapped in `_keep_cpu_hot()` (#5) and posts "✅ awake". This force-loads CLIP
   on demand so manual mode works without waiting for the daily scan.

2. **`handle_file_shared` self-heals.** Instead of deleting the pending scan, it
   now **keeps** it and kicks off the same background wake, telling the user to
   reply with the price. `_run_manual_analysis` calls `_ensure_clip_loaded()` as
   a backstop before matching, closing the race where the price reply arrives
   before hydration finishes.

The shared load path is now a single `_ensure_clip_loaded()` helper used by
`/run-scan`, `/test-clip`, `/scout`, the upload flow, and the startup hydration
thread — one place that calls the lock-guarded, idempotent `clip_matcher.init()`
and flips `vectors_loaded`. A `_wake_lock` / `_wake_in_flight` flag coalesces
concurrent wakes so the channel doesn't get duplicate "ready" posts.

Infra note: the service stays **scale-to-zero** (cheapest). `--no-cpu-throttling`
and a `--min-instances=1` warm instance are **off budget — do not add them**
(see CLAUDE.md). The `/scout` command + self-healing upload absorb the cold-start
cost; the way to keep CPU for heavy work is to run it inside an in-flight HTTP
request (the `/run-scan` pattern), never always-on CPU. `--max-instances=1` must
stay (#17 Bug 3).

Setup requirement: the `/scout` command must be registered in the Slack app
config (Slash Commands → Request URL `…/slack/events`) and the app reinstalled —
see DEPLOY.md.

Also cleaned up here: a dead duplicate `_format_manual_result` in `main.py` was
removed (the active path uses `format_manual_result` from `utils.py`), and the
`tests/test_main.py` `TestParsePriceSource` cases were updated to the 3-tuple
`(price, source, count)` return that `parse_price_source` adopted in #18.

---

## 21. The scan is a "needed-button detector", not a lot valuer

The automated scan originally tried to **value whole lots** and alert on
computed margin. That depends on segmenting individual buttons out of a photo —
and the segmentation step (`image_proc.detect_and_crop`, Hough circles) was only
reliable when the **human supplied a button count** that calibrated the expected
radius (`expected_r`). The manual `/scout` flow still passes that count; the
automated scan can't. On unpredictable eBay photos, count-free segmentation does
poorly. This is a genuinely hard CV problem, not a parameter-tuning miss.

Rather than fight it, the scan's job was **reframed**: *does this listing
plausibly contain a button I still need (`amount_needed > 0`)? If so, post a
candidate alert and let the human value it with `/scout`.* This is a
presence/retrieval problem, which is far more tractable than precise
segmentation. Changes:

- **Score all photos, not just the first.** `get_item_pictures` already returns
  every image; `MAX_PHOTOS_PER_LISTING` raised 1 → 4. A two-stage gate scores
  photo 1 first and only pulls the rest when the listing looks promising (title
  names a year, or photo 1 shows button-like signal) so junk listings stay cheap.
- **Recall over precision.** A new `match_crops_batch(..., top_k=K)` exposes the
  matcher's existing top-3 candidates per crop. A needed button is flagged when
  any candidate's `(year, slogan)` maps (via the fuzzy sheet lookup) to
  `amount_needed > 0` and clears `NEEDED_MATCH_THRESHOLD` (0.60, below the strict
  0.72) — catching the 2nd/3rd guess on a blended photo and the 0.697 near-miss
  from the table below.
- **Title-year corroboration.** `utils.extract_years()` pulls years from the
  title; if a needed button's year is in the title, the bar drops to
  `REJECTION_THRESHOLD`.
- **Undervalued/margin alerts are now opt-in** (`ENABLE_UNDERVALUED_ALERTS`,
  default False) and only fire on strict 0.72 matches. Auto-valuation is
  deferred until we trust it.
- **Scan log groundwork.** Every processed listing is appended as a JSON line to
  `SCAN_LOG_BLOB` (title, asking, photos scored, top matches + scores, needed/
  alerted flags). This is the dataset to later judge whether automated
  undervalued-lot detection is achievable.

Next lever if recall proves insufficient: replace Hough with a learned,
count-free region proposer (e.g. Segment Anything) + CLIP filtering. Deliberately
**not** done yet — it's a hundreds-of-MB model with real CPU/latency cost on
scale-to-zero Cloud Run, and presence detection across multiple photos may not
need it.

---

## 22. Year-aware matching + needed-year deep crawl

The matcher's most error-prone step is **choosing the year** (the 0.00-scores
saga in #12, and the 0.697 "Gopher Broke" near-miss where 2003 lost to
2002/2006). If the year is known up front, matching collapses to "confirm it's a
button + pick the best slogan *within that year*" — small and accurate.

Two independent improvements:

**(A) Title-year restriction (free, in every scan).** When a listing's title
names exactly one year that exists in the reference data, matching is restricted
to that year (`match_crops_batch(restrict_years={year})`). The hook already
existed — `_score_slogans` takes `allowed_years` — so this is wiring, not new
scoring. Ambiguous/zero-year titles fall back to the full matcher.

**(B) Needed-year deep crawl (on-demand).** Each eBay query returns only the
**newest ~100** of its keyword bucket (no deep pagination — #6), so older
needed-year buttons are invisible to the general queries. A query like
`Penn State button 1982` returns the newest ~100 of a *tiny* bucket — usually
*all* of it, including old listings — and matches eBay's title + item-specifics
index, not just the title text we parse. This is the only thing year-searching
adds over title-triggering, and it's the point: coverage of deep inventory.

Mechanics:
- Years come from `utils.needed_years(buy_rules)` — only years with
  `amount_needed > 0`, so the many empty years cost zero calls.
- `utils.build_year_queries(terms, years)` → `("Penn State button 1982", 1982)`
  pairs; `ebay_client.find_year_augmented_listings` runs them and tags each
  listing with `search_year`. `config.YEAR_CRAWL_TERMS` / `YEAR_CRAWL_PSU_TERMS`
  define the base terms (Central Counties Bank omitted — yearless, general pass
  covers it; PSU terms stay category-restricted).
- Triggered by `/run-scan?year_crawl=1` (Cloud Scheduler never sends it).
  Composes with `?ignore_seen=1` / `?dry_run=1`, so the guarded
  preview→tune→live flow and the Slack digest from #20/backfill all apply.
- Each crawl result is matched with `restrict_years={search_year}`.

Why not fold the crawl into the daily scan: it's heavier (more API calls), and
the daily 9am job should stay light. Why only needed years: searching years you
don't collect is wasted quota.

---

## 23. Manual analysis was CPU-throttled (~19 min); moved into an in-flight request

A real test of `/scout` + upload (54-button lot) took **~19 minutes** to return.
CLIP was already warm ("Got it!" posts only when `vectors_loaded`), so the delay
was throttled inference, not model loading.

Root cause: `handle_message` acked the Slack event and ran `_run_manual_analysis`
in a **daemon thread**. The moment the event handler returns, no HTTP request is
in flight, so Cloud Run throttles the container's CPU to ~0%. The `_keep_cpu_hot`
spinner (#5) keeps one core *nominally* ticking but does **not** restore full
CPU — only an active inbound request does. So the encode crawled.

This is exactly the pattern the `buttonmatcher` worker avoids: it runs the encode
**synchronously inside an HTTP request** (`/internal/match`), keeping the request
open so Cloud Run guarantees CPU.

Fix (ported here): `handle_message` now fires a self-HTTP-POST to a new
`/internal/manual-analysis` route (`_dispatch_manual_analysis`) and the analysis
runs **synchronously inside that request** — same trick that already makes
`/run-scan` reliable. The Slack event handler returns immediately (3s ack
preserved); the dispatch runs in a daemon thread that only waits on the POST,
while the *work* runs in the in-flight internal request with full CPU.

Details:
- The internal route is guarded by a per-instance random token
  (`_INTERNAL_TOKEN`); since `workers=1` + `--max-instances=1`, caller and
  handler share the process so the token always matches, and it's unguessable to
  outsiders (the route is publicly reachable, like `/slack/events`).
- `config.SERVICE_BASE_URL` (env-overridable) is the self-call target — must be
  the **external HTTPS URL**. A `localhost` call bypasses the load balancer and
  does **not** prevent throttling (per `CLOUD_RUN_CPU_THROTTLE_FIX.md`).
- Inline fallback is **conditional, never a blind retry**: run inline only when
  the work definitely didn't start — a connection error (never reached the
  server) or a non-200 rejection. On a **read timeout the server-side analysis
  is still running**, so `_dispatch_manual_analysis` does NOT re-run it (that
  would double-post). Dispatch thread is `daemon=False` so it outlives the
  webhook handler; a 0.3s pause lets the ack request release its worker first;
  POST timeout is 1790s (< Cloud Run's 1800).
- **Not** fixed with `--no-cpu-throttling` — that's off budget (CLAUDE.md). The
  in-request pattern keeps us scale-to-zero.

Separately, ebayscout's CLIP is full-precision eager PyTorch (no ONNX, and
`quantize_dynamic` was removed in #12), so it is inherently slower per-encode
than the worker — but that's minutes-at-worst, not the 19 we saw. We do pin the
PyTorch thread budget (`torch.set_num_threads`/`set_num_interop_threads`,
`OMP_NUM_THREADS=2` to match `--cpu=2`) to avoid over-subscription thrash
(`CLOUD_RUN_CPU_THROTTLE_FIX.md` Part 5).

---

## 24. `/scout` confirm step: confirm count + suggested era

Inspired by the `inventory-bot` Detector, which is far more accurate because the
human supplies **count + era** up front (era pre-filters candidate years) and
reviews per-button top-3. ebayscout's `/scout` previously auto-committed to the
top-1 with no era narrowing. We added the cheapest high-value slice of that: a
**confirmation step**.

Flow (`pending_scans[user_id]["stage"]`): `await_price → previewing →
await_confirm`. After the price reply, a **preview** (mode=`preview` on the
internal route) detects the Hough count and guesses the era, then posts
*"Found ~N buttons. Era looks like X. Reply `go`, or correct it…"*. The user's
reply (`go` / a count / an era / both, e.g. `mellon 42`) sets the final count
and `restrict_years` (era → year range, reusing #22), and the **full** match
runs era-restricted.

Era detection (`clip_matcher.guess_lot_era`): per-era centroid ("era-mean")
image embeddings built from the reference vectors grouped by year; a sample of
~`ERA_SAMPLE_LIMIT` crops is classified by nearest centroid and majority-voted.
It is a **heuristic suggestion, always human-overridable** (`all` skips
restriction) — realistically "CCB-vs-not" since Mellon/Citizens look alike.

Because it's unvalidated, era detection is **heavily logged** (greppable `ERA:`
lines): per-crop centroid scores + pick, the vote tally and final guess at
preview, and the user's confirm/override + the restriction actually applied.
Filter Cloud Logging for `ERA:` to mine trends and tune over time. Config:
`BUTTON_ERAS`, `ENABLE_ERA_DETECTION`, `ERA_SAMPLE_LIMIT`. Both preview and full
run inside the in-flight internal request (#23) so neither is throttled.

**Post-result era feedback (closes the loop + labels the data).** When a
specific era was applied, the analysis is followed by a *"Did I identify the era
correctly? ✅ Yes / ❌ No — try {other era}"* prompt (Slack interactive buttons →
`era_feedback_yes` / `era_feedback_no` actions). **Yes** logs positive labelled
feedback and resolves the message (no re-run). **No** logs the miss and re-runs
the analysis restricted to "the other era" (`utils.other_era` — runner-up by
vote, else the next defined era); the re-run carries `feedback_round=True` so it
doesn't loop. Both outcomes emit `ERA: feedback …` log lines — gold-standard
labels for evaluating the classifier. Requires Slack **Interactivity** enabled
with the Request URL set to `/slack/events` (Bolt routes actions through it).

---

## 25. Era-tagged searches (tight → broad crawl)

Extends year-aware matching (#22) to the **bank era**: bake the era into the
*search query* so each result self-tags which era to match against. A query like
`Penn State Mellon button` both surfaces era-relevant listings and carries
`search_era="Mellon"`, which the scan turns into `restrict_years` (the era's year
range) — cutting cross-era misses, especially on multi-year lots.

Crawl strategy, tight → broad (run in this order; each marks `seen` so the next
skips what it caught):

1. **Year crawl** (`?year_crawl=1`, #22) — exact year, tightest.
2. **Era crawl** (`?era_crawl=1`, NEW) — **Mellon + Citizens** bank queries,
   matched restricted to the era's year range. Broader (a bank-era lot spans many
   years).
3. **Daily scan** — very broad general queries (`Penn State`/`Nittany Lions` ×
   button types) **plus the standalone `"Central Counties Bank"` query, kept in
   the general set unrestricted**. CCB buttons are the **rarest**, so that query
   runs every day with maximum broad coverage and is matched against the **full**
   slogan/reference set — deliberately **not** era-narrowed. (Earlier draft wrongly
   moved CCB into an era-restricted pass; reverted.)

Mechanics mirror the year crawl exactly: `find_listings(search_era=…)` stamps the
tag; `find_era_augmented_listings` runs `(query, era)` pairs;
`utils.build_era_queries` generates `{Penn State, PSU, Nittany Lions} × {button,
pin, badge, pinback} × {bank word}` for **Mellon and Citizens** (PSU-prefixed ones
stay category-restricted to Sports Mem). Per-listing restriction priority:
`search_year` (exact) → `search_era` (range) → single title-year → none — so the
unrestricted CCB results (no `search_era`) fall through to the full matcher.
Config: `ERA_SEARCH_PREFIXES`, `MELLON_CITIZENS_ERA_QUERIES`. `?era_crawl=1`
composes with `?dry_run=1` / `?ignore_seen=1`; Cloud Scheduler never sends it, so
the daily 9am job is unchanged.

---

## 26. Placeholder slogans demoted + the 12-button cap removed

First real crawls (log mining) exposed two issues:

**Placeholder slogans over-matched.** "Slogan Unknown N" rows exist for the buy
logic / `/scout` valuation (some priced $100), but in the scan they over-matched
in CLIP and inflated the "lot value" line (~11% of needed hits). Fix:
`config.NON_ALERTING_SLOGAN_PATTERNS = ["slogan unknown"]` +
`utils.is_non_alerting_slogan`; the scan's needed loop skips matched slogans that
hit it, so they never alert or enter `best_needed`. They stay fully active in
`get_buy_decision` / `/scout` — buy logic unchanged.

**A vestigial 12-button cap.** The automated scan called `detect_and_crop` with
no `button_count`, so `expected` defaulted to `rows*cols = 4*3 = 12` and both
truncation sites capped crops at 12. Logs showed a spike at exactly 12 with raw
Hough up to 16 — big lots lost the needed button. This was never intended.

Fix (`image_proc.detect_and_crop`, two modes keyed on `button_count`):
- **scan mode** (no count): multi-scale Hough **sweep** (`HOUGH_RADIUS_SCALES`
  large→small, merged; largest-first dedup in the existing fill-ratio loop
  removes cross-scale dups); the radius-consistency filter is **skipped** (it
  pruned the size-outlier needed button); crops capped only by a high safety
  ceiling `MAX_CROPS_PER_PHOTO = 100`, **raised when the title states a bigger
  lot** (`utils.extract_lot_count` → e.g. "Lot of 250" → 250). Per-listing total
  bounded by `MAX_CROPS_PER_LISTING = 400` (also title-raised).
- **count mode** (manual `/scout`): unchanged single-pass + half-radius fallback,
  capped at the supplied count, consistency filter kept.
- Resize raised 800 → `IMAGE_MAX_DIM = 1400` (better small-button recall; Hough
  cost only — CLIP works on 224px crops).
- `clip_matcher.match_crops_batch` now **sub-batches** encoding
  (`ENCODE_BATCH = 16`) so an uncapped dense lot can't OOM the 4Gi container;
  per-crop scoring / `top_k` / `restrict_years` / order are unchanged.
- `>>> IMAGE:` logs extended with per-scale raw counts, `cap`, and `mode` so the
  12-spike's removal and the new crop distribution are mineable.

Tuning knobs (config-only, no logic redeploy): `MAX_CROPS_PER_PHOTO`,
`MAX_CROPS_PER_LISTING`, `HOUGH_RADIUS_SCALES`, `IMAGE_MAX_DIM`, `ENCODE_BATCH`.

---

## 27. scan_log checkpointed every 50 + decade-aware year restriction

Two follow-ups from running the crawls.

**scan_log every 50.** `seen_items.json` was already checkpointed every 50
(#15), but `scan_log.jsonl` was appended only at the very end of the run — so a
30-min timeout on a big crawl lost that run's structured per-listing data
(the live `>>> TITLE/ERA/IMAGE` stdout still streamed, but the data file didn't).
Fix: append `scan_log.jsonl` every 50 alongside the seen checkpoint, tracked by a
`_scanlog_flushed` watermark (the buffer isn't cleared, so the dry-run digest
still sees the full list); the tail is flushed at the end. This now runs in
**both** live and dry-run (dry-run still does NOT write `seen_items.json` — no
dedup pollution — but it DOES persist the scan-log so a big preview survives a
timeout).

**Decade-aware restriction.** The year crawl searches e.g. `"…1990"`, and eBay
returns **"1990s" decade lots**; those were tagged `search_year=1990` and matching
hard-restricted to 1990 only — missing every 1991–1999 button in the lot. Same
bug on the title path (`extract_years("1990s")` reads 1990). Fix:
`utils.extract_decades(title)` ("1990s"/"90s"/"1990's" → the 10-year set); the
scan broadens the restriction to the decade when a decade marker is present
(year-crawl: `{search_year} | decade`; general: the decade). A plain single year
("…1990" with no `s`) still restricts to that one year.

## 28. Unified buttonmatcher scoring + green/auto-only confirmation + per-event logging + `/crawl500`

A major convergence onto buttonmatcher's pipeline, plus a new on-demand search.

**Exact scoring parity.** ebayscout's matcher had drifted (`ALPHA=0.7/BETA=0.3`,
a second 0.75–0.9 boost tier, no rarity tiebreaker, top-5 year selection). It now
mirrors buttonmatcher's `score_slogans` / `match_logging.build_leaderboard`
**exactly**: `ALPHA=BETA=0.5`, a single `>0.9` text boost, the `<0.3` penalty, the
capped (`≤0.04`) rarity tiebreaker (`scoring.py` — `tokenize`/`STOPWORDS`/
`rarity_weight`, with `word_freq` built in `clip_matcher.init()`), and dual-signal
year selection (top-3 image years ∪ top-3 text years). This makes the calibrated
confidence tiers transfer directly and guarantees logged leaderboards == live
scores.

**Green/auto-only confirmation.** The recall-biased `NEEDED_MATCH_THRESHOLD=0.60`
(/0.45 title-corroborated) bar is replaced by a precision gate: a crop is an
identified button only when its top match is **auto** (`overall ≥ 0.85`) or **green**
(`overall ≥ 0.82`, or its #1 leads #2 by `≥ 0.12`) — `scoring.is_confirmed`. Among
confirmed buttons, `amount_needed > 0` (placeholders suppressed) marks a needed
lot. Daily-scan tallies became `alerted / confirmed_not_needed / rejected`. This
trades recall for precision deliberately (fewer daily alerts).

**buttonmatcher logging, written PER EVENT.** `match_logging.py` is copied
verbatim from buttonmatcher (same cross-bot module) and writes to the shared
`LOGGER_ID` workbook (`match_log` + `confirm_log`, `service="ebayscout"`). Every
crop gets a `match_log` row (detection diag + restricted_top + shadow leaderboard
via `match_crops_with_diagnostics`); every auto/green crop gets a `confirm_log`
row (`source=auto_resolve|green`, `rank_shadow` = the headline automation metric).
**Rows are flushed per image / per confirmation, never buffered to the end**, so a
CPU-throttle/timeout/crash never discards already-processed work — the one
intentional delta from the verbatim module is a bounded 429 retry+backoff in
`SheetLogger`. The goal is thousands of rows to inform future automation.

**`/crawl500` (on-demand2).** New Slack slash command (served at the new
`/slack/events` Bolt route, signed with `SIGNING_SECRET_ES`). It acks in <3s and
kicks `/internal/crawl500` through the load balancer (`SERVICE_URL`, per-startup
`X-Internal-Secret` — mirrors buttonmatcher `/internal/match`) so the heavy run
gets a fresh CPU-funded request. The user-stipulated search
`"Penn State" AND (Citizens OR Mellon OR "Central Counties") AND (button* OR pin*
OR Badge*)` is OR-expanded into 3 banks × 4 button-types = 12 explicit queries
(Browse `q` has no reliable boolean/wildcard support and `find_listings` doesn't
paginate), deduped and **capped at 500 lots**. **No seller exclusion**; the
apparel-keyword + Clothing-category noise filters stay on. Its **first run** may
re-scan already-seen lots to reach 500 (tracked by `ONDEMAND2_STATE_BLOB`); every
run after processes only unseen lots. Cost: up to 500 eBay+CLIP lots per run —
treat like `?year_crawl=1`.

## Remaining known issues

| Issue | Status | Notes |
|-------|--------|-------|
| CLIP accuracy on multi-button photos | Mitigated (#21, #22, #26) | Scan flags *needed-button candidates* (recall-biased, multi-photo) for `/scout` review. Year-aware matching (#22) removes most year-confusion; the 12-crop cap is gone and detection is multi-scale (#26). Tune `NEEDED_MATCH_THRESHOLD` from `SCAN_LOG_BLOB` data. |
| Dense-lot detection of small buttons | Open (improved #26) | Multi-scale Hough + 1400px resize catch far more than the old 12 cap, but truly tiny buttons in a 100+ photo may still be missed; the radius sweep / `IMAGE_MAX_DIM` can be pushed further if logs show misses. |
| kling24toys seller filter | Open | Listed in `EXCLUDED_SELLERS` but need to confirm actual eBay username matches after new scan logs show seller names in brackets. |
| Manual upload "still loading" loop | Fixed (#20) | `/scout` slash command + self-healing `handle_file_shared`. Needs the `/scout` command registered in Slack and validated in production. |
| Automated undervalued-lot valuation | Deferred (#21) | `ENABLE_UNDERVALUED_ALERTS=False`. Revisit once `scan_log.jsonl` shows whether per-lot valuation from photos is trustworthy. |


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

---

## 29. `/crawl <N>` consolidates `/crawl10` + `/crawl500`; two-worker Gem pipeline

**`/crawl <N>` (one parameterized command).** The two fixed-size on-demand
commands were merged into a single Slack command that takes the lot count:
`@app.command("/crawl")` parses `N` from the command text, validates
`1 … config.CRAWL_MAX_LOTS_CAP` (=`1000`) — junk or out-of-range gets a usage
message and **no** run, so a fat-finger can't launch a 50k-lot paid run — then
kicks `/internal/crawl?n=N` (synchronous in-request, CPU-funded, same pattern as
before). `_run_crawl500()` became `_run_crawl(n)`: identical on-demand2
**seen-aware** search (`CRAWL500_QUERIES`, Citizens/Mellon/Central-Counties),
capped at the caller's `N`; first-run marker still allows already-seen lots on the
first run, unseen-only thereafter. Removed: `_run_crawl10`, the `/crawl10` +
`/crawl500` command handlers, `/internal/crawl10` + `/internal/crawl500` routes,
and `CRAWL10_*` config (`CRAWL500_QUERIES`/`BANKS`/`MAX_LOTS` kept; `MAX_LOTS` is
now a historical default since the cap is the user's `N`). The `/crawl` tag flows
through `process_pipeline_lot` (detection mode, `pipeline_command` default,
confirm-log tags). Operator must register the `/crawl` slash command in Slack
(Request URL = existing `/slack/events`); old `/crawl10`/`/crawl500` can be removed.

Rationale: one command, one mental model, with a hard 1–1000 guard — instead of
two hardcoded sizes (and the temptation to keep adding `/crawlNNN` variants).

**Two-worker Gem pipeline.** Throughput scales by running one `watcher.py` per
Gemini account: every worker polls the same `pipeline/input/` but processes only
its `crc32(filename) % worker_count == worker_index` shard (no overlap, no
locking). Each worker needs its **own** Chrome profile (`user_data_dir`,
absolute + unique) pre-authenticated to its account — Google blocks sign-in in the
automation browser, so you log the profile in with real Chrome once and the
watcher reuses the session. The Gem must emit **strictly valid JSON**; slogans
with quotation marks must use single quotes inside the value (a raw `"` such as
`PSU Dots The "I" In Win` breaks `json.loads`, leaving the lot to retry until the
per-worker `gem_empty_limit` halt). Full schema lives in
`PIPELINE_WATCHER_CONTRACT.md`.
