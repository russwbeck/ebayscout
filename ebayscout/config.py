"""
ebayscout/config.py

All tuneable constants for the eBay Button Scout batch job.
Edit EXCLUDED_SELLERS and SLACK_SCOUT_CHANNEL before first deploy.
"""

# --- GCP ---
BUCKET_NAME    = "60d488c5-9c8e-4acc-aac-button-data"
PROJECT_NUMBER = "404960106109"

# --- eBay Marketplace Account Deletion endpoint ---
# This URL must match exactly what is registered in the eBay Developer Portal
# (developer.ebay.com → Application Keys → Notifications).
# The verification token is stored in GCP Secret Manager as
# EBAY_DELETION_VERIFICATION_TOKEN (32-80 chars, alphanumeric + _ -)
EBAY_DELETION_ENDPOINT = (
    "https://ebay-scout-404960106109.us-east1.run.app/ebay/account-deletion"
)

# --- CLIP scoring (match buttonmatcher's constants) ---
CONFIDENCE_THRESHOLD      = 0.72   # above → confident match, eligible for alerts
REJECTION_THRESHOLD       = 0.45   # below → clearly not a bank button
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
MAX_PHOTOS_PER_LISTING = 1       # only process the first photo per listing

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
    "enamel",
    "enameled",
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

# Unrestricted queries — "Penn State" and "Nittany Lions" are unambiguous.
EBAY_SEARCH_QUERIES: list[str] = (
    [f"Penn State {btn}" for btn in BUTTON_TYPES]
    + [f"Nittany Lions {btn}" for btn in BUTTON_TYPES]
    + ["Central Counties Bank"]
)
# Produces 9 queries:
#   "Penn State button/pin/badge/pinback"
#   "Nittany Lions button/pin/badge/pinback"
#   "Central Counties Bank"

# PSU queries run with category_ids=SPORTS_MEMO_CATEGORY_ID so "PSU" matches
# Penn State University buttons rather than Power Supply Units.
PSU_SEARCH_QUERIES: list[str] = [f"PSU {btn}" for btn in BUTTON_TYPES]
# Produces 4 queries: "PSU button", "PSU pin", "PSU badge", "PSU pinback"

# --- Dry-run mode (set True for smoke testing) ---
# When True: the scan runs end-to-end but posts no Slack messages and does
# not write seen_items.json — it logs "[DRY RUN]" lines instead.
DRY_RUN = False
