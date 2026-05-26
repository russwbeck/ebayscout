# eBay Button Scout — Deployment Guide

A Cloud Run **Job** (not a service) that scans eBay daily for Penn State
football button lots and posts Slack alerts for undervalued lots and listings
containing buttons you still need.

Estimated cost: **~$1/month** (Cloud Run Job + Cloud Scheduler).

---

## Prerequisites

### 1. eBay Developer Account (free)
1. Register at https://developer.ebay.com
2. Sign in → **My Account → Application Keys**
3. Create a **Production** app
4. Copy the **App ID (Client ID)** — this is your `EBAY_APP_ID`

> Only the App ID is needed. The Finding API and Shopping API are both
> free and require no OAuth token — just the App ID as a query parameter.

### 2. Slack App
1. Go to https://api.slack.com/apps → **Create New App → From Scratch**
2. Name: `eBay Scout`  Workspace: your workspace
3. Go to **OAuth & Permissions → Scopes → Bot Token Scopes** → add `chat:write`
4. **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-...`)
5. Invite the bot to your `#ebay-scout` channel: `/invite @eBay Scout`

### 3. GCP Secrets (add to existing Secret Manager)
```bash
# New secrets — EBAY_BOT_TOKEN, SIGNING_SECRET_ES, CHANNEL_ID_EBAY
# already created via the GCP Console.
# Once eBay API approval arrives, add the App ID:
echo -n "YOUR_EBAY_APP_ID" | gcloud secrets create EBAY_APP_ID --data-file=-

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
for SECRET in EBAY_APP_ID EBAY_BOT_TOKEN CHANNEL_ID_EBAY GOOGLE_SHEETS_JSON SPREADSHEET_ID; do
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

## Create the Cloud Run Job
```bash
gcloud run jobs create ebay-scout \
  --image=us-east1-docker.pkg.dev/${PROJECT_ID}/buttons/ebayscout:latest \
  --region=us-east1 \
  --task-count=1 \
  --max-retries=1 \
  --task-timeout=600s \
  --memory=2Gi \
  --cpu=2 \
  --service-account=${SA}
```

---

## Create the Cloud Scheduler Trigger (daily 9 AM ET)
```bash
# Grant Scheduler permission to invoke the job
gcloud run jobs add-iam-policy-binding ebay-scout \
  --member="serviceAccount:${SA}" \
  --role="roles/run.invoker" \
  --region=us-east1

gcloud scheduler jobs create http ebay-scout-daily \
  --location=us-east1 \
  --schedule="0 9 * * *" \
  --time-zone="America/New_York" \
  --uri="https://us-east1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/ebay-scout:run" \
  --http-method=POST \
  --oauth-service-account-email=${SA}
```

---

## Cloud Build Trigger (auto-deploy on push)
1. GCP Console → **Cloud Build → Triggers → Create Trigger**
2. Settings:
   - Name: `ebayscout-job`
   - Event: Push to branch `main`
   - Included files filter: `ebayscout/**`
   - Build configuration: `ebayscout/cloudbuild.yaml`
3. Save

---

## Smoke Test (before enabling the scheduler)
```bash
# Set DRY_RUN = True in ebayscout/config.py, then rebuild + push, then:
gcloud run jobs execute ebay-scout --region=us-east1 --wait

# Check logs
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=ebay-scout" \
  --limit=100 --format=json | jq '.[].textPayload'
```

Verify in the logs:
- At least one listing fetched from eBay
- CLIP matching ran without errors
- `[DRY RUN]` lines appear instead of Slack posts
- `seen_items.json` is NOT written

After a successful smoke test:
1. Set `DRY_RUN = False` in `ebayscout/config.py`
2. Rebuild + push the image
3. Re-run manually once to confirm Slack messages appear in `#ebay-scout`
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
