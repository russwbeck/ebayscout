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
