"""
ebayscout/config.py

All tuneable constants for the eBay Button Scout batch job.
Edit EXCLUDED_SELLERS and SLACK_SCOUT_CHANNEL before first deploy.
"""

import os

from .utils import build_era_queries

# --- GCP ---
BUCKET_NAME    = "60d488c5-9c8e-4acc-aac-button-data"
PROJECT_NUMBER = "404960106109"

# --- Service base URL (this Cloud Run service's own public URL) ---
# Used to build the eBay account-deletion endpoint URL (EBAY_DELETION_ENDPOINT
# below). Must match the deployed service URL. Override at runtime with the
# SERVICE_BASE_URL env var.
SERVICE_BASE_URL = os.environ.get(
    "SERVICE_BASE_URL", "https://ebay-scout-404960106109.us-east1.run.app"
)

# --- eBay Marketplace Account Deletion endpoint ---
# This URL must match exactly what is registered in the eBay Developer Portal
# (developer.ebay.com → Application Keys → Notifications).
# The verification token is stored in GCP Secret Manager as
# EBAY_DELETION_VERIFICATION_TOKEN (32-80 chars, alphanumeric + _ -)
EBAY_DELETION_ENDPOINT = f"{SERVICE_BASE_URL}/ebay/account-deletion"

# --- Button eras (bank sponsor → year range) ---
# Used to narrow CLIP matching to a single era (restrict_years). Note: Central
# Counties buttons look visually distinct and separate well; Mellon vs Citizens
# look similar, so era auto-detection is realistically "CCB vs not" — it's always
# offered as a human-overridable suggestion, never an auto-lock. 2001 overlaps
# Mellon/Citizens intentionally.
BUTTON_ERAS: dict = {
    "Central Counties": (1972, 1983),
    "Mellon":           (1984, 2001),
    "Citizens":         (2001, 2026),
}
ENABLE_ERA_DETECTION = True
ERA_SAMPLE_LIMIT     = 5    # crops encoded to guess the lot era at the preview step

# --- CLIP scoring (match buttonmatcher's constants EXACTLY) ---
# ebayscout now mirrors buttonmatcher's unified scorer so its confidence tiers
# (GREEN/AUTO below) transfer directly. ALPHA=BETA=0.5 with a single >0.9 text
# boost, a <0.3 text penalty, and a rarity tiebreaker (see clip_matcher.py).
CONFIDENCE_THRESHOLD      = 0.72   # legacy band marker (kept for scan_log/back-compat)
REJECTION_THRESHOLD       = 0.45   # legacy band marker (kept for scan_log/back-compat)
ALPHA                     = 0.5   # image weight  (matches buttonmatcher score_slogans)
BETA                      = 0.5   # text weight   (matches buttonmatcher score_slogans)
SLOGAN_PENALTY_THRESHOLD  = 0.3   # below this slogan_score → penalise overall
PENALTY_MULTIPLIER        = 0.7

# --- Confidence tiers (calibrated on buttonmatcher's 0.5/0.5 + boost + penalty
# score; copied verbatim from buttonmatcher/main.py). A crop is "confirmed" only
# when its top match is AUTO (>=0.85) or GREEN (>=0.82, or #1 leads #2 by >=0.12).
AUTO_RESOLVE_THRESHOLD    = 0.85   # top-1 at/above this auto-confirms
GREEN_THRESHOLD           = 0.82   # solo-score green: 0 wrong above this in data
GREEN_GAP                 = 0.12   # a #1 leading #2 by this much earns green too
RED_THRESHOLD             = 0.65   # below this = low confidence (35% wrong in data)

# Always-on entry-level reference-photo visual check (buttonmatcher's REF_CHECK
# step): for each candidate, nudge `overall` by REF_CHECK_WEIGHT * (the crop's max
# similarity to THAT entry's reference photos), then re-sort. Mirrors
# buttonmatcher/main.py REF_CHECK_WEIGHT.
REF_CHECK_WEIGHT          = 0.15

# Auto-staging gate (Gemini pipeline). A crop is copied straight into
# reference/_staging (for buttonmatcher's /reference review) when it cleared real
# Hough detection AND Gemini confirmed its slogan (independent agreement). The
# CLIP `overall` is NO LONGER a confidence gate — log analysis (Logger_5) showed a
# score threshold is the wrong safety lever (a 0.968 visual-twin still matched
# wrong, while a 0.82 floor captured ~3.5x more good crops). Gemini agreement is
# the safety; this value is only a tiny junk floor (ignore sub-~0.5 noise).
STAGE_CONF = 0.50

