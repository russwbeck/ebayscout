# eBay Button Scout — Deployment Guide

A Cloud Run **Service** (gunicorn + Flask, entry point `ebayscout/main.py`)
that scans eBay + Etsy for Penn State football button lots and posts Slack
alerts for undervalued lots and listings containing buttons you still need.

The daily scan is triggered by **Cloud Scheduler → `POST /run-scan`**. The same
service also handles Slack file-upload events (`/slack/events`) and the eBay
Marketplace Account Deletion endpoint (`/ebay/account-deletion`).

Estimated cost: **~$1/month** (Cloud Run + Cloud Scheduler).

> **eBay API note:** This bot uses the eBay **Browse API**. The legacy Finding
> and Shopping APIs were decommissioned by eBay on **2025-02-05** and no longer
> work. The Browse API requires an OAuth application token built from your
> **App ID (client id)** + **Cert ID (client secret)** — see below.

---

## Prerequisites

### 1. eBay Developer Account (free)
1. Register at https://developer.ebay.com
2. Sign in → **My Account → Application Keys**
3. Create a **Production** app (or use an existing one)
4. Copy the **App ID (Client ID)** → secret `EBAY_APP_ID`
5. Copy the **Cert ID (Client Secret)** → secret `EBAY_CERT_ID`

> The Browse API uses the OAuth **client-credentials** grant: the bot exchanges
> the App ID + Cert ID for a short-lived application access token at runtime
> (scope `https://api.ebay.com/oauth/api_scope`). No user login or refresh
> token is involved. The default application scope is sufficient for
> `item_summary/search`.

### 2. Slack App
1. Go to https://api.slack.com/apps → **Create New App → From Scratch**
2. Name: `eBay Scout`  Workspace: your workspace
3. Go to **OAuth & Permissions → Scopes → Bot Token Scopes** → add `chat:write`
4. **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-...`)
5. Invite the bot to your `#ebay-scout` channel: `/invite @eBay Scout`

The manual-upload flow (upload a photo → get a lot valuation) also needs the
Events API and a slash command pointed at the same `/slack/events` endpoint
(Slack Bolt routes both events and slash commands through it):

6. **Event Subscriptions → Enable Events**
   - Request URL: `https://ebay-scout-404960106109.us-east1.run.app/slack/events`
   - **Subscribe to bot events:** add `file_shared` and `message.channels`
     (the file upload event and the price/source reply).
7. **Slash Commands → Create New Command**
   - Command: `/scout`
   - Request URL: `https://ebay-scout-404960106109.us-east1.run.app/slack/events`
   - Short description: `Wake eBay Scout for manual photo analysis`
8. **Reinstall to Workspace** (Slack requires a reinstall whenever event
   subscriptions or slash commands change).

> **Why `/scout` exists.** CLIP (~30-60s to load) hydrates in a background
> thread, but Cloud Run throttles CPU to ~0% between requests, so on a cold/idle
> container that thread can stall and uploads would otherwise sit on "waking
> up". Running `/scout` forces a synchronous load *inside* a request (where CPU
> is guaranteed). Uploading a photo also auto-triggers the same wake, so `/scout`
> is a convenience, not a hard requirement.

### 3. GCP Secrets (add to existing Secret Manager)
```bash
# New secrets — EBAY_BOT_TOKEN, SIGNING_SECRET_ES, CHANNEL_ID_EBAY
# already created via the GCP Console.

# eBay Browse API credentials (both required):
echo -n "YOUR_EBAY_APP_ID"  | gcloud secrets create EBAY_APP_ID  --data-file=-
echo -n "YOUR_EBAY_CERT_ID" | gcloud secrets create EBAY_CERT_ID --data-file=-

# The following already exist from buybot — no action needed:
# GOOGLE_SHEETS_JSON, SPREADSHEET_ID
```

> **Note:** `SIGNING_SECRET_ES` is not used by the batch job (it's only
> needed for a Slack server that receives and verifies incoming events).
> It's safe to leave it in Secret Manager for future use.

### 4. Service Account
```bash
PROJECT_ID="your-project-id"

gcloud iam service-accounts create ebay-scout-sa \
  --display-name="eBay Scout Job SA"

SA="ebay-scout-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# GCS access (read vectors, read/write seen_items.json)
gsutil iam ch serviceAccount:${SA}:objectAdmin \
  gs://60d488c5-9c8e-4acc-aac-button-data

# Secret Manager access
for SECRET in EBAY_APP_ID EBAY_CERT_ID EBAY_BOT_TOKEN CHANNEL_ID_EBAY GOOGLE_SHEETS_JSON SPREADSHEET_ID; do
  gcloud secrets add-iam-policy-binding ${SECRET} \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor"
done
```

---

## Artifact Registry

If the `buttons` repository doesn't exist yet:
```bash
gcloud artifacts repositories create buttons \
  --repository-format=docker \
  --location=us-east1 \
  --description="Penn State button bot images"
```

---

## First Build

Build and push the image manually for the first deploy (Cloud Build trigger
handles subsequent deploys automatically):
```bash
cd /path/to/buybot   # repo root

docker build \
  -f ebayscout/Dockerfile \
  -t us-east1-docker.pkg.dev/${PROJECT_ID}/buttons/ebayscout:latest \
  .

gcloud auth configure-docker us-east1-docker.pkg.dev

docker push us-east1-docker.pkg.dev/${PROJECT_ID}/buttons/ebayscout:latest
```

