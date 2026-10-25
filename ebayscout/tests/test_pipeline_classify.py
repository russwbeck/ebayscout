"""
Tests for ebayscout/pipeline_classify.py — the deal-alert helpers the legacy
CLIP scan uses (lot_value_and_deal, lookalike_note).  The Gemini pipeline's
decision tree, DB-direct tier and staging funnel went with the pipeline on
2026-10-25, and their tests with them.  Pure python.

    python tests/run_pipeline_classify_tests.py
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import pipeline_classify as pc


# --- lot_value_and_deal -----------------------------------------------------

_PRICES = {("1984", "Stop Stanford"): 30.0, ("1990", "Beat Pitt"): 12.0}


def _price_of(year, slogan):
    return _PRICES.get((year, slogan), 0.0)


def test_lot_value_sums_matched_prices():
    auto = [{"year": "1984", "slogan": "Stop Stanford"},
            {"year": "1990", "slogan": "Beat Pitt"}]
    value, undervalued, margin = pc.lot_value_and_deal(auto, _price_of, asking=25.0)
    assert value == 42.0
    assert undervalued is True
    assert margin == 17.0


def test_lot_value_not_undervalued_when_asking_exceeds_value():
    auto = [{"year": "1990", "slogan": "Beat Pitt"}]   # 12.0
    value, undervalued, margin = pc.lot_value_and_deal(auto, _price_of, asking=25.0)
    assert value == 12.0 and undervalued is False and margin == -13.0


def test_lot_value_unpriced_buttons_count_zero():
    auto = [{"year": "2099", "slogan": "Unknown"}]      # no price rule
    value, undervalued, _ = pc.lot_value_and_deal(auto, _price_of, asking=5.0)
    assert value == 0.0 and undervalued is False


def test_lot_value_no_asking_never_undervalued():
    auto = [{"year": "1984", "slogan": "Stop Stanford"}]
    value, undervalued, _ = pc.lot_value_and_deal(auto, _price_of, asking=None)
    assert value == 30.0 and undervalued is False


# --- Look-alike value flag ----------------------------------------------------
#
# ebayscout has no human-review lane (`_post_yellow_review` is defined but never
# called; the pipeline posts deals only), so buttonmatcher's confusable guard —
# which demotes an auto-confirm to a human picker — has nowhere to demote to.
# Applying it here would silently DROP the match. What survives the difference
# is the reason the guard exists: a same-year look-alike moves the two numbers
# the alert reports, `amount_needed` and `max_price_single`. So ebayscout flags
# the deal post instead of demoting the match.

_LA_FAM_EERS = [
    {"slogan": "'Eers to Penn State", "year": "1992", "type": "Football"},
    {"slogan": "Penn State and Proud of it", "year": "1992", "type": "Football"},
]

# The real 2026-09-03 pair: same year, same sport, both dominated by the words
# "Penn State" — one needed and worth $30, the other owned already and worth $12.
_LA_PRICES = {
    ("1992", "'Eers to Penn State"):        (30.0, 1),
    ("1992", "Penn State and Proud of it"): (12.0, 0),
}


def _la_priced(year, slogan):
    return _LA_PRICES.get((str(year), slogan), (0.0, 0))


def _la_norm(s):
    import re
    return re.sub(r"[^\w]", "", str(s).lower())


def test_lookalike_note_names_the_sibling_and_the_value_swing():
    note = pc.lookalike_note("'Eers to Penn State", "1992", _LA_FAM_EERS,
                             _la_priced, _la_norm)
    assert note is not None
    assert [a["slogan"] for a in note["alternatives"]] == ["Penn State and Proud of it"]
    assert note["price_here"] == 30.0
    assert note["value_swing"] == 18.0        # 30 - 12: what the lot value rides on
    assert note["need_here"] == 1
    assert note["any_not_needed"] is True     # the alert could be entirely spurious


def test_lookalike_note_excludes_the_match_itself():
    """The family contains the winner's own entries; only OTHER slogans warn."""
    note = pc.lookalike_note("Penn State and Proud of it", "1992", _LA_FAM_EERS,
                             _la_priced, _la_norm)
    assert [a["slogan"] for a in note["alternatives"]] == ["'Eers to Penn State"]
    assert note["any_not_needed"] is False    # the alternative IS needed


def test_lookalike_note_matches_on_normalized_identity():
    """Punctuation and spacing must not smuggle the winner back in as its own
    look-alike — the two services share a reference DB where the same slogan is
    written several ways."""
    fam = [{"slogan": "I-Oh-Was", "year": "1980"},
           {"slogan": "I Oh Was", "year": "1980"},
           {"slogan": "Real Other", "year": "1980"}]
    prices = {("1980", "I-Oh-Was"): (5.0, 1), ("1980", "I Oh Was"): (5.0, 1),
              ("1980", "Real Other"): (25.0, 0)}
    note = pc.lookalike_note("IOhWas", "1980", fam,
                             lambda y, s: prices.get((str(y), s), (0.0, 0)), _la_norm)
    assert [a["slogan"] for a in note["alternatives"]] == ["Real Other"]


def test_lookalike_note_is_silent_when_the_answer_does_not_change():
    """Same price AND same standing need on every alternative: which one it is
    cannot change the buy decision, so it must not spend a line on the alert."""
    fam = [{"slogan": "A", "year": "1992"}, {"slogan": "B", "year": "1992"}]
    same = {("1992", "A"): (30.0, 1), ("1992", "B"): (30.0, 1)}
    assert pc.lookalike_note("A", "1992", fam,
                             lambda y, s: same.get((str(y), s), (0.0, 0)),
                             _la_norm) is None


def test_lookalike_note_warns_on_need_alone_even_at_the_same_price():
    """Equal money, different need — still worth saying, because the alert only
    exists because something was needed."""
    fam = [{"slogan": "A", "year": "1992"}, {"slogan": "B", "year": "1992"}]
    prices = {("1992", "A"): (30.0, 1), ("1992", "B"): (30.0, 0)}
    note = pc.lookalike_note("A", "1992", fam,
                             lambda y, s: prices.get((str(y), s), (0.0, 0)), _la_norm)
    assert note is not None
    assert note["value_swing"] == 0.0
    assert note["any_not_needed"] is True


def test_lookalike_note_orders_by_price_and_caps_the_list():
    fam = [{"slogan": f"S{i}", "year": "1990"} for i in range(7)]
    prices = {("1990", f"S{i}"): (float(i), 0) for i in range(7)}
    note = pc.lookalike_note("S0", "1990", fam,
                             lambda y, s: prices.get((str(y), s), (0.0, 0)),
                             _la_norm, max_listed=3)
    assert [a["slogan"] for a in note["alternatives"]] == ["S6", "S5", "S4"]


def test_lookalike_note_returns_none_without_a_family():
    assert pc.lookalike_note("Anything", "1990", [], _la_priced, _la_norm) is None
    assert pc.lookalike_note("Anything", "1990", None, _la_priced, _la_norm) is None
    assert pc.lookalike_note("", "1990", _LA_FAM_EERS, _la_priced, _la_norm) is None


def test_lookalike_note_skips_entries_with_no_slogan():
    fam = [{"year": "1992"}, {"slogan": "", "year": "1992"},
           {"slogan": "Real", "year": "1992"}]
    prices = {("1992", "Win"): (10.0, 1), ("1992", "Real"): (40.0, 0)}
    note = pc.lookalike_note("Win", "1992", fam,
                             lambda y, s: prices.get((str(y), s), (0.0, 0)), _la_norm)
    assert [a["slogan"] for a in note["alternatives"]] == ["Real"]
