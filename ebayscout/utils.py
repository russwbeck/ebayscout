"""
ebayscout/utils.py

Pure utility functions with no GCP / Slack dependencies.
Kept separate so unit tests can import them without triggering
module-level secret fetching in main.py.
"""


def parse_price_source(text: str) -> tuple[float | None, str, int | None]:
    """
    Parse a user reply in the form "$25.00 | Facebook Marketplace" or
    "$25.00 | Facebook Marketplace | 35" (with optional button count).

    Returns (price_float, source_string, count_or_None).
    Returns (None, "", None) on parse failure.
    Pipe separator is required between price and source.
    """
    if "|" not in text:
        return None, "", None

    parts     = text.split("|")
    price_raw = parts[0].strip().lstrip("$").strip()
    source    = parts[1].strip() if len(parts) > 1 else "Unknown"

    count = None
    if len(parts) >= 3:
        try:
            count = int(parts[2].strip())
        except ValueError:
            pass

    try:
        price = float(price_raw.replace(",", ""))
        return price, source, count
    except ValueError:
        return None, "", None


def title_has_excluded_keyword(title: str, excluded_keywords: list[str]) -> bool:
    """
    Return True if the listing title contains any of the excluded keywords.
    Comparison is case-insensitive substring match.

    Used to filter out apparel/clothing listings from eBay and Etsy results
    before CLIP processing.
    """
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in excluded_keywords)


def format_manual_result(
    source:          str,
    asking_price:    float,
    matches:         list[dict],
    lot_value:       float,
    margin:          float,
    needed:          list[dict],
    unmatched_count: int,
) -> str:
    """
    Format the lot analysis result for a manually-uploaded image.

    Args:
        source:          Where the listing was found (e.g. "Facebook Marketplace")
        asking_price:    Price the seller is asking
        matches:         High-confidence matches enriched with price data
        lot_value:       Sum of max_price_single for all matched buttons
        margin:          lot_value - asking_price (positive = good deal)
        needed:          Subset of matches where amount_needed > 0
        unmatched_count: Number of crops that didn't reach confidence threshold
    """
    lines = [f"📸 *Lot Analysis — {source}*", f"Asking: *${asking_price:.2f}*", ""]

    if matches:
        lines.append("Matched buttons:")
        for m in matches:
            year   = m.get("year", "?")
            slogan = m.get("slogan", "?")
            price  = m.get("max_price_single", "")
            n      = m.get("amount_needed", 0)
            star   = f"  ⭐ need {n}" if n > 0 else ""
            lines.append(f"  • {year} — \"{slogan}\"    max: {price}{star}")
    else:
        lines.append("_No buttons identified with confidence._")

    if unmatched_count > 0:
        lines.append(f"_{unmatched_count} button(s) not identified with confidence._")

    lines.append("")

    if lot_value > 0:
        if margin > 0:
            verdict = f"✅ Good deal — *+${margin:.2f}* below calculated value"
        else:
            verdict = f"⚠️ You'd overpay by *${abs(margin):.2f}*"
        lines.append(f"Calculated value: *${lot_value:.2f}*  |  {verdict}")
    else:
        lines.append("_Calculated value: $0.00 (no matched buttons have price rules)_")

    if needed:
        need_list = ", ".join(f"{m['year']} {m['slogan']}" for m in needed)
        lines.append(f"\n⭐ *Needed buttons in this lot:* {need_list}")

    return "\n".join(lines)
