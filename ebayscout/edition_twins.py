"""edition_twins — detect Penn State gameday-button "twin" slogans: the same
slogan TEXT reused across multiple editions (different year and/or sport —
e.g. "Crush the Orange" exists as 1972 football AND 1973 football; "Plaster
Pitt" as 1973 football AND 1979 basketball).

Text-agreement auto-confirm (score-based or Gemini-agreement) picks a
candidate slogan correctly but then defaults to whichever EDITION happened to
rank/land first — effectively at random when the slogan is a twin. This
module builds the registry of such families so callers can demote an
edition-level auto-confirm to human review while still trusting the slogan
identification itself.

Pure stdlib, no heavy imports — mirrors label_harvest.py / detect_gate.py as a
style model; safe to import from anywhere without pulling in torch/cv2/Slack.

Callers own all string-normalization policy: pass in ``normalize_fn`` (use
``buy_rules._normalize_key``). Do NOT pass the matching code's
``normalize_slogan`` — that function normalizes MATCH SCORES (a float), not
slogan strings; calling it on a string caused a production outage once.
"""

from __future__ import annotations


def build_twin_registry(entries, normalize_fn):
    """Group ``entries`` by normalized slogan key, keeping only families with
    2+ entries — a "twin" family, where the same slogan text spans multiple
    editions.

    ``entries`` is any iterable of plain dicts, each with at least a
    ``slogan``, a ``year``, and a sport/type field (``type`` if present, else
    ``sport``; defaults to "Football" when neither is present — matches the
    text_db.json convention shared by both repos). Entries with a missing or
    blank slogan are skipped. Callers pass plain dicts; this module never
    imports the callers' text-DB loaders.

    Returns ``{normalized_key: [entry, ...]}`` — the original input dicts,
    unmodified, grouped and filtered. Singleton families (a slogan that only
    ever had one edition) are dropped: they carry no edition ambiguity, so
    there is nothing for a caller to guard.
    """
    families = {}
    for entry in entries:
        slogan = (entry or {}).get("slogan")
        if not slogan or not str(slogan).strip():
            continue
        key = normalize_fn(slogan)
        if not key:
            continue
        families.setdefault(key, []).append(entry)
    return {key: fam for key, fam in families.items() if len(fam) >= 2}


def twin_family(registry, slogan, normalize_fn):
    """The family list for ``slogan``, or None if it isn't a registered twin
    (unknown slogan, or a slogan with only one edition in the registry)."""
    if not slogan:
        return None
    key = normalize_fn(slogan)
    return registry.get(key)


def printed_year_marker_matches(season_year, game_year, printed_year):
    """True if a button whose catalog SEASON year is ``season_year`` — and whose
    game/printed CALENDAR year is ``game_year`` when that is known — would carry
    ``printed_year`` as its on-button marker.

    A regular edition's marker is its season year; a BOWL edition's marker is the
    game's calendar year, which is season+1 (a 1 Jan 1979 Sugar Bowl button
    belongs to the 1978 set but reads "1979").  A candidate matches when
    ``printed_year`` equals EITHER value, which means:

      * behaviour is unchanged when ``game_year`` is None — season-year match
        only, exactly the pre-game_date rule — so the ~99% of buttons whose
        marker already equals the season year are unaffected; and
      * a bowl button resolves once its game_date is loaded (game_year season+1),
        without a special case for "is this a bowl".

    Bad/None ``printed_year`` never matches, so a misread resolves nothing.
    Pure; general — the canonical printed-year test for any consumer, not just
    twins."""
    try:
        py = int(printed_year)
    except (TypeError, ValueError):
        return False
    for candidate in (season_year, game_year):
        if candidate is None:
            continue
        try:
            if int(str(candidate).strip()) == py:
                return True
        except (TypeError, ValueError):
            continue
    return False


def resolve_printed_year(candidates, printed_year, game_year_of=None):
    """The single candidate whose on-button marker equals ``printed_year``, or
    None.  The general form behind :func:`resolve_by_printed_year`, usable by
    any printed-year consumer (twin sequence today; single-candidate validation
    or the year-bias audit in future).

    ``candidates``   — dicts each carrying a ``year`` (catalog SEASON year).
    ``printed_year`` — the int Gemini read off the button (None ⇒ no marker).
    ``game_year_of`` — optional callable(candidate) → that candidate's game/
        printed CALENDAR year (from its game_date), or None when unknown.  Omit
        for season-year-only matching (identical to the pre-game_date rule).

    Returns the unique match, or None when zero or 2+ candidates match — so a
    misread or a genuinely ambiguous pair never resolves a year.  Pure."""
    if printed_year is None or not candidates:
        return None
    hits = [
        c for c in candidates
        if printed_year_marker_matches(
            (c or {}).get("year"),
            (game_year_of(c) if game_year_of else None),
            printed_year,
        )
    ]
    return hits[0] if len(hits) == 1 else None


def resolve_by_printed_year(family, printed_year):
    """The single family edition whose PRINTED year marker equals
    ``printed_year``, or None.

    ``family`` is a twin_family() list (entries with a ``year``);
    ``printed_year`` is the int Gemini read off the button (1983/84 and
    1997-2025 buttons carry a small year marker).  Offset-aware: an edition
    matches when ``printed_year`` equals its season ``year`` OR its game_date
    calendar year (``entry['game_year']``, stamped at hydration when the entry
    has a game_date) — so a bowl edition resolves on its game year while every
    dateless edition keeps the pure season-year rule.  Returns the entry ONLY
    when exactly one edition matches — no match, an unparseable year, or two
    matching editions all return None, so a misread can never resolve a twin.
    Pure; callers keep their fail-open wrappers."""
    return resolve_printed_year(
        family, printed_year,
        game_year_of=lambda e: (e or {}).get("game_year"),
    )


def should_demote(registry, slogan, normalize_fn):
    """True iff ``slogan`` belongs to a registered multi-edition family — the
    pure yes/no at the heart of the auto-confirm guard. Callers wrap this in
    a try/except and fail OPEN (leave today's auto-confirm behavior
    unchanged) so a registry hiccup never blocks an otherwise-good
    auto-confirm; this function itself never raises for well-formed input."""
    return twin_family(registry, slogan, normalize_fn) is not None


def registry_summary(registry):
    """Short human-readable line for the startup log, e.g.
    '12 slogan families with multiple editions (27 entries)'."""
    n_families = len(registry)
    n_entries = sum(len(fam) for fam in registry.values())
    return f"{n_families} slogan families with multiple editions ({n_entries} entries)"
