"""
gemini_resolve — cross-check buttonmatcher's independent CLIP slogan ranking
against Gemini's per-button reading, and resolve buttons where the two agree.

CLIP produces a ranked top-N candidate list per crop.  Each crop is also linked
(by position, in gemini_geometry) to at most one Gemini slogan.  This module
decides, per crop, one of three outcomes:

  Scenario A — confirmed, unique year:
      The crop's Gemini slogan matches a candidate in the crop's CLIP top-N and
      that slogan resolves to a single year among the matching candidates →
      resolve directly.

  Scenario B — confirmed, repeated slogan:
      Same match, but the matching candidates span multiple years (the slogan is
      reused across years) → resolve to the year closest to the photo's MAJORITY
      era (from the Scenario-A anchors).  No clear majority → fall back to CLIP's
      own top-ranked matching candidate.

  Scenario C — pure miss:
      No Gemini slogan, or it matches nothing in the crop's top-N → left for
      manual Slack resolution.

A confirmed crop only AUTO-resolves (no human click) when Gemini's confidence
clears ``conf_min``, the slogan is not flagged as a problem read, AND the
association is physically anchored (``assoc["anchored"]``, stamped by the caller
from gemini_geometry.assoc_anchored — Gemini's point actually sits on the crop);
otherwise it is still surfaced/pre-highlighted but downgraded to require
confirmation.  The 2026-07-16 shifted-lot incident: detection dropped circles on
blank bag and missed real buttons, unlimited nearest-neighbor association then
paired the orphaned slogans with the blank crops at 2.6–6.6× radius, and this
gate (absent then) was the only thing between those pairs and a wrong AUTO.

Pure python — unit-testable with synthetic candidate lists.  ``normalize_fn`` is
injected (use ``buy_rules._normalize_key``) so this module owns no string policy.
"""

from __future__ import annotations

from collections import Counter

import edition_twins as edt


def _year_int(y):
    """Best-effort int from a year label; None if not numeric."""
    try:
        return int(str(y).strip())
    except (TypeError, ValueError):
        # pull the first 4-digit run if embedded (e.g. "1984 (Mellon)")
        digits = ""
        for ch in str(y):
            if ch.isdigit():
                digits += ch
                if len(digits) == 4:
                    return int(digits)
            else:
                digits = ""
        return None


def _matching_candidates(candidates, norm_g, normalize_fn):
    """Return [(rank, cand)] for top-N candidates whose slogan matches norm_g."""
    out = []
    for rank, c in enumerate(candidates):
        if normalize_fn(c.get("slogan", "")) == norm_g:
            out.append((rank, c))
    return out


def _majority_year(era_votes):
    """Modal year from a Counter; returns (year_int, clear) — clear iff a single
    mode with >= 2 votes."""
    if not era_votes:
        return None, False
    most = era_votes.most_common()
    top_year, top_n = most[0]
    if top_n < 2:
        return _year_int(top_year), False
    if len(most) > 1 and most[1][1] == top_n:
        return _year_int(top_year), False  # tie → not clear
    return _year_int(top_year), True


