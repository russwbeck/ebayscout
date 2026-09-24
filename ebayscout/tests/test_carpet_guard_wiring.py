"""Carpet guard wiring (2026-09-24): process_pipeline_lot stamps ``off_board``
on Gemini-positioned crops that sit off the button mask, before DB-direct and
the resolver read the associations.  The verdict itself
(gemini_geometry.assoc_off_board) and the resolver gate are unit-tested in
test_gemini_geometry.py / test_gemini_resolve.py; this checks main.py uses
them, in order.  main.py imports the heavy stack, so it is read via ast.

Run: python tests/run_carpet_guard_wiring_tests.py
"""

import ast
import os

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(PKG, "main.py")).read()
_TREE = ast.parse(_SRC)


def _src(name):
    f = next(n for n in ast.walk(_TREE)
             if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_SRC, f) or ""


def test_guard_stamps_before_db_direct_and_the_resolver():
    src = _src("process_pipeline_lot")
    stamp = src.index('["off_board"] = True')
    assert "ggeo.assoc_off_board(" in src and "ggeo.gemini_positioned(" in src
    assert stamp < src.index("pipeline_classify.gemini_db_candidates(")
    assert stamp < src.index("gres.resolve_with_gemini_slogans(")


def test_db_direct_skips_an_off_board_crop():
    src = _src("process_pipeline_lot")
    at = src.index("pipeline_classify.gemini_db_candidates(")
    assert '_assoc.get("off_board")' in src[at - 400:at]


def test_guard_has_the_shared_kill_switch():
    assert "BUTTONMATCHER_CARPET_GUARD" in _src("_carpet_guard_enabled")
    assert "_carpet_guard_enabled()" in _src("process_pipeline_lot")
