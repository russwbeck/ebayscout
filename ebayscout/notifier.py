"""
ebayscout/notifier.py

Format and send Slack alerts for the two alert types:
  1. Undervalued lot  (asking price < calculated lot value)
  2. Needed buttons   (lot contains buttons where amount_needed > 0)

Uses slack_sdk WebClient directly — no Bolt / Flask needed for a batch job.
"""

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import config


def send_undervalued_alert(
    slack_token: str,
    channel: str,
    listing: dict,
    matches: list[dict],
    lot_value: float,
    asking_price: float,
    margin: float,
    unmatched_count: int,
) -> None:
    """
    Post an undervalued-lot alert.

    Args:
        listing:         {item_id, title, listing_url, seller, ...}
        matches:         High-confidence matches with price data fields:
                         {year, slogan, overall, max_price_single, amount_needed}
        lot_value:       Sum of max_price_single for all high-conf matches.
        asking_price:    eBay asking price.
        margin:          lot_value - asking_price (always > 0 when this fires).
        unmatched_count: Number of crops that didn't reach confidence threshold.
    """
    title       = listing.get("title", "Untitled")
    listing_url = listing.get("listing_url", "")
    seller      = listing.get("seller", "unknown")

    header_text = (
        f"🔍 *Undervalued lot found on eBay*\n"
        f"*<{listing_url}|{_truncate(title, 80)}>*\n"
        f"Asking: *${asking_price:.2f}*  |  "
        f"Calculated value: *${lot_value:.2f}*  |  "
        f"Margin: *+${margin:.2f}*"
    )

    button_lines = []
    for m in matches:
        year   = m.get("year", "?")
        slogan = m.get("slogan", "?")
        price  = m.get("max_price_single", "")
        needed = m.get("amount_needed", 0)
        needed_tag = f"  ⭐ needed ({needed})" if needed > 0 else ""
        line = f"  • {year} — \"{slogan}\"    max: {price}{needed_tag}"
        button_lines.append(line)

    buttons_text = "\n".join(button_lines) if button_lines else "  (none identified with confidence)"

    unmatched_text = (
        f"\n_{unmatched_count} additional button(s) could not be identified with confidence._"
        if unmatched_count > 0
        else ""
    )

    seller_text = f"Seller: {seller}"

    full_text = "\n\n".join(filter(None, [
        header_text,
        "Matched buttons:\n" + buttons_text + unmatched_text,
        seller_text,
    ]))

    _post_message(slack_token, channel, full_text)


def send_needed_alert(
    slack_token: str,
    channel: str,
    listing: dict,
    needed_buttons: list[dict],
    asking_price: float,
    lot_value: float,
) -> None:
    """
    Post a needed-buttons alert.

    Args:
        needed_buttons: High-confidence matches where amount_needed > 0.
                        Each dict: {year, slogan, max_price_single, amount_needed}
        asking_price:   eBay asking price.
        lot_value:      Calculated lot value (may be 0 if some buttons have no price rule).
    """
    title       = listing.get("title", "Untitled")
    listing_url = listing.get("listing_url", "")
    seller      = listing.get("seller", "unknown")

    value_note = (
        f"Lot value (matched): ${lot_value:.2f}"
        if lot_value > 0
        else "Lot value: not calculable"
    )

    header_text = (
        f"⭐ *Possible needed button — review the photos*\n"
        f"*<{listing_url}|{_truncate(title, 80)}>*\n"
        f"Asking: *${asking_price:.2f}*  |  {value_note}"
    )

    needed_lines = []
    for m in needed_buttons:
        year   = m.get("year", "?")
        slogan = m.get("slogan", "?")
        price  = m.get("max_price_single", "")
        needed = m.get("amount_needed", 0)
        line = f"  • {year} — \"{slogan}\"   need {needed}, max: {price}"
        needed_lines.append(line)

    needed_text = "\n".join(needed_lines)
    seller_text = f"Seller: {seller}"

    full_text = "\n\n".join(filter(None, [
        header_text,
        "Needed buttons in this lot:\n" + needed_text,
        seller_text,
    ]))

    _post_message(slack_token, channel, full_text)


