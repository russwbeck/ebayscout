"""Unit tests for confusable_slogans (pure — no torch/cv2/Slack imports)."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from ebayscout import confusable_slogans as cfs
from ebayscout import edition_twins as edt


def _norm(s):
    """Mirror buy_rules._normalize_key without importing it (strip every
    non-alphanumeric char so hyphen/space/joined variants collapse equal)."""
    return re.sub(r"[^\w]", "", str(s).lower())


def _entry(id_, slogan, year, type_="Football"):
    return {"id": id_, "slogan": slogan, "year": year, "type": type_}


# The real 2026-09-03 pair: same year, same sport, both read "Penn State".
_EERS = _entry("e1", "Eers to Penn State", 1992)
_PROUD = _entry("e2", "Penn State and Proud of it", 1992)
_OTHER = _entry("e3", "Catnipped", 1992)


# --- build_confusable_registry -----------------------------------------------

def test_group_members_map_to_the_whole_group():
    reg = cfs.build_confusable_registry(
        [["Eers to Penn State", "Penn State and Proud of it"]],
        [_EERS, _PROUD, _OTHER], _norm)
    # Whichever member ranked #1, the human is offered both.
    for slogan in ("Eers to Penn State", "Penn State and Proud of it"):
        fam = cfs.confusable_family(reg, slogan, _norm)
        assert fam is not None
        assert {e["id"] for e in fam} == {"e1", "e2"}


def test_unlisted_slogan_is_not_confusable():
    reg = cfs.build_confusable_registry(
        [["Eers to Penn State", "Penn State and Proud of it"]],
        [_EERS, _PROUD, _OTHER], _norm)
    assert cfs.confusable_family(reg, "Catnipped", _norm) is None
    assert cfs.confusable_family(reg, "Never Badger A Lion", _norm) is None


def test_normalization_absorbs_punctuation_and_case():
    # The button reads "'Eers to Penn State"; the catalog has no apostrophe.
    reg = cfs.build_confusable_registry(
        [["'EERS TO PENN STATE!", "penn state and proud of it"]],
        [_EERS, _PROUD], _norm)
    assert cfs.confusable_family(reg, "Eers to Penn State", _norm) is not None


def test_group_dropped_when_fewer_than_two_slogans_resolve():
    # A typo'd or retired slogan must never produce a one-option "picker".
    reg = cfs.build_confusable_registry(
        [["Eers to Penn State", "Slogan That Does Not Exist"]],
        [_EERS, _PROUD], _norm)
    assert reg == {}
    assert cfs.confusable_family(reg, "Eers to Penn State", _norm) is None


def test_empty_and_malformed_input_is_tolerated():
    assert cfs.build_confusable_registry([], [_EERS], _norm) == {}
    assert cfs.build_confusable_registry(None, None, _norm) == {}
    assert cfs.build_confusable_registry([[]], [_EERS], _norm) == {}
    assert cfs.build_confusable_registry(
        [["Eers to Penn State", "Penn State and Proud of it"]],
        [None, {}, {"slogan": ""}, _EERS, _PROUD], _norm) != {}


def test_listed_slogan_brings_all_its_catalog_editions():
    # A confusable member that is itself an edition twin offers both editions,
    # so the picker never silently drops one.
    proud_79 = _entry("e4", "Penn State and Proud of it", 1979)
    reg = cfs.build_confusable_registry(
        [["Eers to Penn State", "Penn State and Proud of it"]],
        [_EERS, _PROUD, proud_79], _norm)
    fam = cfs.confusable_family(reg, "Eers to Penn State", _norm)
    assert {e["id"] for e in fam} == {"e1", "e2", "e4"}


# --- should_demote ------------------------------------------------------------

def test_should_demote_matches_family_lookup():
    reg = cfs.build_confusable_registry(
        [["Eers to Penn State", "Penn State and Proud of it"]],
        [_EERS, _PROUD, _OTHER], _norm)
    assert cfs.should_demote(reg, "Penn State and Proud of it", _norm) is True
    assert cfs.should_demote(reg, "Catnipped", _norm) is False
    assert cfs.should_demote(reg, "", _norm) is False
    assert cfs.should_demote(reg, None, _norm) is False


# --- the shipped registry -----------------------------------------------------

def test_shipped_groups_are_well_formed():
    # Each group needs 2+ distinct slogans, or it can never demote anything.
    assert cfs.CONFUSABLE_GROUPS
    for group in cfs.CONFUSABLE_GROUPS:
        keys = {_norm(s) for s in group}
        assert len(keys) >= 2, group
        assert all(s and str(s).strip() for s in group), group


def test_shipped_registry_covers_the_2026_09_03_incident():
    reg = cfs.build_confusable_registry(
        cfs.CONFUSABLE_GROUPS, [_EERS, _PROUD, _OTHER], _norm)
    # The wrong auto-confirmed slogan must demote; that is the whole fix.
    assert cfs.should_demote(reg, "Penn State and Proud of it", _norm) is True
    assert cfs.should_demote(reg, "Eers to Penn State", _norm) is True


def test_registry_summary_counts_groups_not_keys():
    reg = cfs.build_confusable_registry(
        [["Eers to Penn State", "Penn State and Proud of it"]],
        [_EERS, _PROUD], _norm)
    # Two keys, but ONE group of two entries.
    assert len(reg) == 2
    assert cfs.registry_summary(reg) == "1 confusable slogan group (2 entries)"


# --- picker labels (edition_twins.picker_labels, shared by both pickers) -------

def test_same_year_group_labels_by_slogan():
    # Without this a same-year confusable group renders two identical buttons.
    labels = edt.picker_labels([_EERS, _PROUD])
    assert labels == ["1. 1992 Eers to Penn State",
                      "2. 1992 Penn State and Proud of it"]
    assert len(set(labels)) == 2


def test_edition_family_keeps_year_sport_labels():
    fam = [_entry("a", "Crush the Orange", 1972),
           _entry("b", "Crush the Orange", 1973)]
    assert edt.picker_labels(fam) == ["1. 1972 Football", "2. 1973 Football"]


def test_cross_sport_edition_family_keeps_year_sport_labels():
    fam = [_entry("a", "Plaster Pitt", 1973, "Football"),
           _entry("b", "Plaster Pitt", 1979, "Basketball")]
    assert edt.picker_labels(fam) == ["1. 1973 Football", "2. 1979 Basketball"]


def test_labels_stay_inside_slack_button_limit():
    long_slogan = "A" * 200
    fam = [_entry("a", long_slogan, 1992), _entry("b", "B" * 200, 1992)]
    labels = edt.picker_labels(fam)
    assert all(len(lbl) <= 70 for lbl in labels)
    assert all(lbl.endswith("…") for lbl in labels)


def test_labels_are_index_aligned_and_one_per_entry():
    fam = [_EERS, _PROUD, _OTHER]
    labels = edt.picker_labels(fam)
    assert len(labels) == len(fam)
    assert [lbl.split(".")[0] for lbl in labels] == ["1", "2", "3"]


def test_picker_labels_tolerates_empty_and_missing_fields():
    assert edt.picker_labels([]) == []
    assert edt.picker_labels(None) == []
    # No slogan to fall back on → year+type, never a crash.
    assert edt.picker_labels([{"year": 1992}, {"year": 1992}]) == [
        "1. 1992 Football", "2. 1992 Football"]
