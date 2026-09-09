"""Guards on the Progress Trackers repair path.

The workbook is a ONE-TIME build. It already exists in Sheets with the
operator's typed PROGRESS LOG in it, so `emit_apps_script` repairs cells in
place rather than rebuilding — and that makes the LIVE block's row count a
hard constraint, not a style choice.

Run: python ebayscout/tests/run_goal_tracker_repair_tests.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from ebayscout.tools import build_goal_trackers as b


def test_every_live_block_fits_the_built_workbook():
    """The live config must match the workbook as built — no exceptions."""
    b.check_live_row_budget()


def test_the_guard_fires_when_a_block_grows():
    """One extra reading on A3 would push a formula onto the "Pooled over…"
    note, and the next one onto the PROGRESS LOG header. The log is the
    workbook's whole point, so growing a block must fail loudly."""
    original = b.LIVE["A3"]
    b.LIVE["A3"] = original + [("an added reading", "=1")]
    try:
        b.check_live_row_budget()
        raise AssertionError("the guard did not fire on an over-long block")
    except SystemExit as exc:
        assert "A3: 8 rows, workbook has 7" in str(exc), exc
    finally:
        b.LIVE["A3"] = original


def test_the_guard_fires_when_a_block_shrinks():
    """A short block leaves a stale formula in the row below it, still
    computing the old, wrong number."""
    original = b.LIVE["A3"]
    b.LIVE["A3"] = original[:-1]
    try:
        b.check_live_row_budget()
        raise AssertionError("the guard did not fire on a short block")
    except SystemExit as exc:
        assert "A3: 6 rows, workbook has 7" in str(exc), exc
    finally:
        b.LIVE["A3"] = original


def test_every_budgeted_front_still_has_a_live_block():
    """A front listed in the budget but dropped from LIVE would leave its
    tab's stale formulas untouched by the repair."""
    missing = [f for f in b.LIVE_ROW_BUDGET if f not in b.LIVE]
    assert not missing, f"budgeted fronts with no LIVE block: {missing}"


def test_the_repair_writes_a_label_beside_every_formula():
    """A3's bands are graded human-confirmed now. A cell whose caption still
    reads "[0.82, 0.85)" would be read as the pooled number it no longer is,
    so the repair rewrites column A too."""
    src = open(b.__file__).read()
    assert "setValue(CELLS[i][3])" in src
    for _label, formula in b.LIVE["A3"]:
        assert formula.startswith("="), formula
    labels = [lbl for lbl, _ in b.LIVE["A3"]]
    assert all("human" in lbl.lower() for lbl in labels), labels


def test_bands_are_graded_on_human_confirmations():
    """gemini_auto fires only when CLIP already agreed with Gemini, so a
    pooled band asks the board whether it agrees with itself."""
    for fid in ("A3", "A4"):
        for _label, formula in b.LIVE[fid]:
            assert '"<>gemini*"' in formula and '"<>auto*"' in formula, (fid, formula)


def test_detection_readings_count_images_not_crops():
    """match_log is one row per crop, so an 80-button sheet counts 80 times
    unless the count is pinned to crop_num = 1."""
    crop_col = b.M["crop_num"]
    for fid in ("B22", "B27", "E2"):
        for _label, formula in b.LIVE[fid]:
            assert f'match_log!{crop_col}2:{crop_col}' in formula, (fid, formula)


def test_shadow_columns_do_not_count_their_empty_marker():
    """A JSON column holds "[]" when its shadow did not run — counting it as
    data read A1 at 4170 rows against 310 real ones."""
    for fid in ("A1", "A2", "A7"):
        assert any('"[]"' in f or '"{}"' in f for _l, f in b.LIVE[fid]), fid
