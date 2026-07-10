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
