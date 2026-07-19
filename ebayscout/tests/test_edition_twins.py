"""Unit tests for edition_twins (pure — no torch/cv2/Slack imports)."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import edition_twins as edt


def _norm(s):
    """Mirror buy_rules._normalize_key without importing it (strip every
    non-alphanumeric char so hyphen/space/joined variants collapse equal)."""
    return re.sub(r"[^\w]", "", str(s).lower())


def _entry(id_, slogan, year, type_="Football"):
    return {"id": id_, "slogan": slogan, "year": year, "type": type_}


# --- build_twin_registry ------------------------------------------------------

def test_build_registry_groups_duplicate_slogans():
    entries = [
        _entry("1", "Crush the Orange", 1972),
        _entry("2", "Crush the Orange", 1973),
        _entry("3", "Beat Pitt", 1990),
    ]
    reg = edt.build_twin_registry(entries, _norm)
    assert set(reg.keys()) == {"crushtheorange"}
    assert len(reg["crushtheorange"]) == 2


def test_build_registry_drops_singleton_families():
    entries = [
        _entry("1", "Crush the Orange", 1972),
        _entry("2", "Crush the Orange", 1973),
        _entry("3", "Only Once", 1985),
    ]
    reg = edt.build_twin_registry(entries, _norm)
    assert "onlyonce" not in reg
    assert len(reg) == 1


def test_build_registry_skips_missing_or_blank_slogan():
    entries = [
        _entry("1", "Crush the Orange", 1972),
        _entry("2", "Crush the Orange", 1973),
        {"id": "3", "year": 1990, "type": "Football"},   # no 'slogan' key
        _entry("4", "", 1991),                            # blank slogan
        _entry("5", "   ", 1992),                         # whitespace-only slogan
    ]
    reg = edt.build_twin_registry(entries, _norm)
    assert set(reg.keys()) == {"crushtheorange"}


def test_build_registry_normalization_insensitive_to_punctuation_case():
    entries = [
        _entry("1", "I-Oh-Was", 1972),
        _entry("2", "i oh was", 1973),
    ]
    reg = edt.build_twin_registry(entries, _norm)
    assert set(reg.keys()) == {"iohwas"}
    assert len(reg["iohwas"]) == 2


def test_build_registry_cross_sport_twin():
    # "Plaster Pitt" 1973 football and 1979 basketball — the motivating case.
    entries = [
        _entry("1", "Plaster Pitt", 1973, "Football"),
        _entry("2", "Plaster Pitt", 1979, "Basketball"),
    ]
    reg = edt.build_twin_registry(entries, _norm)
    fam = reg["plasterpitt"]
    assert {e["type"] for e in fam} == {"Football", "Basketball"}


def test_build_registry_empty_input():
    assert edt.build_twin_registry([], _norm) == {}


# --- twin_family --------------------------------------------------------------

def test_twin_family_lookup_hit_and_normalization():
    entries = [_entry("1", "Crush the Orange", 1972), _entry("2", "Crush the Orange", 1973)]
    reg = edt.build_twin_registry(entries, _norm)
    fam = edt.twin_family(reg, "CRUSH, THE ORANGE!", _norm)
    assert fam is not None
    assert len(fam) == 2


def test_twin_family_lookup_miss_singleton_or_unknown():
    entries = [_entry("1", "Crush the Orange", 1972), _entry("2", "Crush the Orange", 1973),
               _entry("3", "Beat Navy", 1985)]
    reg = edt.build_twin_registry(entries, _norm)
    assert edt.twin_family(reg, "Beat Navy", _norm) is None       # singleton, dropped
    assert edt.twin_family(reg, "Nonexistent Slogan", _norm) is None
    assert edt.twin_family(reg, None, _norm) is None
    assert edt.twin_family(reg, "", _norm) is None


# --- should_demote --------------------------------------------------------------

def test_should_demote_true_for_twin_family():
    entries = [_entry("1", "Plaster Pitt", 1973), _entry("2", "Plaster Pitt", 1979, "Basketball")]
    reg = edt.build_twin_registry(entries, _norm)
    assert edt.should_demote(reg, "Plaster Pitt", _norm) is True


def test_should_demote_false_for_singleton_or_unknown():
    entries = [_entry("1", "Plaster Pitt", 1973), _entry("2", "Plaster Pitt", 1979, "Basketball")]
    reg = edt.build_twin_registry(entries, _norm)
    assert edt.should_demote(reg, "Beat Navy", _norm) is False
    assert edt.should_demote(reg, None, _norm) is False


# --- registry_summary ----------------------------------------------------------

def test_registry_summary_counts_families_and_entries():
    entries = [
        _entry("1", "Crush the Orange", 1972),
        _entry("2", "Crush the Orange", 1973),
        _entry("3", "Plaster Pitt", 1973),
        _entry("4", "Plaster Pitt", 1979, "Basketball"),
        _entry("5", "Beat Navy", 1985),   # singleton, excluded from registry
    ]
    reg = edt.build_twin_registry(entries, _norm)
    summary = edt.registry_summary(reg)
    assert summary == "2 slogan families with multiple editions (4 entries)"


def test_registry_summary_empty():
    assert edt.registry_summary({}) == "0 slogan families with multiple editions (0 entries)"


def test_resolve_by_printed_year_unique_match():
    fam = [{"slogan": "Crush the Orange", "year": 1972, "type": "Football"},
           {"slogan": "Crush the Orange", "year": 1973, "type": "Football"}]
    import edition_twins as edt
    hit = edt.resolve_by_printed_year(fam, 1973)
    assert hit is fam[1]


def test_resolve_by_printed_year_no_match_or_ambiguous_returns_none():
    import edition_twins as edt
    fam = [{"year": 1972}, {"year": 1973}]
    assert edt.resolve_by_printed_year(fam, 1984) is None    # not in family
    assert edt.resolve_by_printed_year(fam, None) is None    # no marker read
    assert edt.resolve_by_printed_year([], 1972) is None     # empty family
    dup = [{"year": 2015}, {"year": 2015}]                   # pathological
    assert edt.resolve_by_printed_year(dup, 2015) is None


def test_resolve_by_printed_year_tolerates_string_years():
    import edition_twins as edt
    fam = [{"year": "1972"}, {"year": "1973"}, {"year": "n/a"}]
    assert edt.resolve_by_printed_year(fam, 1972) is fam[0]


# --- printed_year_marker_matches (the shared primitive) -----------------------

def test_marker_matches_season_year():
    # regular edition: marker == season year
    assert edt.printed_year_marker_matches(1990, None, 1990) is True
    assert edt.printed_year_marker_matches(1990, None, 1991) is False


def test_marker_matches_bowl_game_year():
    # bowl edition: season 1978, game/printed calendar year 1979 -> marker "1979"
    assert edt.printed_year_marker_matches(1978, 1979, 1979) is True
    # its own season year still matches too (either value is accepted)
    assert edt.printed_year_marker_matches(1978, 1979, 1978) is True
    # an unrelated year matches neither
    assert edt.printed_year_marker_matches(1978, 1979, 1980) is False


def test_marker_matches_bad_inputs_never_match():
    assert edt.printed_year_marker_matches(1978, 1979, None) is False
    assert edt.printed_year_marker_matches(1978, 1979, "junk") is False
    assert edt.printed_year_marker_matches(None, None, 1978) is False
    assert edt.printed_year_marker_matches("n/a", None, 1978) is False


# --- resolve_printed_year (general) + game_year on resolve_by_printed_year -----

def test_resolve_by_printed_year_resolves_bowl_edition_to_season_year():
    # Gemini reads the bowl game's printed year (1979); the correct edition is
    # the 1978 season entry, distinguished only by its game_year.
    fam = [
        {"slogan": "Lion Power", "year": 1978, "type": "Football", "game_year": 1979},
        {"slogan": "Lion Power", "year": 1981, "type": "Football", "game_year": None},
    ]
    hit = edt.resolve_by_printed_year(fam, 1979)
    assert hit is fam[0]          # resolved to the SEASON-1978 bowl edition
    assert hit["year"] == 1978


def test_resolve_by_printed_year_dateless_unchanged():
    # No game_year anywhere -> pure season-year matching, exactly as before.
    fam = [{"year": 1972}, {"year": 1973}]
    assert edt.resolve_by_printed_year(fam, 1973) is fam[1]
    assert edt.resolve_by_printed_year(fam, 1979) is None


def test_resolve_by_printed_year_ambiguous_game_vs_season_defers():
    # A real 1979 season edition AND a 1978 bowl edition (game year 1979) both
    # match printed 1979 -> two hits -> defer (None), never resolve wrong.
    fam = [
        {"slogan": "X", "year": 1978, "game_year": 1979},
        {"slogan": "X", "year": 1979, "game_year": None},
    ]
    assert edt.resolve_by_printed_year(fam, 1979) is None


def test_resolve_printed_year_general_with_callable():
    cands = [{"slogan": "X", "year": 1978}, {"slogan": "X", "year": 1981}]
    game_year = {("x", 1978): 1979}
    hit = edt.resolve_printed_year(
        cands, 1979,
        game_year_of=lambda c: game_year.get((c["slogan"].lower(), c["year"])))
    assert hit is cands[0]
    # no callable -> season-only; 1979 matches neither
    assert edt.resolve_printed_year(cands, 1979) is None
