"""
ebayscout/pipeline_classify.py

Pure helpers for the deal alerts of the legacy CLIP scan (_run_daily_scan):
``lot_value_and_deal`` (a lot's matched value against its asking price) and
``lookalike_note`` (the value warning on a confirmed slogan with curated
look-alikes).

The module was the Gemini pipeline's autoconfirmation decision tree, factored
out of ``main.process_pipeline_lot``; that tree (classify_crops), the DB-direct
tier and the staging funnel went with the pipeline on 2026-10-25.

Pure python (stdlib only) — no heavy deps.
"""

from __future__ import annotations


def lot_value_and_deal(auto_confirmed, price_of, asking):
    """Total matched lot value + the undervalued-deal flag.

    ``price_of(year, slogan) -> float`` returns a button's max single-sale price
    (0.0 when no buy-rule/price exists for it). Kept pure by injection — main
    wires ``price_of`` to ``sheets_client.get_buy_decision`` + ``parse_price``;
    tests pass a stub.

    Returns ``(lot_value, undervalued, margin)`` where
    ``undervalued = asking > 0 and lot_value > asking`` and
    ``margin = lot_value - asking``.
    """
    lot_value = 0.0
    for b in auto_confirmed:
        try:
            lot_value += float(price_of(b.get("year"), b.get("slogan")) or 0.0)
        except Exception:
            pass
    ask = float(asking or 0.0)
    return lot_value, (ask > 0 and lot_value > ask), lot_value - ask


# --- Look-alike value flag ----------------------------------------------------
#
# ebayscout has no human-review lane: `_post_yellow_review` is defined but never
# called, and the pipeline posts deals only.  So buttonmatcher's confusable
# guard — which DEMOTES an auto-confirm to a human picker — has nowhere to
# demote to here; applying it would silently drop the match instead.
#
# The failure mode still matters, because it lands somewhere different.  A
# confirmed (year, slogan) is what `get_buy_decision` is keyed on, so a wrong
# within-year slogan pick moves BOTH of the numbers an alert exists to report:
# `amount_needed` (whether you need it at all) and `max_price_single` (what the
# lot is worth).  A same-year look-alike can therefore send you to buy a lot for
# a button that isn't in it, priced off the wrong row.
#
# There IS a human in this system — reading the deal post, deciding to spend
# money.  So the fix that fits is to FLAG the alert, not to demote the match:
# keep the deal, name the look-alikes, and say what the value would be if the
# other one is the right read.

def lookalike_note(slogan, year, family, priced_fn, normalize_fn, max_listed=4):
    """Value warning for a confirmed match whose slogan has curated look-alikes.

    Parameters
    ----------
    slogan, year : the confirmed match (the one that will be alerted on).
    family       : the catalog entries of its confusable group, as returned by
                   ``confusable_slogans.confusable_family`` — dicts carrying at
                   least ``slogan`` and ``year``.
    priced_fn    : ``(year, slogan) -> (price: float, amount_needed: int)``.
                   Injected so this stays pure and unit-testable; the caller
                   passes a closure over the already-loaded buy_rules dict, so
                   no Sheet round-trip happens here.
    normalize_fn : slogan-identity normalizer, used to drop the match's own
                   entries from its family.

    Returns None when there is nothing worth saying — no family, or every
    alternative carries the same price AND the same need, so a wrong pick would
    not change the alert.  Otherwise a dict:

        alternatives  [{slogan, year, price, amount_needed}], priciest first,
                      capped at ``max_listed``
        price_here    the confirmed match's own price
        value_swing   the largest |alternative price - price_here|, i.e. how far
                      off the lot value could be
        need_here     the confirmed match's amount_needed
        any_not_needed  True when at least one alternative is NOT needed — the
                      case where the whole alert could be spurious
    """
    if not slogan or not family:
        return None
    own_key = normalize_fn(slogan)
    price_here, need_here = priced_fn(year, slogan)

    alts, seen = [], set()
    for entry in family:
        e_slogan = (entry or {}).get("slogan")
        if not e_slogan:
            continue
        if normalize_fn(e_slogan) == own_key:
            continue                      # the match's own entries, incl. its twins
        e_year = (entry or {}).get("year")
        dedup = (normalize_fn(e_slogan), str(e_year))
        if dedup in seen:
            continue
        seen.add(dedup)
        e_price, e_need = priced_fn(e_year, e_slogan)
        alts.append({"slogan": e_slogan, "year": e_year,
                     "price": e_price, "amount_needed": e_need})
    if not alts:
        return None

    swing = max(abs(a["price"] - price_here) for a in alts)
    any_not_needed = any(a["amount_needed"] <= 0 for a in alts)
    if swing == 0 and not any_not_needed:
        # Same price, same standing need: which one it is doesn't change the
        # decision, so don't spend a line on it.
        return None

    alts.sort(key=lambda a: a["price"], reverse=True)
    return {
        "alternatives":   alts[:max_listed],
        "price_here":     price_here,
        "value_swing":    swing,
        "need_here":      need_here,
        "any_not_needed": any_not_needed,
    }
