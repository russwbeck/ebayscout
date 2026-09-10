"""Parity lock: the values and formulas ebayscout MUST keep equal to buttonmatcher.

WHY THIS EXISTS
---------------
Some code is shared byte-for-byte between the two services (`match_logging.py`,
`detect_scale.py`, `detect_gate.py`, `sheet_retry.py`, ...) and a plain `diff`
catches drift there.  But the *matching* path is not one file: buttonmatcher
scores inside `main.py`, ebayscout inside `clip_matcher.py` + `scoring.py` +
`config.py`.  Those files carry comments promising they "mirror buttonmatcher
EXACTLY" — and a comment cannot fail.

It already drifted once.  `rerank.py`'s YEAR_WEIGHT/SID_WEIGHT were recalibrated
0.05 -> 0.02 in buttonmatcher on 2026-09-07 (replay over 1,416 confirmations:
at 0.05 a maximally wrong signal flips 251 of 1,297 correct #1s) and ebayscout
kept the discredited seeds until 2026-09-10.  Both services rank against the
same shared reference DB and write to the same Sheet, so a divergent constant
does not fail loudly — it silently makes the two leaderboards incomparable and
corrupts every pooled analysis drawn from them.

WHAT IT DOES
------------
Pins the golden values on ebayscout's side, each naming the buttonmatcher source
it mirrors.  This runs with no cv2/torch and without buttonmatcher checked out,
so it works in CI and in a web session.  It cannot see buttonmatcher, so it
cannot detect drift introduced on THAT side — when a value below is changed in
buttonmatcher deliberately, change it here in the same PR.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from ebayscout import config
from ebayscout import normalize
from ebayscout import rerank
from ebayscout import scoring


# --- Blend + confidence tiers (buttonmatcher/main.py score_slogans + :139-151) --

def test_blend_weights():
    """ALPHA/BETA — buttonmatcher/main.py:593 `(ALPHA * image) + (BETA * slogan)`."""
    assert config.ALPHA == 0.5
    assert config.BETA == 0.5


def test_confidence_thresholds():
    """buttonmatcher/main.py confidence_emoji + the auto gate."""
    assert config.GREEN_THRESHOLD == 0.82
    assert config.RED_THRESHOLD == 0.65
    assert config.GREEN_GAP == 0.12
    assert config.AUTO_RESOLVE_THRESHOLD == 0.85


def test_confidence_emoji_boundaries():
    """Exact boundary behaviour, not just the constants."""
    assert scoring.confidence_emoji(0.82) == "🟢"
    assert scoring.confidence_emoji(0.819) == "🟡"
    assert scoring.confidence_emoji(0.65) == "🟡"
    assert scoring.confidence_emoji(0.649) == "🔴"
    # a #1 that leads by GREEN_GAP is green even when its own score is weak
    assert scoring.confidence_emoji(0.40, gap=0.12) == "🟢"
    assert scoring.confidence_emoji(0.40, gap=0.119) == "🔴"


def test_is_confirmed_is_auto_or_green_or_gap_green():
    assert scoring.is_confirmed(0.85)
    assert scoring.is_confirmed(0.82)
    assert not scoring.is_confirmed(0.819)
    assert scoring.is_confirmed(0.50, 0.12)
    assert not scoring.is_confirmed(0.50, 0.119)
    assert not scoring.is_confirmed(0.50, None)


# --- Tokenization + rarity (buttonmatcher/main.py tokenize / rarity_weight) -----

def test_tokenize_collapses_apostrophes_before_word_extraction():
    """Logger_14: without this "I-O-Wasn't" tokenized to [i, o, wasn, t] and a
    typed search scored ~0.067. Straight AND typographic apostrophes."""
    assert scoring.tokenize("I-O-Wasn't") == ["i", "o", "wasnt"]
    assert scoring.tokenize("I-O-Wasn’t") == ["i", "o", "wasnt"]
    assert scoring.tokenize("Lion's Court") == ["lions", "court"]
    assert scoring.tokenize("Skin-ya, West Virginia") == ["skin", "ya", "west", "virginia"]
    assert scoring.tokenize("") == []


def test_stopwords_are_generic_english_only():
    """Domain words (pitt, lion, state) are differentiators and must NOT be
    suppressed — buttonmatcher/main.py:581."""
    assert scoring.STOPWORDS == {
        "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "of",
        "its", "is",
    }
    for w in ("pitt", "lion", "lions", "state", "penn"):
        assert w not in scoring.STOPWORDS


def test_rarity_weight_is_inverse_square():
    """1/freq² — buttonmatcher/main.py:637-641."""
    scoring.word_freq.clear()
    scoring.word_freq.update({"bowling": 1, "pair": 2, "common": 5})
    assert scoring.rarity_weight("bowling") == 1.0
    assert scoring.rarity_weight("pair") == 0.25
    assert scoring.rarity_weight("common") == 0.04
    assert scoring.rarity_weight("never-seen") == 1.0   # absent => freq 1


def test_rarity_bonus_formula_and_cap():
    """0.04 * mean(rarity) capped at 0.04 — buttonmatcher/main.py:1774-1780,
    where it is inline in build_leaderboard rather than a named function."""
    scoring.word_freq.clear()
    scoring.word_freq.update({"mellon": 40, "bank": 40, "teepees": 1, "upset": 1})
    # all-rare phrase saturates the cap
    assert abs(scoring.rarity_bonus("Upset the Teepees") - 0.04) < 1e-12
    # all-common phrase earns almost nothing
    assert scoring.rarity_bonus("Mellon Bank") < 0.001
    # stopwords-only earns exactly nothing
    assert scoring.rarity_bonus("the a an of") == 0.0
    assert scoring.rarity_bonus("") == 0.0
    # never exceeds the cap
    scoring.word_freq.clear()
    assert scoring.rarity_bonus("aaa bbb ccc ddd") <= 0.04


# --- Slogan identity (buttonmatcher/buy_rules.py _normalize_key) ---------------

def test_normalize_key_matches_buy_rules():
    """The two services share the reference DB / text_db, so slogan identity
    must resolve the same on both sides."""
    assert normalize.normalize_key("I-Oh-Was") == "iohwas"
    assert normalize.normalize_key("I Oh Was") == "iohwas"
    assert normalize.normalize_key("IOhWas") == "iohwas"
    assert normalize.normalize_key("Don’t Volunteer") == "dontvolunteer"
    assert normalize.normalize_key("PSU — A Pinning Tradition") == "psuapinningtradition"
    assert normalize.normalize_key("") == ""
    assert normalize.normalize_key(42) == "42"
    # \w keeps underscores and unicode letters, exactly like re.sub(r"[^\w]", "")
    assert normalize.normalize_key("a_b") == "a_b"
    assert normalize.normalize_key("Café") == "café"


# --- Re-rank weights (buttonmatcher/rerank.py) ---------------------------------

def test_rerank_weights_stay_calibrated():
    """CALIBRATED 2026-09-07 over 1,416 confirmations. This is the value that
    actually drifted: ebayscout sat at the discredited 0.05 seeds for three
    days. Raising these needs a fresh replay against the REAL Year/SloganID
    scores, in BOTH repos, in the same PR."""
    assert rerank.YEAR_WEIGHT == 0.02
    assert rerank.SID_WEIGHT == 0.02


def test_rerank_is_off_by_default():
    """Opt-in until calibrated, both services."""
    for var in ("EBAYSCOUT_RERANK", "BUTTONMATCHER_RERANK"):
        os.environ.pop(var, None)
    assert not rerank.rerank_enabled()


def test_rerank_honours_either_service_flag():
    """ebayscout's own name wins, but the shared buttonmatcher name also works
    so one rollback toggle can cover both services."""
    for var in ("EBAYSCOUT_RERANK", "BUTTONMATCHER_RERANK"):
        os.environ.pop(var, None)
    try:
        os.environ["BUTTONMATCHER_RERANK"] = "1"
        assert rerank.rerank_enabled()
        os.environ.pop("BUTTONMATCHER_RERANK")
        os.environ["EBAYSCOUT_RERANK"] = "1"
        assert rerank.rerank_enabled()
    finally:
        for var in ("EBAYSCOUT_RERANK", "BUTTONMATCHER_RERANK"):
            os.environ.pop(var, None)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_"):
            _fn()
    print("all buttonmatcher-parity tests passed")