# --- GCS dedup file ---
SEEN_ITEMS_BLOB = "ebay_scout/seen_items.json"

# --- on-demand2 (/crawl500) run-state marker ---
# Tracks whether /crawl500 has completed its first run. On the FIRST run it may
# re-scan already-seen lots to reach the 500 cap; on every run after it processes
# only unseen lots (still capped at 500). See seen_items.py load/save helpers.
ONDEMAND2_STATE_BLOB = "ebay_scout/ondemand2_state.json"

# --- eBay Browse API ---
# Replaces the Finding + Shopping APIs, both decommissioned by eBay 2025-02-05.
# Browse requires an OAuth application token (client-credentials grant) built
# from the App ID (client id) + Cert ID (client secret).
EBAY_OAUTH_URL         = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_OAUTH_SCOPE       = "https://api.ebay.com/oauth/api_scope"
EBAY_BROWSE_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_BROWSE_ITEM_URL   = "https://api.ebay.com/buy/browse/v1/item"
EBAY_MAX_RESULTS  = 100          # per query; Browse page size limit is 200
MAX_PHOTOS_PER_LISTING = 4       # photos scored per listing (was 1); presence
                                 # detection benefits from per-button photos

# --- Needed-button detection (recall-biased) ---
# The daily scan's job is to flag listings that *plausibly* contain a button
# still needed (amount_needed > 0 in the sheet) so the human can value it with
# the /scout slash command. This threshold is intentionally below
# CONFIDENCE_THRESHOLD (0.72) to favour recall — false pings are cheap because
# the human reviews each one; a missed needed button is the costly outcome.
NEEDED_MATCH_THRESHOLD = 0.60
# Number of candidate matches inspected per crop (the matcher computes top-3
# internally). A needed button is often the 2nd/3rd guess on a blended photo.
NEEDED_MATCH_TOP_K     = 3

# Slogans that must NOT trigger scan alerts even when amount_needed > 0. These
# are placeholder/unknown rows that exist for the buy logic + /scout valuation
# but over-match in CLIP and inflate "lot value" in the scan. Case-insensitive
# substring match. They stay fully active in get_buy_decision / /scout.
NON_ALERTING_SLOGAN_PATTERNS = ["slogan unknown"]

# --- Button detection (image_proc.detect_and_crop) ---
# Safety ceiling on crops returned for ONE photo in the automated scan (no
# explicit button_count). It is NOT a feature limit — the old behaviour
# accidentally capped at 12 (a 4x3 grid default). 100 is far above any real lot;
# it only guards against a noise-storm image hallucinating thousands of circles.
# Raised automatically when the listing title states a larger count.
MAX_CROPS_PER_PHOTO   = 100
MAX_CROPS_PER_LISTING = 400      # ceiling across all photos of one listing
IMAGE_MAX_DIM         = 1400     # resize longest side (was 800); better small-button recall
HOUGH_RADIUS_SCALES   = (1.0, 0.66, 0.45, 0.30)  # multi-scale sweep, large→small
HOUGH_MIN_RADIUS_PX   = 9        # floor so we don't chase speckle
ENCODE_BATCH          = 16       # sub-batch size for CLIP image encoding

# --- Undervalued-lot alerts (deferred / opt-in) ---
# Precise auto-valuation of a whole lot is unreliable without per-button
# segmentation, so the undervalued/margin alert is OFF by default. The
# needed-button path above is the headline. Flip to True once the scan-log data
# (SCAN_LOG_BLOB) shows valuation is trustworthy.
ENABLE_UNDERVALUED_ALERTS = False

# --- Scan log (groundwork for a future automated valuer) ---
# One JSON line per processed listing, appended to this GCS blob: title, asking
# price, photos scored, top matches + scores, needed-hit / alerted flags.
SCAN_LOG_BLOB = "ebay_scout/scan_log.jsonl"

# --- ID hunt list (rebuild the market DB from known eBay IDs) ---
# A JSON array of eBay item_ids (e.g. recovered from a prior run's logs) that
# the year crawl / hunt mode fetches directly by ID to backfill full per-listing
# market data (asking price, condition, format) the original run never stored.
# Upload the list to this blob; /run-scan?hunt_ids=1 (or ?year_crawl=1) reads it.
HUNT_IDS_BLOB = "ebay_scout/hunt_ids.json"

