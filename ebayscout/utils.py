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


# Decade markers — "1990s", "1990's", "'90s", "90s". A decade lot spans 10 years,
# so restricting matching to one search year would miss most of it.
_DECADE_RE_4 = re.compile(r"(?<!\d)(19|20)(\d)0'?s\b", re.I)   # 1990s / 1980's
_DECADE_RE_2 = re.compile(r"(?<!\d)'?([0-9])0s\b", re.I)        # 90s / '80s / 00s


def extract_decades(title: str) -> set[int]:
    """
    Years covered by any decade marker in a title ("1990s" → {1990..1999}).

    Catches the case where a year-crawl query for "1990" returns a "1990s"
    decade lot: matching must consider the whole decade, not just the one year,
    or we miss the other-year buttons in the lot.
    """
    if not title:
        return set()
    years: set[int] = set()
    for m in _DECADE_RE_4.finditer(title):
        dec = int(m.group(1) + m.group(2) + "0")
        years |= set(range(dec, dec + 10))
    for m in _DECADE_RE_2.finditer(title):
        d = int(m.group(1))                 # tens digit 0-9
        base = 1900 if d >= 7 else 2000     # 70s-90s → 19xx; 00s-30s → 20xx
        dec = base + d * 10
        years |= set(range(dec, dec + 10))
    return years


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


def dedup_listings(listings: list[dict]) -> list[dict]:
    """
    Drop cross-pass duplicate listings, keeping the FIRST occurrence of each
    item_id (which carries the search metadata — search_year / search_era — from
    the query pass that found it first).

    The scan combines several overlapping passes (general eBay + PSU + Etsy, or
    multiple year-/era-augmented queries), so the same item_id routinely appears
    2-3x in the merged list. Without this, a backfill processes — and counts —
    the same listing repeatedly: the May 2026 backfill fetched 1024 rows that
    were only 814 unique listings (~20% wasted CLIP compute and inflated volume).
    Listings missing an "item_id" are kept as-is (nothing to dedup on).
    """
    seen_ids: set = set()
    out: list[dict] = []
    for listing in listings:
        item_id = listing.get("item_id")
        if item_id is None:
            out.append(listing)
            continue
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        out.append(listing)
    return out


def is_non_alerting_slogan(slogan: str, patterns) -> bool:
    """
    True if a matched slogan is a placeholder that should NOT trigger scan
    alerts (e.g. "Slogan Unknown 5"). Case-insensitive substring match — same
    style as title_has_excluded_keyword.

    These rows stay in buy_rules / get_buy_decision so the /scout valuation and
    buy logic still use them; this only suppresses them from the daily scan's
    needed-button alerts, where they over-match and inflate lot value.
    """
    if not slogan:
        return False
    s = slogan.lower()
    return any(p.lower() in s for p in (patterns or []))


# A 1-3 digit quantity stated next to a lot/quantity keyword in a listing title,
# e.g. "Lot of 54", "set of 35", "(12) buttons", "250 pins", "200+ pinbacks".
# 1-3 digits only, so 4-digit years (1982) and price-like runs never match.
_LOT_COUNT_RES = (
    re.compile(r"(?:lot|set|group|grouping|collection)\s+of\s+\(?(\d{1,3})", re.I),
    re.compile(r"(?<!\d)(\d{1,3})\s*\+?\s*(?:button|pin|badge|pinback|pinbacks|pins|buttons|badges|pc|pcs|piece|pieces)\b", re.I),
    re.compile(r"\((\d{1,3})\)\s*(?:button|pin|badge|pinback|pc|pcs|piece)", re.I),
)


def extract_lot_count(title: str) -> int | None:
    """
    Parse a stated button/pin quantity from a listing title (for the detector's
    per-photo crop ceiling: 100 unless the title says more). Returns the largest
    plausible count found, or None.

    Deliberately conservative: only 1-3 digit numbers adjacent to a lot/quantity
    keyword, so years and prices don't masquerade as counts.
    """
    if not title:
        return None
    best = None
    for rx in _LOT_COUNT_RES:
        for m in rx.finditer(title):
            try:
                n = int(m.group(1))
            except (ValueError, TypeError):
                continue
            if n > 0 and (best is None or n > best):
                best = n
    return best


def sweep_radii(base_r: int, scales, floor: int) -> list[int]:
    """
    Multi-scale Hough radius schedule: base_r × each scale, floored, descending,
    de-duplicated (the floor can collapse small scales onto the same value).
    Pure arithmetic so it's unit-testable without cv2.
    """
    out: list[int] = []
    for s in scales:
        r = max(floor, int(base_r * s))
        if r not in out:
            out.append(r)
    return out


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


def build_era_queries(
    prefixes: list[str],
    button_types: list[str],
    era_word: str,
    era_label: str,
) -> list[tuple[str, str]]:
    """
    Build era-named search queries: one (query, era_label) pair per
    prefix × button_type, e.g. ("Penn State Mellon button", "Mellon").

    The bank word in the query both surfaces era-tagged listings and tells the
    matcher which era to restrict to (search_era → restrict_years).
    """
    return [
        (f"{prefix} {era_word} {btn}", era_label)
        for prefix in prefixes
        for btn in button_types
    ]


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
