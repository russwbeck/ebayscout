"""
ebayscout/utils.py

Pure utility functions with no GCP / Slack dependencies.
Kept separate so unit tests can import them without triggering
module-level secret fetching in main.py.
"""

import re

# A 4-digit year 1900-2099 bounded by non-digits so prices like "$1,982" or
# model numbers embedded in longer digit runs don't produce false matches.
_YEAR_RE = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")


def extract_years(title: str) -> set[int]:
    """
    Pull plausible button years (1900-2099) out of a listing title.

    Used as a cheap corroborating signal for the needed-button scan: if a
    needed button's year appears in the title, a moderate image match for that
    year is treated as a hit, and the title-year presence gates whether we pull
    the listing's additional photos.

    Returns a set of ints (empty if none found).
    """
    if not title:
        return set()
    return {int(m.group(0)) for m in _YEAR_RE.finditer(title)}


def needed_years(buy_rules: dict) -> set[int]:
    """
    Years that still have at least one needed button (amount_needed > 0).

    Drives the year-augmented deep crawl: we only issue year-specific eBay
    searches for years the collector is actually chasing, skipping the many
    empty years. buy_rules is keyed by (year_str, slogan) with an
    "amount_needed" string value (see sheets_client.load_buy_rules).
    """
    years: set[int] = set()
    for key, rule in buy_rules.items():
        year = key[0] if isinstance(key, (tuple, list)) else key
        try:
            amount = int((rule or {}).get("amount_needed", 0) or 0)
        except (ValueError, TypeError):
            amount = 0
        if amount <= 0:
            continue
        try:
            years.add(int(str(year).strip()))
        except (ValueError, TypeError):
            continue
    return years


_ERA_ALIASES = {
    "Central Counties": ("central counties", "central", "counties", "ccb", "cc"),
    "Mellon":           ("mellon",),
    "Citizens":         ("citizens", "citizen"),
}
_ALL_ALIASES = ("all", "any", "everything", "both")


def era_year_set(era_label: str, eras: dict) -> set[int]:
    """
    All years in an era's [lo, hi] range. Empty set for "all"/None/unknown
    (meaning: no year restriction).
    """
    if not era_label or era_label.lower() in _ALL_ALIASES:
        return set()
    bounds = eras.get(era_label)
    if not bounds:
        return set()
    lo, hi = bounds
    return set(range(lo, hi + 1))


def parse_era(text: str) -> str | None:
    """
    Map free text to a canonical era label, "all", or None.
    Returns "all" if the user explicitly opts out of era restriction.
    """
    t = (text or "").lower()
    if any(a in t for a in _ALL_ALIASES):
        return "all"
    for label, aliases in _ERA_ALIASES.items():
        if any(a in t for a in aliases):
            return label
    return None


def other_era(era_used: str | None, era_ranked: list, eras: dict) -> str | None:
    """
    The best alternative era to try on a 'No' — the runner-up by vote
    (era_ranked), else the next defined era that isn't the one we used.
    Returns None if there is no other era.
    """
    for era in (era_ranked or []):
        if era and era != era_used:
            return era
    for era in eras:
        if era != era_used:
            return era
    return None


def parse_confirmation(text: str) -> tuple[int | None, str | None]:
    """
    Parse a confirmation reply at the count/era confirm step.

    Returns (count_override, era_override):
      - count_override: first bare integer found, else None (keep detected count)
      - era_override:   canonical era label, "all", or None (keep suggested era)
    "go"/"yes"/"ok" with nothing else → (None, None) = accept the suggestions.
    """
    count = None
    m = re.search(r"(?<!\d)(\d{1,3})(?!\d)", text or "")
    if m:
        count = int(m.group(1))
    return count, parse_era(text)


def build_year_queries(base_terms: list[str], years) -> list[tuple[str, int]]:
    """
    Build year-augmented search queries: one (query, year) pair per
    base_term × year, e.g. ("Penn State button", 1982) → "Penn State button 1982".

    Returned in ascending year order (then base-term order) for deterministic,
    log-friendly crawling. Empty if either input is empty.
    """
    return [
        (f"{term} {year}", year)
        for year in sorted(set(years))
        for term in base_terms
    ]


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
