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
# Used for the manual-analysis self-request (see main.py). Must match the
# deployed service URL. Override at runtime with the SERVICE_BASE_URL env var.
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

# --- CLIP scoring (match buttonmatcher's constants) ---
CONFIDENCE_THRESHOLD      = 0.72   # above → confident match, eligible for alerts
REJECTION_THRESHOLD       = 0.45   # below → clearly not a gameday button
ALPHA                     = 0.7   # image weight  (matches match_buttons.py)
BETA                      = 0.3   # text weight   (matches match_buttons.py)
SLOGAN_PENALTY_THRESHOLD  = 0.3   # below this slogan_score → penalise overall
PENALTY_MULTIPLIER        = 0.7

# --- GCS dedup file ---
SEEN_ITEMS_BLOB = "ebay_scout/seen_items.json"

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
