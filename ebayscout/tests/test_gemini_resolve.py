"""Unit tests for gemini_resolve — pure-python, synthetic candidate lists.

    python tests/run_gemini_resolve_tests.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gemini_resolve as gr


def _norm(s):
    """Mirror buy_rules._normalize_key without importing it (strip every
    non-alphanumeric char so hyphen/space/joined variants collapse equal)."""
    return re.sub(r"[^\w]", "", str(s).lower())


def _cand(year, slogan, overall=0.5, type_="Football"):
    return {"year": year, "slogan": slogan, "overall": overall, "type": type_}


# --- Scenario A: unique-year agreement, high confidence ----------------------

def test_scenario_a_confirms_and_autos():
    crop_candidates = {0: [_cand("1984", "Stop Stanford"), _cand("1990", "Beat Pitt")]}
    crop_to_slogan = {0: {"slogan": "Stop Stanford", "confidence": 0.92}}
    slogan_years = {"stopstanford": {"1984"}}
    out = gr.resolve_with_gemini_slogans(
        crop_candidates, crop_to_slogan, slogan_years, set(), normalize_fn=_norm)
    r = out[0]
    assert r["year"] == "1984" and r["source"] == "gemini_auto"
    assert r["auto"] is True
    assert r["matched_rank"] == 0
    assert out["telemetry"]["n_gemini_confirmed"] == 1


def test_scenario_a_promotes_lower_ranked_match():
    # CLIP's #1 is wrong; Gemini's slogan matches the rank-2 candidate → promote.
    crop_candidates = {0: [_cand("1990", "Beat Pitt"), _cand("1984", "Stop Stanford")]}
    crop_to_slogan = {0: {"slogan": "Stop Stanford", "confidence": 0.9}}
    out = gr.resolve_with_gemini_slogans(
        crop_candidates, crop_to_slogan, {"stopstanford": {"1984"}}, set(), normalize_fn=_norm)
    assert out[0]["year"] == "1984"
    assert out[0]["matched_rank"] == 1


def test_low_confidence_resolves_but_not_auto():
    crop_candidates = {0: [_cand("1984", "Stop Stanford")]}
    crop_to_slogan = {0: {"slogan": "Stop Stanford", "confidence": 0.40}}
    out = gr.resolve_with_gemini_slogans(
        crop_candidates, crop_to_slogan, {"stopstanford": {"1984"}}, set(), normalize_fn=_norm,
        conf_min=0.70)
    assert out[0]["year"] == "1984"
    assert out[0]["auto"] is False   # below the gate → needs a click
    assert out["telemetry"]["n_low_confidence"] == 1
    assert out["telemetry"]["n_gemini_confirmed"] == 0


def test_flagged_index_not_auto():
    # Gemini flagged this button's index → resolves but does not auto-confirm.
    crop_candidates = {0: [_cand("1984", "Stop Stanford")]}
    crop_to_slogan = {0: {"slogan": "Stop Stanford", "confidence": 0.99, "index": 3}}
    flagged_indices = {3}
    out = gr.resolve_with_gemini_slogans(
        crop_candidates, crop_to_slogan, {"stopstanford": {"1984"}}, flagged_indices,
        normalize_fn=_norm)
    assert out[0]["year"] == "1984"
    assert out[0]["auto"] is False
    # a different flagged index does NOT suppress this button
    crop_to_slogan2 = {0: {"slogan": "Stop Stanford", "confidence": 0.99, "index": 1}}
    out2 = gr.resolve_with_gemini_slogans(
        crop_candidates, crop_to_slogan2, {"stopstanford": {"1984"}}, {99},
        normalize_fn=_norm)
    assert out2[0]["auto"] is True


def test_missing_confidence_defaults_to_auto():
    crop_candidates = {0: [_cand("1984", "Stop Stanford")]}
    crop_to_slogan = {0: {"slogan": "Stop Stanford", "confidence": None}}
    out = gr.resolve_with_gemini_slogans(
        crop_candidates, crop_to_slogan, {"stopstanford": {"1984"}}, set(), normalize_fn=_norm)
    assert out[0]["auto"] is True   # back-compat: no gate when confidence absent


# --- Scenario B: repeated slogan, disambiguate by photo majority -------------

def test_scenario_b_disambiguates_by_majority():
    # Three buttons. Two anchor unambiguously to 1980 (majority era).
    # The third's slogan "Beat Pitt" exists in both 1980 and 1990 → choose 1980.
    crop_candidates = {
        0: [_cand("1980", "Whip the Wolfpack")],
        1: [_cand("1980", "Stop Stanford")],
        2: [_cand("1990", "Beat Pitt"), _cand("1980", "Beat Pitt")],
    }
    crop_to_slogan = {
        0: {"slogan": "Whip the Wolfpack", "confidence": 0.9},
        1: {"slogan": "Stop Stanford", "confidence": 0.9},
        2: {"slogan": "Beat Pitt", "confidence": 0.9},
    }
    slogan_years = {"beatpitt": {"1980", "1990"}}
    out = gr.resolve_with_gemini_slogans(
        crop_candidates, crop_to_slogan, slogan_years, set(), normalize_fn=_norm)
    assert out[2]["year"] == "1980"
    assert out[2]["source"] == "gemini_majority"
    assert out["telemetry"]["n_disambiguated_by_majority"] == 1
    assert out["telemetry"]["majority_year"] == 1980


def test_scenario_b_no_majority_falls_back_to_clip():
    # Single button, repeated slogan, no anchors → fall back to CLIP top match.
    crop_candidates = {0: [_cand("1990", "Beat Pitt"), _cand("1980", "Beat Pitt")]}
    crop_to_slogan = {0: {"slogan": "Beat Pitt", "confidence": 0.9}}
    out = gr.resolve_with_gemini_slogans(
        crop_candidates, crop_to_slogan, {"beatpitt": {"1980", "1990"}}, set(), normalize_fn=_norm)
    assert out[0]["year"] == "1990"   # CLIP's own #1
    assert out[0]["source"] == "gemini_clip_fallback"


# --- Scenario C: pure miss ---------------------------------------------------

def test_scenario_c_no_agreement_left_manual():
    crop_candidates = {0: [_cand("1984", "Stop Stanford")]}
    crop_to_slogan = {0: {"slogan": "Totally Different", "confidence": 0.9}}
    out = gr.resolve_with_gemini_slogans(
        crop_candidates, crop_to_slogan, {}, set(), normalize_fn=_norm)
    assert 0 not in out                       # not resolved
    assert out["telemetry"]["n_manual"] == 1
    pc = out["telemetry"]["per_crop"][0]
    assert pc["gemini_agree"] is False and pc["source"] == "manual"


def test_crop_with_no_gemini_association_is_manual():
    crop_candidates = {0: [_cand("1984", "X")], 1: [_cand("1990", "Y")]}
    crop_to_slogan = {0: {"slogan": "X", "confidence": 0.9}}  # crop 1 has no gemini
    out = gr.resolve_with_gemini_slogans(
        crop_candidates, crop_to_slogan, {"x": {"1984"}}, set(), normalize_fn=_norm)
    assert 0 in out and 1 not in out
    assert out["telemetry"]["n_manual"] == 1


# --- build_slogan_year_multimap ----------------------------------------------

def test_build_multimap_flags_duplicates():
    phrases = ["Beat Pitt", "beat pitt", "Stop Stanford"]
    years = ["1980", "1990", "1984"]
    mm = gr.build_slogan_year_multimap(phrases, years, _norm)
    assert mm["beatpitt"] == {"1980", "1990"}
    assert mm["stopstanford"] == {"1984"}


# --- hyphenated opponent puns (the "Iowa" class) -----------------------------

def test_hyphen_space_join_variants_resolve():
    """A hyphenated DB slogan must resolve for Gemini reads that split it with
    spaces or join it — the whole point of the non-alphanumeric-stripping key.
    Before the fix, 'I-Oh-Was' normalized to 'iohwas' while 'I Oh Was' stayed
    'i oh was', so the two never matched and the pun dropped to manual."""
    for gemini_read in ("I-Oh-Was", "I Oh Was", "IOhWas", "i oh was"):
        crop_candidates = {0: [_cand("1984", "I-Oh-Was")]}
        crop_to_slogan = {0: {"slogan": gemini_read, "confidence": 0.9}}
        out = gr.resolve_with_gemini_slogans(
            crop_candidates, crop_to_slogan, {"iohwas": {"1984"}}, set(),
            normalize_fn=_norm)
        assert out[0]["year"] == "1984", f"gemini read {gemini_read!r} missed"
        assert out[0]["auto"] is True


def test_normalize_key_collapses_pun_variants():
    """The shared identity key: hyphen, space and joined forms are one key."""
    assert _norm("I-Oh-Was") == _norm("I Oh Was") == _norm("IOhWas") == "iohwas"
    assert _norm("I-O-Won't") == _norm("IO Wont") == "iowont"
    assert _norm("I-O-Wouldn't") == "iowouldnt"
    assert _norm("Indi-gestion") == _norm("Indigestion") == "indigestion"
    assert _norm("Panthers' Pittfall") == _norm("Panthers Pittfall") == "pantherspittfall"