def send_scan_summary(
    slack_token: str,
    channel: str,
    alerted: int,
    confirmed_not_needed: int,
    rejected: int,
    ebay_count: int,
    etsy_count: int,
) -> None:
    """
    Post a single end-of-scan summary message.

    alerted:              listings with a confirmed (auto/green) needed button
    confirmed_not_needed: listings with a confirmed button, but none needed
    rejected:             listings where no crop reached the green/auto gate
    ebay_count:           new eBay listings processed
    etsy_count:           new Etsy listings processed
    """
    from datetime import date
    today      = date.today().strftime("%a %b %-d")
    total      = alerted + confirmed_not_needed + rejected
    source_str = f"eBay: {ebay_count}"
    if etsy_count:
        source_str += f" · Etsy: {etsy_count}"

    lines = [f"🔍 *Daily scan — {today}*", f"📦 {total} new listings  ({source_str})"]

    if alerted:
        lines.append(
            f"⭐ {alerted} needed-button candidate{'s' if alerted != 1 else ''}"
            f" to review"
        )
    else:
        lines.append("⭐ No needed-button candidates today")

    if confirmed_not_needed:
        lines.append(
            f"🟢 {confirmed_not_needed} confirmed gameday button"
            f"{'s' if confirmed_not_needed != 1 else ''}"
            f"  (auto/green match, but not a needed year — no alert)"
        )

    if rejected:
        lines.append(f"🗑️ {rejected} not confirmed  (no auto/green match)")

    _post_message(slack_token, channel, "\n".join(lines))


def send_backfill_digest(
    slack_token: str,
    channel: str,
    records: list[dict],
    threshold: float,
) -> None:
    """
    Post a single dry-run preview digest to Slack instead of N alerts.

    Used by a `?dry_run=1` backfill so the run's needed-button candidate
    scores land somewhere readable (Slack) for tuning NEEDED_MATCH_THRESHOLD,
    without writing anything to GCS or firing real alerts.

    `records` are the _scan_log_record dicts; each may carry a `best_needed`
    {year, slogan, overall} — the top needed-mapped candidate for that listing,
    *even if it fell below the bar* — which is exactly the distribution to tune.
    """
    needed = [r for r in records if r.get("best_needed")]
    needed.sort(key=lambda r: r["best_needed"]["overall"], reverse=True)

    would_alert = sum(1 for r in records if r.get("needed_hit"))
    n_total     = len(records)

    lines = [
        "🧪 *Backfill preview (DRY RUN)* — no alerts posted, nothing written.",
        f"Processed *{n_total}* listings · would alert on *{would_alert}* "
        f"needed candidate(s) at threshold *{threshold:.2f}*.",
    ]

    if needed:
        lines.append("")
        lines.append("*Needed-button candidates by score* (✅ = above threshold):")
        for r in needed[:25]:
            bn    = r["best_needed"]
            mark  = "✅" if bn["overall"] >= threshold else "▫️"
            title = _truncate(r.get("title", ""), 60)
            url   = r.get("listing_url", "")
            link  = f"<{url}|{title}>" if url else title
            lines.append(
                f"  {mark} `{bn['overall']:.2f}`  {bn['year']} \"{bn['slogan']}\"  "
                f"· ${r.get('asking', 0):.0f} · {link}"
            )
        if len(needed) > 25:
            lines.append(f"  …and {len(needed) - 25} more.")
        lines.append("")
        lines.append(
            "_Set `NEEDED_MATCH_THRESHOLD` just below the lowest score you'd "
            "still want pinged, then run the live backfill (`?ignore_seen=1`)._"
        )
    else:
        lines.append("_No needed-button candidates surfaced in this preview._")

    _post_message(slack_token, channel, "\n".join(lines))


def send_crawl500_summary(
    slack_token: str,
    channel: str,
    processed: int,
    alerted: int,
    confirmed_not_needed: int,
    rejected: int,
    first_run: bool,
) -> None:
    """Post a single end-of-run summary for an on-demand2 /crawl500 search."""
    lines = [
        f"🔎 *crawl500 complete* — processed *{processed}* lot{'s' if processed != 1 else ''}"
        f"{' (first run — included already-seen lots)' if first_run else ''}.",
    ]
    if alerted:
        lines.append(f"⭐ {alerted} needed-button candidate{'s' if alerted != 1 else ''} to review")
    else:
        lines.append("⭐ No needed-button candidates this run")
    if confirmed_not_needed:
        lines.append(
            f"🟢 {confirmed_not_needed} confirmed gameday button"
            f"{'s' if confirmed_not_needed != 1 else ''}  (auto/green, not a needed year)"
        )
    if rejected:
        lines.append(f"🗑️ {rejected} not confirmed  (no auto/green match)")
    _post_message(slack_token, channel, "\n".join(lines))


def send_warning(slack_token: str, channel: str, message: str) -> None:
    """Post a plain-text operational warning to the scout channel."""
    _post_message(slack_token, channel, f"⚠️ *ebayscout warning*: {message}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _post_message(slack_token: str, channel: str, text: str) -> None:
    client = WebClient(token=slack_token)
    try:
        client.chat_postMessage(channel=channel, text=text, mrkdwn=True)
    except SlackApiError as exc:
        print(f"!!! SLACK: Failed to post to {channel}: {exc.response['error']}", flush=True)


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"
