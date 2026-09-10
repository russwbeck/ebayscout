"""confusable_slogans — curated groups of DIFFERENT slogans the matcher cannot
separate on its own, so a score-based auto-confirm must not pick between them.

This is the INVERSE of edition_twins. A twin family is ONE slogan text reused
across several editions (different year and/or sport); the slogan is trusted
and only the edition needs a human. A confusable group is several DIFFERENT
slogans whose identity is the thing in doubt.

Why the auto-confirm gates cannot catch these on their own
----------------------------------------------------------
``score_slogans`` (main.py) iterates over YEARS and keeps only the best-scoring
slogan within each year::

    for year, image_score in year_scores.items():
        best_local_idx = int(np.argmax(local_sims))   # one survivor per year

``match_logging.build_leaderboard`` folds the same way (``best_by_year``). The
catalog averages ~12 slogans per year, so ~92% of it is invisible on every
leaderboard at every depth, and a same-year sibling can NEVER appear as a
second row. That matters because every signal the auto gates use is an
ACROSS-year comparison — the #1->#2 gap, the slogan-level gap, the
visual-disagreement veto — and a losing same-year slogan has no row to
disagree from. Worse, the surviving row inherits its year's image score, which
is legitimately high when the year is right, so a wrong within-year slogan pick
arrives at the gate looking like a confident, well-separated match.

2026-09-03 (Slack thread p1788484142952169): a "'Eers to Penn State" button
(1992, West Virginia) auto-confirmed as "Penn State and Proud of it" (1992,
Stanford). Both 1992 Football; both dominated by the words "Penn State". The
1992 row's image score was correct and high, #2 was a different YEAR, so the
score cleared AUTO_RESOLVE_THRESHOLD with a wide gap. log_analysis.md had
already recorded this exact pair twice (overall 0.910 in Logger_21, 0.870 in
Logger_18) — both times caught by ``gemini_auto``, which the /sort path does
not run.

Same-year is necessary, not sufficient
--------------------------------------
Do NOT add a group just because two slogans share a year. On 2026-09-03 a
12-button 1987 board carried all four of the 1987 same-year pairs from
log_analysis.md's FLAGGED table and matched every one correctly, seven on AUTO.
Within a year the fold picks on CLIP TEXT similarity alone — ``image_score`` is
a per-year max, identical for every slogan of that year, so it cannot break a
within-year tie — which means the risk tracks SHARED SALIENT TEXT. "Eers to
Penn State" and "Penn State and Proud of it" share the bigram "Penn State", the
most legible text on the button; none of the 1987 pairs share a token. Every
entry here costs a human tap on an otherwise-automatable button, so require an
OBSERVED mis-fire, not a structural resemblance.

Why a curated list rather than a rule
-------------------------------------
The general fix is a within-year margin ("demote when the runner-up slogan
inside #1's own year scores close"), but every year has same-year siblings, so
such a rule needs a calibrated threshold — and the ⛔ directive above
main.py's threshold block requires a pooled Logger batch before any gate moves.
The ``within_year_json`` match-log column was added alongside this module to
collect exactly that evidence. Until it exists, listing an observed pair costs
one extra human tap on a slogan already known to mis-fire, and costs nothing
anywhere else.

Pure stdlib, no heavy imports — mirrors edition_twins.py / detect_gate.py as a
style model; safe to import from anywhere without pulling in torch/cv2/Slack.

Callers own all string-normalization policy: pass in ``normalize_fn`` (use
``buy_rules._normalize_key``). Do NOT pass the matching code's
``normalize_slogan`` — that function normalizes MATCH SCORES (a float), not
slogan strings.
"""

from __future__ import annotations


# Each inner list is one group of slogans that must never be auto-confirmed
# over one another. Slogan text is matched through ``normalize_fn``, so
# punctuation and casing here need not match the catalog exactly.
#
# Keep this list SHORT and evidence-backed: add a group only when a wrong
# auto-confirm (or a repeatedly wrong #1) has actually been observed between
# its members. Every entry costs a human tap on an otherwise-automatable
# button.
CONFUSABLE_GROUPS = [
    # 2026-09-03, Slack p1788484142952169 — shipped as a wrong AUTO on /sort.
    # Also log_analysis.md "FLAGGED" rows 0.910 (Logger_21) and 0.870
    # (Logger_18), both caught by gemini_auto. Same year (1992), same sport,
    # both read "Penn State" as their most legible text.
    ["Eers to Penn State", "Penn State and Proud of it"],
]


def build_confusable_registry(groups, entries, normalize_fn):
    """Map each listed slogan to the catalog entries of its WHOLE group.

    ``groups`` is an iterable of slogan-text lists (see ``CONFUSABLE_GROUPS``).
    ``entries`` is any iterable of plain dicts carrying at least ``slogan``,
    ``year`` and a ``type`` — the same entry dicts
    ``edition_twins.build_twin_registry`` consumes, so callers can build both
    registries from one list. A slogan that appears in the catalog under
    several entries contributes all of them (a listed slogan that is itself an
    edition twin brings its editions along, which is what the picker should
    offer).

    Returns ``{normalized_key: [entry, ...]}`` where every member key of a
    group maps to the SAME combined entry list, so a lookup on whichever slogan
    happened to rank #1 offers the human the full group.

    A group is dropped unless at least two of its slogans resolve to catalog
    entries: a typo or a retired slogan must never produce a "picker" with one
    real option.
    """
    by_key = {}
    for entry in entries or []:
        slogan = (entry or {}).get("slogan")
        if not slogan or not str(slogan).strip():
            continue
        key = normalize_fn(slogan)
        if key:
            by_key.setdefault(key, []).append(entry)

    registry = {}
    for group in groups or []:
        keys = []
        for slogan in group or []:
            key = normalize_fn(slogan) if slogan else ""
            if key and key not in keys:
                keys.append(key)
        resolved = [k for k in keys if by_key.get(k)]
        if len(resolved) < 2:
            continue
        members = []
        for key in resolved:
            members.extend(by_key[key])
        for key in resolved:
            registry[key] = members
    return registry


def confusable_family(registry, slogan, normalize_fn):
    """The group entry list for ``slogan``, or None when it isn't a registered
    confusable (unknown slogan, or one no group lists)."""
    if not slogan:
        return None
    key = normalize_fn(slogan)
    return registry.get(key)


def should_demote(registry, slogan, normalize_fn):
    """True iff ``slogan`` belongs to a registered confusable group — the pure
    yes/no at the heart of the auto-confirm guard. Callers wrap this in a
    try/except and fail OPEN (leave today's auto-confirm behaviour unchanged)
    so a registry hiccup never blocks an otherwise-good auto-confirm; this
    function itself never raises for well-formed input."""
    return confusable_family(registry, slogan, normalize_fn) is not None


def registry_summary(registry):
    """Short human-readable line for the startup log, e.g.
    '1 confusable slogan group (2 entries)'."""
    n_entries = len({id(e) for fam in registry.values() for e in fam})
    n_groups = len({tuple(sorted(id(e) for e in fam)) for fam in registry.values()})
    plural = "" if n_groups == 1 else "s"
    return f"{n_groups} confusable slogan group{plural} ({n_entries} entries)"
