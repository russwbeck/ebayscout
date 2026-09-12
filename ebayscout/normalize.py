"""
ebayscout/normalize.py

Single slogan-string normalization policy, shared by the Gemini pipeline's
slogan/year multimap (gemini_resolve.build_slogan_year_multimap) and the
two-pass resolver (gemini_resolve.resolve_with_gemini_slogans), and by the
reference-staging entry-id lookup.

Replicates buttonmatcher/buy_rules._normalize_key EXACTLY so the two services
agree on slogan identity (the GCS reference DB + text_db are shared): lowercase
and strip every non-alphanumeric character (spaces, hyphens, apostrophes,
punctuation) so hyphen/space/joined slogan variants share one identity key.
"""

import re

_PUNCT_RE = re.compile(r"[^\w]")


def normalize_key(s) -> str:
    """Lowercase and strip every non-alphanumeric char — 'I-Oh-Was', 'I Oh Was'
    and 'IOhWas' all -> 'iohwas'.  Matches buttonmatcher._normalize_key."""
    return _PUNCT_RE.sub("", str(s).lower())


def is_placeholder_slogan(phrase) -> bool:
    """True for text_db.json placeholder rows like 'Slogan Unknown 3'.

    These are bookkeeping entries: a year/era is recorded but the exact slogan
    text isn't.  They have no reference photo and no real text, so buttonmatcher
    deliberately EXCLUDES them when it encodes `text_features.pt`
    (buttonmatcher/main.py hydrate_data, via `slogan_search.is_placeholder_slogan`).
    ebayscout must know about them for one reason: a placeholder is in
    text_db.json and permanently absent from the cache, so anything that
    cross-checks the two has to skip them or it reports a permanent false
    "stale cache" (see clip_matcher's staleness guard).

    Body kept byte-identical to buttonmatcher/slogan_search.py's copy — that
    file is buttonmatcher-only (it serves the human "type the slogan" lane,
    which ebayscout has no equivalent of), so this is a second implementation
    of one rule.  tests/test_buttonmatcher_parity.py pins the behaviour; change
    both sides in one PR.
    """
    return str(phrase).strip().lower().startswith("slogan unknown")