---

## Create the Cloud Run Service
```bash
gcloud run deploy ebay-scout \
  --image=us-east1-docker.pkg.dev/${PROJECT_ID}/buttons/ebayscout:latest \
  --region=us-east1 \
  --memory=4Gi \
  --cpu=2 \
  --timeout=1800 \
  --no-allow-unauthenticated \
  --service-account=${SA}
```

> **`--timeout=1800` matters.** `/run-scan` runs the scan **synchronously** and
> returns 200 only when it finishes — this is what keeps Cloud Run's CPU
> allocated for the whole scan (a background thread gets throttled to ~0%).
> The request timeout (max 3600s) must comfortably exceed a full scan.

> Subsequent deploys are handled automatically by the Cloud Build trigger
> (see below) — you only run this manually for the first deploy.

> **Optional — eliminate the cold-start wait (higher cost).** By default the
> service scales to zero, so the first upload after idle (or right after a
> deploy) costs a ~60s CLIP wake (handled by `/scout` and the self-healing
> upload flow). To make it instant instead, add these two flags to the
> `gcloud run deploy` args here **and** in `cloudbuild.yaml`:
> ```
>   --min-instances=1        # keep one container warm (no scale-to-zero)
>   --no-cpu-throttling      # let the background CLIP loader finish between requests
> ```
> Trade-off: `--min-instances=1` bills for one always-on 4Gi/2-vCPU instance
> (well above the ~$1/month idle cost). Keep `--max-instances=1` either way —
> manual `pending_scans` state lives in a single container's memory.

---

## Create the Cloud Scheduler Trigger (daily 9 AM ET)
The scheduler calls the service's `/run-scan` endpoint, which kicks off the
daily scan in a background thread and returns 200 immediately.
```bash
SERVICE_URL=$(gcloud run services describe ebay-scout \
  --region=us-east1 --format='value(status.url)')

# Grant Scheduler permission to invoke the service
gcloud run services add-iam-policy-binding ebay-scout \
  --member="serviceAccount:${SA}" \
  --role="roles/run.invoker" \
  --region=us-east1

gcloud scheduler jobs create http ebay-scout-daily \
  --location=us-east1 \
  --schedule="0 9 * * *" \
  --time-zone="America/New_York" \
  --uri="${SERVICE_URL}/run-scan" \
  --http-method=POST \
  --attempt-deadline=1800s \
  --oidc-service-account-email=${SA} \
  --oidc-token-audience="${SERVICE_URL}"
```

> `--attempt-deadline=1800s` (the max for HTTP targets) gives the synchronous
> scan time to finish before Scheduler considers the attempt failed. If a scan
> ever exceeds it, Scheduler may retry — but `/run-scan` holds a lock and
> returns 409 to an overlapping trigger, so a retry can't start a second
> concurrent scan.

---

## Cloud Build Trigger (auto-deploy on push)
1. GCP Console → **Cloud Build → Triggers → Create Trigger**
2. Settings:
   - Name: `ebayscout-job`
   - Event: Push to branch `main`
   - Included files filter: `ebayscout/**`
   - Build configuration: `ebayscout/cloudbuild.yaml`
3. Save

> **One deploy path only.** `cloudbuild.yaml` builds, pushes, and runs
> `gcloud run deploy` — this Cloud Build trigger is the single source of
> deploys. (A previous `deploy-ebayscout.yml` GitHub Action that also ran
> builds was removed to stop two concurrent builds racing on every push.)

---

## Smoke Test (before enabling the scheduler)
```bash
# Set DRY_RUN = True in ebayscout/config.py, push (auto-deploys), then
# trigger a scan manually against the running service:
SERVICE_URL=$(gcloud run services describe ebay-scout \
  --region=us-east1 --format='value(status.url)')

curl -X POST "${SERVICE_URL}/run-scan" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)"

# Check logs
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=ebay-scout" \
  --limit=100 --format=json | jq '.[].textPayload'
```

Verify in the logs:
- An eBay OAuth token was obtained (no `EBAY AUTH` errors)
- At least one listing fetched from eBay
- CLIP matching ran without errors
- `seen_items.json` is NOT written

After a successful smoke test:
1. Set `DRY_RUN = False` in `ebayscout/config.py`
2. Push (auto-deploys)
3. `POST /run-scan` once to confirm Slack messages appear in `#ebay-scout`
4. Verify `ebay_scout/seen_items.json` appears in the GCS bucket
5. Re-run immediately — confirm no duplicate Slack messages (dedup working)

---

## Updating the Excluded Sellers List

Edit `ebayscout/config.py`:
```python
EXCLUDED_SELLERS: list[str] = [
    "kling24toys",
    "another_seller",   # add more here
]
```
Push to `main` → Cloud Build automatically rebuilds and updates the job image.

---

## Monitoring

- **Cloud Logging**: GCP Console → Logging → filter `resource.type=cloud_run_job`
- **Cloud Run Job history**: GCP Console → Cloud Run → Jobs → `ebay-scout` → Executions
- **Scheduler**: GCP Console → Cloud Scheduler → `ebay-scout-daily`

If `seen_items.json` save fails, the bot posts a warning to `#ebay-scout`.