def resolve_with_gemini_slogans(crop_candidates, crop_to_slogan, slogan_years,
                                flagged_indices, *, normalize_fn, conf_min=0.70,
                                game_year_by_key=None):
    """Resolve crops where CLIP and Gemini agree.

    Parameters
    ----------
    crop_candidates : dict[int, list[dict]]
        crop_idx → CLIP top-N candidates, best first.  Each candidate is at least
        ``{"year", "slogan", "type"}`` (``overall`` optional, used for logging).
    crop_to_slogan : dict[int, dict]
        crop_idx → {slogan, confidence, index, gemini_idx, dist, anchored}
        (gemini_geometry; ``anchored`` is optional and defaults True — fail-open
        for callers that predate the anchoring gate).
    slogan_years : dict[str, set]
        normalized slogan → set of DB years (the duplicate multimap).  Used only
        as a hint; the actual disambiguation uses the years present in the crop's
        own top-N candidates.
    flagged_indices : set[int]
        Gemini ``index`` values flagged as problematic (cut off / smudged /
        unmatchable).  A crop whose associated Gemini slogan carries a flagged
        index does not AUTO-resolve.
    normalize_fn : callable
        slogan → normalized key (buy_rules._normalize_key).
    conf_min : float
        minimum Gemini confidence for an AUTO resolve.
    game_year_by_key : dict | None
        ``(normalize_fn(slogan), season_year) -> game_date calendar year`` for
        entries whose game year differs from their season year (bowl editions).
        Lets the printed-year rung match a bowl button's marker (season+1) to its
        season-year candidate.  None ⇒ season-year-only matching (unchanged).

    Returns ``{crop_idx: resolution, "telemetry": {...}}`` where ``resolution`` is::

        {year, slogan, type, source, auto, confidence, gemini_slogan, matched_rank}

    ``source`` ∈ {"gemini_auto", "gemini_majority", "gemini_clip_fallback"}.
    Crops not present in the result are Scenario C (manual).
    """
    flagged_indices = flagged_indices or set()
    resolutions = {}
    era_votes = Counter()
    deferred = []  # Scenario B crops, resolved in pass 2
    per_crop = []
    n_low_confidence = 0
    n_unanchored = 0

    # --- Pass 1: Scenario A (unique year) + collect anchors ------------------
    for crop_idx, assoc in crop_to_slogan.items():
        candidates = crop_candidates.get(crop_idx) or []
        g_slogan = assoc.get("slogan") or ""
        norm_g = normalize_fn(g_slogan)
        matches = _matching_candidates(candidates, norm_g, normalize_fn)

        if not matches:
            per_crop.append({
                "crop_idx": crop_idx, "gemini_slogan": g_slogan,
                "gemini_agree": False, "resolved_year": None, "source": "manual",
                "confidence": assoc.get("confidence"),
            })
            continue  # Scenario C — Gemini saw a slogan we don't rank

        conf = assoc.get("confidence")
        anchored = assoc.get("anchored", True)
        gate_ok = ((conf is None or conf >= conf_min)
                   and assoc.get("index") not in flagged_indices
                   and anchored)
        if not anchored:
            n_unanchored += 1
        elif not gate_ok:
            n_low_confidence += 1

        years = []
        for _rank, c in matches:
            yr = c.get("year")
            if yr not in years:
                years.append(yr)

        if len(years) == 1:
            rank, cand = matches[0]
            resolutions[crop_idx] = {
                "year": cand.get("year"),
                "slogan": cand.get("slogan"),
                "type": cand.get("type"),
                "source": "gemini_auto",
                "auto": gate_ok,
                "confidence": conf,
                "gemini_slogan": g_slogan,
                "printed_year": assoc.get("printed_year"),
                "matched_rank": rank,
            }
            era_votes[cand.get("year")] += 1
            per_crop.append({
                "crop_idx": crop_idx, "gemini_slogan": g_slogan,
                "gemini_agree": True, "resolved_year": cand.get("year"),
                "source": "gemini_auto", "confidence": conf, "matched_rank": rank,
            })
        else:
            deferred.append((crop_idx, norm_g, matches, conf, gate_ok, g_slogan,
                             assoc.get("printed_year")))

    # --- Pass 2: Scenario B (repeated slogan) -------------------------------
    # Disambiguation ladder: the button's own PRINTED YEAR marker (when Gemini
    # read one and it matches exactly one candidate — 1983/84 + 1997-2025
    # buttons carry the marker) beats the photo's majority era, which beats
    # CLIP's own ranking.  The printed year is per-button physical evidence;
    # the majority is an inference from the photo's other buttons.
    anchor_year, clear = _majority_year(era_votes)
    n_disambiguated = 0
    n_printed_year = 0
    n_printed_year_gamematch = 0   # printed-year hits resolved via game_date, not season year

    def _game_year_of(c):
        """Candidate's game/printed CALENDAR year from game_year_by_key
        ((normkey(slogan), season_year) -> calendar year), or None."""
        if not game_year_by_key:
            return None
        return game_year_by_key.get(
            (normalize_fn(c.get("slogan") or ""), _year_int(c.get("year"))))

    for crop_idx, norm_g, matches, conf, gate_ok, g_slogan, printed_year in deferred:
        rank = cand = None
        if printed_year is not None:
            # Offset-aware: a bowl edition's marker is its game_date calendar year
            # (season+1), so match printed_year against EITHER the candidate's
            # season year or its game year.  Dateless candidates fall back to
            # season-only, so pre-game_date behaviour is unchanged.
            _py = [(rk, c) for rk, c in matches
                   if edt.printed_year_marker_matches(
                       _year_int(c.get("year")), _game_year_of(c), printed_year)]
            if len(_py) == 1:
                rank, cand = _py[0]
                source = "gemini_printed_year"
                n_printed_year += 1
                if _year_int(cand.get("year")) != printed_year:
                    n_printed_year_gamematch += 1
        if cand is None and clear and anchor_year is not None:
            # candidate (rank, cand) whose year is closest to the majority era
            def _dist(item):
                yi = _year_int(item[1].get("year"))
                return (abs(yi - anchor_year) if yi is not None else 1e9, item[0])
            rank, cand = min(matches, key=_dist)
            source = "gemini_majority"
            n_disambiguated += 1
        elif cand is None:
            rank, cand = matches[0]  # CLIP's own top-ranked match
            source = "gemini_clip_fallback"

        resolutions[crop_idx] = {
            "year": cand.get("year"),
            "slogan": cand.get("slogan"),
            "type": cand.get("type"),
            "source": source,
            "auto": gate_ok,
            "confidence": conf,
            "gemini_slogan": g_slogan,
            "printed_year": printed_year,
            "matched_rank": rank,
        }
        per_crop.append({
            "crop_idx": crop_idx, "gemini_slogan": g_slogan, "gemini_agree": True,
            "resolved_year": cand.get("year"), "source": source,
            "confidence": conf, "matched_rank": rank,
        })

    n_confirmed = sum(1 for r in resolutions.values() if r["auto"])
    telemetry = {
        "n_gemini_confirmed": n_confirmed,
        "n_resolved": len(resolutions),
        "n_disambiguated_by_majority": n_disambiguated,
        "n_printed_year": n_printed_year,
        "n_printed_year_gamematch": n_printed_year_gamematch,
        "n_low_confidence": n_low_confidence,
        "n_unanchored": n_unanchored,
        "n_manual": len(crop_candidates) - len(resolutions),
        "majority_year": anchor_year,
        "majority_clear": clear,
        "era_votes": dict(era_votes),
        "per_crop": per_crop,
    }

    result = dict(resolutions)
    result["telemetry"] = telemetry
    return result


def build_slogan_year_multimap(phrases, years, normalize_fn):
    """{normalized slogan → set(years)} from parallel DB arrays.

    A slogan with more than one year is a duplicate needing Scenario-B
    disambiguation.  Built once at hydration and reused per photo.
    """
    multimap = {}
    for phrase, year in zip(phrases, years):
        key = normalize_fn(phrase)
        if not key:
            continue
        multimap.setdefault(key, set()).add(year)
    return multimap
