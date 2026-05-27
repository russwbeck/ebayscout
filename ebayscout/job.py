"""
ebayscout/job.py

Entry point for the eBay Button Scout Cloud Run Job.

Run order:
  1. Fetch secrets from GCP Secret Manager
  2. Load seen items (GCS dedup)
  3. Load buy rules (Google Sheets)
  4. Init CLIP matcher (GCS vectors)
  5. Find new eBay listings (13 queries, deduplicated)
  6. For each new listing: identify buttons, check value & needs, alert
  7. Save updated seen items back to GCS

Exit codes:
  0 — completed successfully
  1 — fatal error (Sheets load failed, or CLIP init failed)
"""

import sys
import traceback
from datetime import date

from google.cloud import secretmanager

from . import config
from . import ebay_client
from . import etsy_client
from . import seen_items as seen_store
from . import sheets_client
from . import clip_matcher
from . import image_proc
from . import notifier


# ---------------------------------------------------------------------------
# Secret retrieval
# ---------------------------------------------------------------------------

def _get_secret(secret_id: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    name   = f"projects/{config.PROJECT_NUMBER}/secrets/{secret_id}/versions/latest"
    resp   = client.access_secret_version(request={"name": name})
    return resp.payload.data.decode("UTF-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(">>> EBAYSCOUT: Starting job.", flush=True)

    # ------------------------------------------------------------------
    # 1. Secrets
    # ------------------------------------------------------------------
    print(">>> EBAYSCOUT: Fetching secrets...", flush=True)
    try:
        slack_token    = _get_secret("EBAY_BOT_TOKEN")
        slack_channel  = _get_secret("CHANNEL_ID_EBAY")
        sheets_json    = _get_secret("GOOGLE_SHEETS_JSON")
        spreadsheet_id = _get_secret("SPREADSHEET_ID")
    except Exception as exc:
        print(f"!!! EBAYSCOUT: Failed to fetch secrets: {exc}", flush=True)
        traceback.print_exc()
        return 1

    # Optional secrets — log warning but continue if unavailable
    try:
        ebay_app_id = _get_secret("EBAY_APP_ID")
    except Exception as exc:
        print(f"!!! EBAYSCOUT: EBAY_APP_ID not available — skipping eBay: {exc}", flush=True)
        ebay_app_id = None

    try:
        etsy_api_key = _get_secret("ETSY_API_KEY")
    except Exception as exc:
        print(f"!!! EBAYSCOUT: ETSY_API_KEY not available — skipping Etsy: {exc}", flush=True)
        etsy_api_key = None

    if not ebay_app_id and not etsy_api_key:
        print("!!! EBAYSCOUT: No API keys available (EBAY_APP_ID, ETSY_API_KEY) — nothing to scan.", flush=True)
        return 1

    if config.DRY_RUN:
        slack_channel = slack_channel + "-test"   # post to a test channel in dry-run mode

    print(
        f">>> EBAYSCOUT: Slack channel: {slack_channel} "
        f"{'(DRY RUN)' if config.DRY_RUN else ''}",
        flush=True,
    )

    # ------------------------------------------------------------------
    # 2. Load seen items
    # ------------------------------------------------------------------
    seen = seen_store.load_seen()

    # ------------------------------------------------------------------
    # 3. Load buy rules (fatal if unavailable — can't calculate value)
    # ------------------------------------------------------------------
    print(">>> EBAYSCOUT: Loading buy rules from Google Sheets...", flush=True)
    try:
        buy_rules = sheets_client.load_buy_rules(sheets_json, spreadsheet_id)
    except RuntimeError as exc:
        print(f"!!! EBAYSCOUT: {exc}", flush=True)
        return 1

    # ------------------------------------------------------------------
    # 4. Init CLIP matcher (fatal if vectors unavailable)
    # ------------------------------------------------------------------
    try:
        clip_matcher.init(config.BUCKET_NAME)
    except Exception as exc:
        print(f"!!! EBAYSCOUT: CLIP init failed: {exc}", flush=True)
        traceback.print_exc()
        return 1

    # ------------------------------------------------------------------
    # 5. Find new listings (eBay + Etsy)
    # ------------------------------------------------------------------
    all_listings: list[dict] = []

    if ebay_app_id:
        print(">>> EBAYSCOUT: Querying eBay...", flush=True)
        try:
            ebay_listings = ebay_client.find_all_listings(
                app_id=ebay_app_id,
                queries=config.EBAY_SEARCH_QUERIES,
                excluded_sellers=config.EXCLUDED_SELLERS,
                max_results=config.EBAY_MAX_RESULTS,
            )
            all_listings.extend(ebay_listings)
            print(f">>> EBAYSCOUT: eBay returned {len(ebay_listings)} listings.", flush=True)
        except Exception as exc:
            print(f"!!! EBAYSCOUT: eBay find_all_listings failed: {exc}", flush=True)
            traceback.print_exc()
    else:
        print(">>> EBAYSCOUT: Skipping eBay (no EBAY_APP_ID).", flush=True)

    if etsy_api_key:
        print(">>> EBAYSCOUT: Querying Etsy...", flush=True)
        try:
            etsy_listings = etsy_client.find_all_listings(
                api_key=etsy_api_key,
                queries=config.EBAY_SEARCH_QUERIES,
                excluded_sellers=config.ETSY_EXCLUDED_SELLERS,
                max_results=config.EBAY_MAX_RESULTS,
            )
            all_listings.extend(etsy_listings)
            print(f">>> EBAYSCOUT: Etsy returned {len(etsy_listings)} listings.", flush=True)
        except Exception as exc:
            print(f"!!! EBAYSCOUT: Etsy find_all_listings failed: {exc}", flush=True)
            traceback.print_exc()
    else:
        print(">>> EBAYSCOUT: Skipping Etsy (no ETSY_API_KEY).", flush=True)

    new_listings = [l for l in all_listings if seen_store.is_new(l["item_id"], seen)]
    ebay_new     = sum(1 for l in new_listings if not l["item_id"].startswith("etsy_"))
    etsy_new     = sum(1 for l in new_listings if     l["item_id"].startswith("etsy_"))
    print(
        f">>> EBAYSCOUT: {len(all_listings)} total listings across all sources, "
        f"{len(new_listings)} new (not yet seen).",
        flush=True,
    )

    # ------------------------------------------------------------------
    # 6. Process each new listing
    # ------------------------------------------------------------------
    # Scan-summary counters
    stat_alerted        = 0   # listings that triggered at least one alert
    stat_low_confidence = 0   # best crop 45–72% — might be a button, no alert
    stat_rejected       = 0   # best crop <45%  — clearly not a bank button

    for listing in new_listings:
        item_id = listing["item_id"]
        title   = listing.get("title", "?")
        seller  = listing.get("seller", "?")
        asking  = listing.get("current_price", 0.0)
        print(
            f"\n>>> LISTING: {item_id} | seller={seller} | ${asking:.2f} | {title[:60]}",
            flush=True,
        )

        try:
            # a. Get full-size picture URLs
            # Etsy: full image already in gallery_url (url_fullxfull from API response)
            # eBay: fetch via Shopping API; fall back to gallery_url thumbnail
            if item_id.startswith("etsy_"):
                picture_urls = [listing["gallery_url"]] if listing.get("gallery_url") else []
            elif ebay_app_id:
                picture_urls = ebay_client.get_item_pictures(ebay_app_id, item_id)
                if not picture_urls and listing.get("gallery_url"):
                    picture_urls = [listing["gallery_url"]]
            else:
                picture_urls = [listing["gallery_url"]] if listing.get("gallery_url") else []

            # b–c. Detect buttons and match across all photos.
            # match_crop is called at REJECTION_THRESHOLD so we capture every
            # result ≥ 0.45.  Confident matches (≥ CONFIDENCE_THRESHOLD) go into
            # all_match_keys for alerts; sub-threshold results only affect the
            # summary counter.
            all_match_keys: dict[tuple, dict] = {}   # (year, slogan) → best confident match
            best_score_seen = 0.0                    # highest overall score across all crops
            photos_processed = 0

            for photo_url in picture_urls[: config.MAX_PHOTOS_PER_LISTING]:
                try:
                    image_bytes = image_proc.download_image(photo_url)
                except Exception as exc:
                    print(f"!!! LISTING: Failed to download photo {photo_url}: {exc}", flush=True)
                    continue

                try:
                    crops = image_proc.detect_and_crop(image_bytes)
                except Exception as exc:
                    print(f"!!! LISTING: detect_and_crop failed: {exc}", flush=True)
                    continue

                photos_processed += 1

                for crop in crops:
                    try:
                        # Use REJECTION_THRESHOLD so low-confidence crops are visible
                        match = clip_matcher.match_crop(
                            crop, threshold=config.REJECTION_THRESHOLD
                        )
                    except Exception as exc:
                        print(f"!!! LISTING: match_crop failed: {exc}", flush=True)
                        continue

                    if match is None:
                        continue   # below REJECTION_THRESHOLD — truly not a button

                    best_score_seen = max(best_score_seen, match["overall"])

                    if match["overall"] >= config.CONFIDENCE_THRESHOLD:
                        key = (match["year"], match["slogan"])
                        if key not in all_match_keys or match["overall"] > all_match_keys[key]["overall"]:
                            all_match_keys[key] = match

            if photos_processed == 0:
                print(f"!!! LISTING: No photos could be processed for {item_id}.", flush=True)
                stat_rejected += 1
                seen_store.mark_seen(item_id, seen)
                continue

            # Categorise listing for the summary
            if not all_match_keys and best_score_seen < config.REJECTION_THRESHOLD:
                stat_rejected += 1
                print(f">>> LISTING: Rejected — best score {best_score_seen:.2f}", flush=True)
                seen_store.mark_seen(item_id, seen)
                continue
            elif not all_match_keys:
                stat_low_confidence += 1
                print(
                    f">>> LISTING: Low confidence — best score {best_score_seen:.2f}, no alert",
                    flush=True,
                )
                seen_store.mark_seen(item_id, seen)
                continue

            # d. Enrich confident matches with price data
            high_conf_matches: list[dict] = []
            for (year, slogan), match in all_match_keys.items():
                price_single, price_year, notes, amount_needed = sheets_client.get_buy_decision(
                    year, slogan, buy_rules
                )
                enriched = dict(match)
                enriched["max_price_single"] = price_single
                enriched["max_price_year"]   = price_year
                enriched["notes"]            = notes
                enriched["amount_needed"]    = amount_needed
                high_conf_matches.append(enriched)

            # e–f. Calculate lot value and find needed buttons
            lot_value    = sum(sheets_client.parse_price(m["max_price_single"]) for m in high_conf_matches)
            unmatched    = 0   # crops below confidence but above rejection (already tracked via best_score_seen)
            margin       = lot_value - asking
            needed_found = [m for m in high_conf_matches if m["amount_needed"] > 0]

            print(
                f">>> LISTING: {len(high_conf_matches)} matched | "
                f"lot_value=${lot_value:.2f}, asking=${asking:.2f}, margin=${margin:.2f} | "
                f"{len(needed_found)} needed",
                flush=True,
            )

            # g. Send alerts
            listing_alerted = False

            if margin > 0:
                print(">>> ALERT: Undervalued lot — sending Slack notification.", flush=True)
                if not config.DRY_RUN:
                    notifier.send_undervalued_alert(
                        slack_token=slack_token,
                        channel=slack_channel,
                        listing=listing,
                        matches=high_conf_matches,
                        lot_value=lot_value,
                        asking_price=asking,
                        margin=margin,
                        unmatched_count=unmatched,
                    )
                else:
                    print(f"    [DRY RUN] Would post undervalued alert to {slack_channel}", flush=True)
                listing_alerted = True

            if needed_found:
                print(">>> ALERT: Needed buttons found — sending Slack notification.", flush=True)
                if not config.DRY_RUN:
                    notifier.send_needed_alert(
                        slack_token=slack_token,
                        channel=slack_channel,
                        listing=listing,
                        needed_buttons=needed_found,
                        asking_price=asking,
                        lot_value=lot_value,
                    )
                else:
                    print(f"    [DRY RUN] Would post needed-buttons alert to {slack_channel}", flush=True)
                listing_alerted = True

            if listing_alerted:
                stat_alerted += 1
            else:
                # Confident match found but no alert condition met
                stat_low_confidence += 1

        except Exception as exc:
            print(f"!!! LISTING: Unexpected error processing {item_id}: {exc}", flush=True)
            traceback.print_exc()

        # Always mark as seen (even on error, to avoid infinite retries)
        seen_store.mark_seen(item_id, seen)

    # ------------------------------------------------------------------
    # 7. Persist updated seen items
    # ------------------------------------------------------------------
    if not config.DRY_RUN:
        success = seen_store.save_seen(seen)
        if not success:
            try:
                notifier.send_warning(
                    slack_token,
                    slack_channel,
                    "Failed to save seen_items.json to GCS. "
                    "Next run may re-alert on already-seen listings.",
                )
            except Exception:
                pass
    else:
        print("[DRY RUN] Skipping save_seen().", flush=True)

    # ------------------------------------------------------------------
    # 8. Post scan summary
    # ------------------------------------------------------------------
    print(
        f"\n>>> EBAYSCOUT: Done. "
        f"alerted={stat_alerted}, low_conf={stat_low_confidence}, rejected={stat_rejected}",
        flush=True,
    )
    if not config.DRY_RUN:
        try:
            notifier.send_scan_summary(
                slack_token=slack_token,
                channel=slack_channel,
                alerted=stat_alerted,
                low_confidence=stat_low_confidence,
                rejected=stat_rejected,
                ebay_count=ebay_new,
                etsy_count=etsy_new,
            )
        except Exception as exc:
            print(f"!!! EBAYSCOUT: Failed to post scan summary: {exc}", flush=True)
    else:
        print(
            f"[DRY RUN] Summary: alerted={stat_alerted}, "
            f"low_conf={stat_low_confidence}, rejected={stat_rejected}",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
