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


def _norm_py(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def test_scenario_b_printed_year_beats_majority():
    """Repeated-slogan disambiguation ladder (2026-07-16): the button's own
    printed year marker beats the photo's majority era."""
    from gemini_resolve import resolve_with_gemini_slogans
    cands = [{"year": 1972, "slogan": "Crush the Orange", "type": "Football"},
             {"year": 1973, "slogan": "Crush the Orange", "type": "Football"}]
    # two anchor crops vote 1972 as the majority era; the printed year says 1973
    anchor = [{"year": 1972, "slogan": f"Anchor {i}", "type": "Football"}
              for i in range(2)]
    res = resolve_with_gemini_slogans(
        {0: [anchor[0]], 1: [anchor[1]], 2: cands},
        {0: {"slogan": "Anchor 0", "confidence": 0.95, "index": 1},
         1: {"slogan": "Anchor 1", "confidence": 0.95, "index": 2},
         2: {"slogan": "Crush the Orange", "confidence": 0.95, "index": 3,
             "printed_year": 1973}},
        {}, set(), normalize_fn=_norm_py)
    r = res[2]
    assert r["source"] == "gemini_printed_year"
    assert r["year"] == 1973 and r["printed_year"] == 1973


def test_scenario_b_falls_back_to_majority_without_marker():
    from gemini_resolve import resolve_with_gemini_slogans
    cands = [{"year": 1972, "slogan": "Crush the Orange", "type": "Football"},
             {"year": 1973, "slogan": "Crush the Orange", "type": "Football"}]
    anchor = [{"year": 1972, "slogan": f"Anchor {i}", "type": "Football"}
              for i in range(2)]
    res = resolve_with_gemini_slogans(
        {0: [anchor[0]], 1: [anchor[1]], 2: cands},
        {0: {"slogan": "Anchor 0", "confidence": 0.95, "index": 1},
         1: {"slogan": "Anchor 1", "confidence": 0.95, "index": 2},
         2: {"slogan": "Crush the Orange", "confidence": 0.95, "index": 3}},
        {}, set(), normalize_fn=_norm_py)
    assert res[2]["source"] == "gemini_majority" and res[2]["year"] == 1972


def test_scenario_a_carries_printed_year_through():
    from gemini_resolve import resolve_with_gemini_slogans
    res = resolve_with_gemini_slogans(
        {0: [{"year": 2019, "slogan": "Idaho'nt Think So", "type": "Football"}]},
        {0: {"slogan": "Idaho'nt Think So", "confidence": 0.95, "index": 1,
             "printed_year": 2019}},
        {}, set(), normalize_fn=_norm_py)
    assert res[0]["printed_year"] == 2019


def test_scenario_b_bowl_offset_resolves_to_season_year():
    """Bowl-year normalization (2026-07-19): Gemini reads the printed GAME year
    (1979) but the edition belongs to the 1978 season.  game_year_by_key lets the
    printed-year rung match 1979 to the season-1978 candidate and emit 1978."""
    from gemini_resolve import resolve_with_gemini_slogans
    cands = [{"year": 1978, "slogan": "Lion Power", "type": "Football"},
             {"year": 1981, "slogan": "Lion Power", "type": "Football"}]
    gyk = {(_norm_py("Lion Power"), 1978): 1979}
    res = resolve_with_gemini_slogans(
        {0: cands},
        {0: {"slogan": "Lion Power", "confidence": 0.95, "index": 1,
             "printed_year": 1979}},
        {}, set(), normalize_fn=_norm_py, game_year_by_key=gyk)
    assert res[0]["source"] == "gemini_printed_year"
    assert res[0]["year"] == 1978
    assert res["telemetry"]["n_printed_year_gamematch"] == 1


def test_scenario_b_bowl_offset_noop_without_map():
    """Without game_year_by_key, printed 1979 matches neither 1978 nor 1981, so
    the printed-year rung does NOT fire — behaviour is unchanged when no game
    dates are loaded."""
    from gemini_resolve import resolve_with_gemini_slogans
    cands = [{"year": 1978, "slogan": "Lion Power", "type": "Football"},
             {"year": 1981, "slogan": "Lion Power", "type": "Football"}]
    res = resolve_with_gemini_slogans(
        {0: cands},
        {0: {"slogan": "Lion Power", "confidence": 0.95, "index": 1,
             "printed_year": 1979}},
        {}, set(), normalize_fn=_norm_py)
    assert res[0]["source"] != "gemini_printed_year"
    assert res["telemetry"]["n_printed_year_gamematch"] == 0


# --- anchoring gate (2026-07-16 shifted-lot incident) --------------------------

def test_unanchored_association_never_autos():
    """A wrong-neighbor pair (Gemini's point 2.6x radius off the crop) may still
    surface as a suggestion but must NOT auto-resolve, even at conf 0.9 — this
    is the exact failure that auto-confirmed blank-bag crops on 1979-front."""
    res = gr.resolve_with_gemini_slogans(
        {0: [_cand("1979", "Wave Good-bye")]},
        {0: {"slogan": "Wave Good-bye", "confidence": 0.9, "index": 12,
             "anchored": False}},
        {"wavegoodbye": {"1979"}}, set(), normalize_fn=_norm)
    r = res[0]
    assert r["source"] == "gemini_auto" and r["auto"] is False
    assert res["telemetry"]["n_unanchored"] == 1


def test_anchored_true_and_absent_both_auto():
    """Fail-open: callers that predate the gate (no `anchored` key) behave
    exactly as before; an explicit anchored=True is identical."""
    for assoc in ({"slogan": "Stop Stanford", "confidence": 0.92},
                  {"slogan": "Stop Stanford", "confidence": 0.92, "anchored": True}):
        res = gr.resolve_with_gemini_slogans(
            {0: [_cand("1984", "Stop Stanford")]}, {0: dict(assoc)},
            {"stopstanford": {"1984"}}, set(), normalize_fn=_norm)
        assert res[0]["auto"] is True
        assert res["telemetry"]["n_unanchored"] == 0


def test_unanchored_scenario_b_also_refused():
    """The gate must hold through the deferred (repeated-slogan) pass too."""
    cands = [_cand("1972", "Crush the Orange"), _cand("1973", "Crush the Orange")]
    res = gr.resolve_with_gemini_slogans(
        {0: cands},
        {0: {"slogan": "Crush the Orange", "confidence": 0.95, "index": 1,
             "anchored": False}},
        {}, set(), normalize_fn=_norm)
    assert res[0]["auto"] is False
    assert res["telemetry"]["n_unanchored"] == 1


def test_anchor_gate_flag_default_on_with_kill_switch():
    """main.py's _anchor_gate_enabled: default ON; BUTTONMATCHER_ANCHOR_GATE=0
    reverts to pre-incident behavior (every association stamped anchored)."""
    import ast
    main_py = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    src = open(main_py).read()
    node = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef)
                and n.name == "_anchor_gate_enabled")
    ns = {"os": os}
    exec(compile(ast.get_source_segment(src, node), main_py, "exec"), ns)
    fn = ns["_anchor_gate_enabled"]
    os.environ.pop("BUTTONMATCHER_ANCHOR_GATE", None)
    assert fn() is True
    os.environ["BUTTONMATCHER_ANCHOR_GATE"] = "0"
    assert fn() is False
    os.environ.pop("BUTTONMATCHER_ANCHOR_GATE", None)
