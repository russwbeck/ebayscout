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


def resolve_by_printed_year(family, printed_year):
    """The single family edition whose year equals the button's PRINTED year
    marker, or None.

    ``family`` is a twin_family() list (entries with a ``year``);
    ``printed_year`` is the int Gemini read off the button (1983/84 and
    1997-2025 buttons carry a small year marker).  Returns the matching entry
    ONLY when exactly one edition matches — no match, an unparseable year, or
    (pathologically) two same-year editions all return None, so a misread can
    never resolve a twin.  Pure; callers keep their fail-open wrappers."""
    if printed_year is None or not family:
        return None
    hits = []
    for entry in family:
        try:
            if int(str((entry or {}).get("year")).strip()) == int(printed_year):
                hits.append(entry)
        except (TypeError, ValueError):
            continue
    return hits[0] if len(hits) == 1 else None


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