# How many hunt IDs the ordinary DAILY scheduled run drains per day (auto, in
# the background of the normal scan). The daily run already happens, so this
# amortises a big ID backlog over several days for free, bounded so it never
# nears the request CPU window, and self-stops once every ID has been processed
# (seen). 0 disables auto-draining (then hunt only via explicit ?hunt_ids=1).
DAILY_HUNT_BUDGET = 50

# --- eBay sellers to exclude (exact username, case-insensitive) ---
EXCLUDED_SELLERS: list[str] = [
    "kling24toys",
    "gertb2002",
    "wearepinstate",
]

# --- Etsy sellers to exclude (shop_name, case-insensitive) ---
ETSY_EXCLUDED_SELLERS: list[str] = []

# --- Listing title keywords that indicate apparel/non-button items ---
# Any listing whose title contains one of these words (case-insensitive) is
# skipped by both the eBay and Etsy clients before CLIP processing.
EXCLUDED_KEYWORDS: list[str] = [
    # apparel
    "embroidered",
    "drifit",
    "hoodie",
    "sweatshirt",
    "stitched",
    "polo",
    "quarterzip",
    "quarter zip",
    "quarter-zip",
    "denim",
    "antigua",
    "jacket",
    "pullover",
    "shirt",
    "jersey",
    "vest",
    # accessories / non-pin items
    "enamel",
    "enameled",
    "brooch",
    "lanyard",
    "strap",
    "ornament",
    "christmas",
    # clearly non-button objects
    "wooden",
    "cable",
    "badge reel",
    "map",
    "sticker",
    "decal",
]

# --- eBay category IDs to exclude entirely ---
# itemSummary.categories includes the full ancestry (top-level → leaf), so a
# listing anywhere under one of these top-level categories is dropped before
# CLIP processing. 11450 = "Clothing, Shoes & Accessories" (≈20% of the noise
# in Penn State button searches).
EXCLUDED_CATEGORY_IDS: list[str] = ["11450"]

# --- Multi-query search strategy ---
# "Central Counties Bank" runs standalone (no button-type suffix) because it is a
# low-frequency, precise term that collides with noise when combined with other words.
BUTTON_TYPES  = ["button", "pin", "badge", "pinback"]

# eBay category 64482 = "Sports Mem, Cards & Fan Shop" (top-level sports
# memorabilia). Restricting PSU queries to this category keeps Penn State
# University results while dropping the Power Supply Unit electronics noise
# that floods unrestricted "PSU button/pin/badge/pinback" searches.
SPORTS_MEMO_CATEGORY_ID = "64482"

# Unrestricted ("very broad") queries — "Penn State" and "Nittany Lions" are
# unambiguous. "Central Counties Bank" STAYS here, unrestricted, and runs every
# day: CCB buttons are the rarest, so we want maximum broad coverage on them
# (matched against the full slogan/reference set, not era-narrowed).
EBAY_SEARCH_QUERIES: list[str] = (
    [f"Penn State {btn}" for btn in BUTTON_TYPES]
    + [f"Nittany Lions {btn}" for btn in BUTTON_TYPES]
    + ["Central Counties Bank"]
)
# Produces 9 queries.

# --- Era-named searches (bake the bank era into the query → restrict matching) ---
# Mellon + Citizens only. Each (query, era_label) result is tagged search_era and
# matched restricted to that era's year range (see BUTTON_ERAS). Run ON-DEMAND via
# /run-scan?era_crawl=1 (broader, multi-year within an era; the tight year crawl
# runs first and marks listings seen). Central Counties is deliberately NOT here —
# it stays in the always-on general queries above. Prefixes include Nittany Lions.
ERA_SEARCH_PREFIXES = ["Penn State", "PSU", "Nittany Lions"]

MELLON_CITIZENS_ERA_QUERIES: list[tuple[str, str]] = (
    build_era_queries(ERA_SEARCH_PREFIXES, BUTTON_TYPES, "Mellon", "Mellon")
    + build_era_queries(ERA_SEARCH_PREFIXES, BUTTON_TYPES, "Citizens", "Citizens")
)

# PSU queries run with category_ids=SPORTS_MEMO_CATEGORY_ID so "PSU" matches
# Penn State University buttons rather than Power Supply Units.
PSU_SEARCH_QUERIES: list[str] = [f"PSU {btn}" for btn in BUTTON_TYPES]
# Produces 4 queries: "PSU button", "PSU pin", "PSU badge", "PSU pinback"

