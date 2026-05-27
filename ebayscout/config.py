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

# --- eBay API ---
EBAY_FINDING_URL  = "https://svcs.ebay.com/services/search/FindingService/v1"
EBAY_SHOPPING_URL = "https://open.api.ebay.com/shopping"
EBAY_MAX_RESULTS  = 100          # per query; eBay page size limit is 100
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
]

# --- Multi-query search strategy ---
# "Central Counties Bank" runs standalone (no button-type suffix) because it is a
# low-frequency, precise term that collides with noise when combined with other words.
BUTTON_TYPES  = ["button", "pin", "badge", "pinback"]
PSU_VARIANTS  = ["Penn State", "PSU", "Nittany Lions"]

EBAY_SEARCH_QUERIES: list[str] = (
    [f"{psu} {btn}" for psu in PSU_VARIANTS for btn in BUTTON_TYPES]
    + ["Central Counties Bank"]
)
# Produces 13 queries:
#   "Penn State button", "Penn State pin", "Penn State badge", "Penn State pinback",
#   "PSU button", "PSU pin", "PSU badge", "PSU pinback",
#   "Nittany Lions button", "Nittany Lions pin", "Nittany Lions badge", "Nittany Lions pinback",
#   "Central Counties Bank"

# --- Dry-run mode (set True for smoke testing) ---
# When True: skips save_seen() and sends Slack to #ebay-scout-test instead.
DRY_RUN = False
