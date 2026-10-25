"""Parity lock: the values and formulas ebayscout MUST keep equal to buttonmatcher.

WHY THIS EXISTS
---------------
Some code is shared byte-for-byte between the two services (`match_logging.py`,
`detect_scale.py`, `sheet_retry.py`, ...) and a plain `diff`
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


# --- Retired 2026-10-25 with the Gemini pipeline ---------------------------
# The bowl-year map (front A26) fed the shared Gemini resolver, and the
# detector comparison (front B31) kept detect_pipeline.py in step with
# buttonmatcher's detect.py.  ebayscout runs neither any more: its lots go to
# buttonmatcher (yolo_test.py).


# --- placeholder slogans (buttonmatcher/slogan_search.py is not shared) -------

def test_placeholder_slogan_predicate_matches_buttonmatcher():
    """`normalize.is_placeholder_slogan` mirrors buttonmatcher's copy.

    buttonmatcher drops 'Slogan Unknown N' rows when it encodes
    text_features.pt (main.py hydrate_data, via slogan_search).  ebayscout
    needs the same predicate so its staleness guard doesn't count those rows
    as a stale cache forever.  slogan_search.py serves the human typed-slogan
    lane and is buttonmatcher-only, so the rule exists twice; these cases pin
    it on this side.
    """
    for phrase in ("Slogan Unknown", "Slogan Unknown 3", "slogan unknown 12",
                   "SLOGAN UNKNOWN 5", "  Slogan Unknown 1  "):
        assert normalize.is_placeholder_slogan(phrase), phrase
    for phrase in ("Beat Michigan", "Year Unknown", "The Unknown Soldier",
                   "unknown slogan", "", None):
        assert not normalize.is_placeholder_slogan(phrase), phrase


def test_placeholder_predicate_body_is_byte_identical_to_buttonmatcher():
    """Same one-line body on both sides, or this is real drift."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    theirs = os.path.join(os.path.dirname(here), "buttonmatcher",
                          "slogan_search.py")
    if not os.path.exists(theirs):
        print("    SKIP: buttonmatcher not checked out alongside")
        return
    import ast
    want = None
    for node in ast.parse(open(theirs).read()).body:
        if isinstance(node, ast.FunctionDef) and node.name == "is_placeholder_slogan":
            want = ast.dump(ast.Module(body=[n for n in node.body
                                             if not _is_docstring(n)],
                                       type_ignores=[]))
    assert want is not None, "buttonmatcher lost is_placeholder_slogan"
    mine = None
    src = os.path.join(here, "ebayscout", "normalize.py")
    for node in ast.parse(open(src).read()).body:
        if isinstance(node, ast.FunctionDef) and node.name == "is_placeholder_slogan":
            mine = ast.dump(ast.Module(body=[n for n in node.body
                                             if not _is_docstring(n)],
                                       type_ignores=[]))
    assert mine == want, (
        "is_placeholder_slogan has DRIFTED between the two repos:\n"
        f"  ebayscout/normalize.py : {mine}\n"
        f"  buttonmatcher/slogan_search.py : {want}")


def _is_docstring(node):
    import ast
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def test_the_staleness_guard_ignores_placeholders():
    """Reproduces the arithmetic of clip_matcher's guard on the live data.

    The live warning read "6 text_db.json entries not found in the cache" for
    months; `/reference check` said 0 were unencoded.  Both were right — the 6
    were placeholders.  With them excluded the guard reports clean.
    """
    db = {(1985, normalize.normalize_key("Beat Michigan")): "1",
          (1986, normalize.normalize_key("Slogan Unknown 3")): "755",
          (1987, normalize.normalize_key("Slogan Unknown 4")): "888"}
    placeholders = {k for k, _ in db.items()
                    if k[0] in (1986, 1987)}   # what the loop records
    cache = {(1985, normalize.normalize_key("Beat Michigan"))}
    pending = len(set(db) - placeholders - cache)
    stale = len(cache - (set(db) - placeholders))
    assert (stale, pending) == (0, 0)
    # and without the exclusion, the same data warns — the old false positive
    assert len(set(db) - cache) == 2


def test_clip_matcher_actually_excludes_placeholders_in_the_guard():
    """The fix lives in clip_matcher.py, which needs torch and can't be
    imported here, so assert on its source: the guard must subtract the
    placeholder set rather than only mention it in a comment."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    src = open(os.path.join(here, "ebayscout", "clip_matcher.py")).read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "_placeholder_keys = set()" in code
    assert "_placeholder_keys.add(" in code
    assert ("_db_keys = set(_slogan_key_to_entry.keys()) - _placeholder_keys"
            in code), "the staleness guard no longer excludes placeholders"
    assert "if not normalize.is_placeholder_slogan(_p)" in code, \
        "the cached side of the guard no longer excludes placeholders"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_"):
            _fn()
    print("all buttonmatcher-parity tests passed")


# --- The un-fold (match_logging.year_slogan_rows, shared byte-for-byte) --------

def _unfold_norm(s, min_s=0.15, max_s=0.35):
    return max(0.0, min(1.0, (s - min_s) / (max_s - min_s)))


def test_unfold_constants_stay_calibrated():
    """M_UNFOLD = 0.10 and K = 4 come from the 2026-09-18 replay: 64-68 of the
    96 rank-1-usurper rows become visible, at 4 of 187 correct gap-rule autos
    withheld.  Moving either needs a fresh replay in BOTH repos in one PR, the
    same rule rerank's weights carry."""
    from ebayscout import match_logging as ml
    assert ml.UNFOLD_MARGIN == 0.10
    assert ml.UNFOLD_CAP == 4


def test_unfold_is_on_by_default_and_killable():
    from ebayscout import match_logging as ml
    os.environ.pop("BUTTONMATCHER_UNFOLD", None)
    assert ml.unfold_enabled(), "the un-fold should be live once shipped"
    try:
        os.environ["BUTTONMATCHER_UNFOLD"] = "0"
        assert not ml.unfold_enabled()
    finally:
        os.environ.pop("BUTTONMATCHER_UNFOLD", None)


def test_both_scorers_keep_the_same_sibling():
    """clip_matcher._score_slogans and buttonmatcher's score_slogans both defer
    to year_slogan_rows, so the sibling set cannot drift.  Pinned on a tie,
    where an unstable sort would diverge between the two services."""
    from ebayscout import match_logging as ml
    rows = [(0.26, "Zulu", ""), (0.26, "Alpha", ""), (0.19, "Far", "")]
    kept = ml.year_slogan_rows(rows, normalize_fn=_unfold_norm, enabled=True)
    assert [p for _r, p, _t in kept] == ["Alpha", "Zulu"], (
        "a tie must break deterministically or the logged leaderboard stops "
        "equalling the live one")


def test_the_unfolded_sibling_never_leads_its_year():
    """The invariant the cost was priced on, asserted on the shared sort key:
    a sibling lands under the row that displaced it, never above it."""
    from ebayscout import match_logging as ml
    winner = {"year": "1992", "overall": 0.6955, "phrase": "Penn State and Proud of it"}
    sibling = {"year": "1992", "overall": 0.7215, "phrase": "Undo EMU"}
    ordered = sorted([sibling, winner],
                     key=lambda r: ml.sort_key(
                         r, None if r is winner else winner["overall"]))
    assert ordered[0] is winner and ordered[1] is sibling