# --- on-demand2 (/crawl) search ------------------------------------------------
# User-stipulated search, distinct from everything above:
#   "Penn State" AND (Citizens OR Mellon OR "Central Counties")
#                AND (button* OR pin* OR Badge*)
# The eBay Browse `q` parameter has no reliable boolean/wildcard support, and
# find_listings() does not paginate past one <=200 window, so we OR-expand the
# query into one explicit phrase per (bank x button-type) and dedup.
# NO seller exclusion (see /internal/crawl); the apparel-keyword + Clothing
# category noise filters stay on.
CRAWL500_BANKS: list[str] = ["Citizens", "Mellon", "Central Counties"]
CRAWL500_QUERIES: list[str] = [
    f"Penn State {bank} {btn}"
    for bank in CRAWL500_BANKS
    for btn in BUTTON_TYPES
]
# Produces 3 banks x 4 button-types = 12 queries.
CRAWL500_MAX_LOTS = 500   # historical default; the /crawl command now takes N.

# Hard upper bound on the N accepted by the `/crawl <N>` slash command. A bigger
# number is a costly paid run (eBay + CLIP), so this guards against a fat-finger
# like `/crawl 50000` — the handler rejects N outside 1..CRAWL_MAX_LOTS_CAP.
CRAWL_MAX_LOTS_CAP = 1000

# --- Gemini → GCS pipeline (Drive watcher → Gem → GCS → /pipeline/notify) -----
# Google Drive folder the external watcher polls; /crawl10 uploads primary lot
# photos here. Set DRIVE_FOLDER_ID in the deploy env. The Drive service-account
# key is stored in Secret Manager as DRIVE_SA_JSON (scope drive.file; the folder
# must be shared with the SA's email).
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")
# Filename/object-name prefix that routes pipeline outputs to ebayscout (vs
# buttonmatcher) — both services share the bucket's pipeline/output/ prefix.
PIPELINE_OBJECT_PREFIX = "ebayscout__"
# GCS prefix where the Gem writes <f>.png + <f>.png.response.json.
PIPELINE_OUTPUT_PREFIX = "pipeline/output/"
# GCS prefix where /crawl10 drops each lot's primary photo for the watcher to
# pick up (a service account can't own Drive files on personal Gmail — "no
# storage quota" — so ebayscout feeds the pipeline via GCS instead of Drive).
PIPELINE_INPUT_PREFIX = "pipeline/input/"
# Per-lot correlation context written at upload, read when the async result
# returns (one small JSON blob per key; survives cold start).
PENDING_CONTEXT_PREFIX = "ebay_scout/pending/"
# Temp holding area for auto-confirmed crops awaiting the Yes/No reference vote.
PIPELINE_CROPS_PREFIX  = "ebay_scout/pipeline_crops/"
# Shared reference staging area buttonmatcher's /reference flow consumes. On a
# Yes vote ebayscout copies crop FILES here (it never writes vectors.pt).
REFERENCE_STAGING_PREFIX = "reference/_staging/"
# Stale pending/crop blobs older than this many days are swept (never-returned
# Gem reads, abandoned Yes/No prompts).
PIPELINE_TTL_DAYS = 7

# --- Year-augmented deep-crawl terms (on-demand /run-scan?year_crawl=1) ---
# Base terms that get a year appended (e.g. "Penn State button 1982") for each
# needed year. "Central Counties Bank" is omitted — it's yearless and covered by
# the general pass. PSU terms run category-restricted like the general scan.
YEAR_CRAWL_TERMS: list[str] = (
    [f"Penn State {btn}" for btn in BUTTON_TYPES]
    + [f"Nittany Lions {btn}" for btn in BUTTON_TYPES]
)
YEAR_CRAWL_PSU_TERMS: list[str] = list(PSU_SEARCH_QUERIES)

# --- Dry-run mode (set True for smoke testing) ---
# When True: the scan runs end-to-end but fires no real alerts and does NOT
# write seen_items.json (no dedup pollution) — it logs "[DRY RUN]" lines and
# posts a single preview digest of needed-button candidate scores to the scout
# Slack channel (for tuning NEEDED_MATCH_THRESHOLD). It DOES persist
# scan_log.jsonl (checkpointed every 50) so a big preview's per-listing data
# survives the 30-min request cap. Can be overridden per-request with
# /run-scan?dry_run=1.
DRY_RUN = False
