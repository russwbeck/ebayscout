"""
ebayscout/scoring.py

Pure-python scoring helpers shared by the CLIP matcher: the confidence tiers
(GREEN/AUTO gate) and the rarity tiebreaker. Kept dependency-free (stdlib +
config) so they are unit-testable without torch/clip/cv2.

`word_freq` is populated by clip_matcher.init() once the slogan set is loaded;
until then rarity_weight() treats every word as frequency 1.

All constants/formulas mirror buttonmatcher/main.py EXACTLY so ebayscout's
confidence tiers and logged leaderboards match buttonmatcher's.
"""

import re
from collections import Counter

from . import config

# Word → number of distinct slogans it appears in. Populated in clip_matcher.init().
word_freq: Counter = Counter()


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens — identical to buttonmatcher/main.py tokenize().

    Apostrophes (straight AND typographic) are collapsed BEFORE word
    extraction so contraction puns keep their distinctive token ("I-O-Wasn't"
    → [i, o, wasnt], matching what users type — Logger_14 diagnosis; the old
    split [i, o, wasn, t] made typed searches score ~0.067)."""
    return re.findall(r"\b[a-z0-9]+\b",
                      text.lower().replace("'", "").replace("’", ""))


# Generic English stop words only — domain words like 'pitt', 'lion', 'state'
# are meaningful differentiators and must NOT be suppressed (buttonmatcher:188).
STOPWORDS = {"a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "of", "its", "is"}


def rarity_weight(word: str) -> float:
    """1/freq² — superlinear boost for truly rare words (buttonmatcher:190-198)."""
    freq = max(word_freq.get(word, 1), 1)
    return 1.0 / (freq ** 2)


def rarity_bonus(phrase: str) -> float:
    """Capped (≤0.04) tiebreaker bonus for a slogan's distinctive words.
    Returns 0.0 when the phrase has only stopwords. Mirrors build_leaderboard."""
    words = set(tokenize(phrase)) - STOPWORDS
    if not words:
        return 0.0
    return min(0.04 * sum(rarity_weight(w) for w in words) / len(words), 0.04)


# --- Confidence tiers (verbatim from buttonmatcher/main.py:139-151) ------------

def confidence_emoji(overall, *, gap=None) -> str:
    """🟢/🟡/🔴 for a candidate's overall score.

    Green when the absolute score is strong OR (for the #1 candidate only) it
    leads the runner-up by a decisive ``gap``; red when weak; yellow in between.
    """
    if overall >= config.GREEN_THRESHOLD or (gap is not None and gap >= config.GREEN_GAP):
        return "🟢"
    if overall < config.RED_THRESHOLD:
        return "🔴"
    return "🟡"


def is_confirmed(overall: float, gap: float | None = None) -> bool:
    """True when a crop's top match is auto-confirmed or 'in the green'.

    The single gate ebayscout uses to decide a button is identified: AUTO
    (>=AUTO_RESOLVE_THRESHOLD) or GREEN (>=GREEN_THRESHOLD, or a #1-vs-#2 gap
    >= GREEN_GAP).
    """
    return (
        overall >= config.AUTO_RESOLVE_THRESHOLD
        or overall >= config.GREEN_THRESHOLD
        or (gap is not None and gap >= config.GREEN_GAP)
    )
