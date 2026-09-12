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
    # B2/B4/B11/B14/B28 were missed by the 2026-09-07 sweep and stayed
    # per-crop: B28 read 983 whitepass "lots" against 54, B14 51 swaps
    # against 5.
    for fid in ("B2", "B4", "B11", "B14", "B22", "B27", "B28", "E2"):
        for _label, formula in b.LIVE[fid]:
            assert f'match_log!{crop_col}2:{crop_col}' in formula, (fid, formula)


def test_confirm_type_counts_ignore_the_untyped_blank():
    """`chosen_type` is blank on a confirmation that resolved no type, and
    COUNTA scored those as data — A11 read 483 non-football confirms
    against 5, and C4 inherited the same denominator."""
    for fid in ("A11", "C4"):
        for _label, formula in b.LIVE[fid]:
            assert "COUNTA" not in formula, (fid, formula)
            assert '"?*"' in formula, (fid, formula)


def test_shadow_columns_do_not_count_their_empty_marker():
    """A JSON column holds "[]" when its shadow did not run — counting it as
    data read A1 at 4170 rows against 310 real ones."""
    for fid in ("A1", "A2", "A7"):
        assert any('"[]"' in f or '"{}"' in f for _l, f in b.LIVE[fid]), fid


def test_no_bare_not_blank_criterion_on_a_pasted_column():
    """`COUNTIFS(..., "<>")` does not mean "has a value" on a pasted tab.

    An export writes a zero-length STRING into an empty field, and that is not
    blank, so the criterion counts it. B22's "lots where the match could not
    run (blank != zero)" read 0 against 157 -- the one cell whose whole caption
    is about blanks -- and E2's >=98% gate read 74.4% against 79.6% on a
    denominator of 215 lots instead of the 201 carrying a Gemini count.
    ISNUMBER is the test that separates a written number from an empty paste.
    """
    for fid, block in b.LIVE.items():
        for label, formula in block:
            assert '"<>"' not in formula, (fid, label, formula)


def test_b7_and_b14_report_the_same_two_facts_per_image():
    """B7 is B14's sibling -- it proposes widening exactly the population B14
    acts on -- so the two tabs must not disagree about how big that population
    is. Both were per-crop (698 and 51 against 65 and 5); B14 was fixed first
    and B7 was missed, which would have put two different numbers for one fact
    on two tabs."""
    crop_col = b.M["crop_num"]
    for label, formula in b.LIVE["B7"][:2]:
        assert f'match_log!{crop_col}2:{crop_col}' in formula, (label, formula)
    b7_pop = b.LIVE["B7"][0][1]
    b14_pop = b.LIVE["B14"][1][1]
    assert b7_pop == b14_pop, (b7_pop, b14_pop)


def test_the_repair_never_writes_into_a_content_tab_and_never_blocks():
    """Two failure modes from the 2026-09-12 runs, in the reporting tail.

    The status line fell back to `ss.getSheets()[0]` when there was no REPAIR
    tab — that is INDEX, and it overwrote INDEX!A4, the "Fronts per stage" row
    label. And `getUi().alert()` does NOT throw when the script is
    container-bound: it opens a modal and blocks until someone clicks, so a
    run nobody is watching burns the whole 6-minute quota and dies with
    "Exceeded maximum execution time" long after the 87 writes succeeded.
    """
    import tempfile
    fronts = b.parse_register(os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "LOGGER_FRONTS.md"))
    with tempfile.NamedTemporaryFile("r+", suffix=".gs") as fh:
        b.emit_apps_script(fronts, fh.name)
        gs = open(fh.name).read()
    # Assert on the EMITTED script, not the generator: the generator's own
    # comments name both hazards, so grepping the source would pass on prose.
    code = "\n".join(ln for ln in gs.splitlines()
                     if not ln.lstrip().startswith(("//", "*", "/*")))
    assert "getSheets()[0]" not in code, "status write can still land on INDEX"
    assert "getUi" not in code, "a modal in a headless run blocks to timeout"
    assert "insertSheet('REPAIR')" in code, "REPAIR tab must be created, not fallen back from"
    assert "SpreadsheetApp.flush();" in code.split("insertSheet('REPAIR')")[1], \
        "the status write must be flushed, or a kill discards it"


def test_both_registers_parse_and_bold_labels_carry_their_colon():
    """The register is the workbook's source of truth — a bullet that breaks
    the parser silently eats the field after it.

    `parse_register` reads fields as `- **Name:** value`, non-greedily, with
    re.S. A bullet whose bold label has NO colon inside the asterisks makes
    that scan run on until it finds the next `:**` — swallowing whatever field
    follows. It cost three rounds of "C5: missing Source" while writing this
    review up, and the failure is invisible until the build runs.
    """
    import re
    here = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    registers = [os.path.join(here, "LOGGER_FRONTS.md")]
    sibling = os.path.join(os.path.dirname(here), "buttonmatcher",
                           "LOGGER_FRONTS.md")
    if os.path.exists(sibling):          # only when both repos are checked out
        registers.append(sibling)

    for path in registers:
        fronts = b.parse_register(path)      # SystemExits on a broken field
        assert len(fronts) >= 69, (path, len(fronts))
        text = open(path).read()
        # Inside a front's entry, every bulleted bold label needs its colon.
        for chunk in re.split(r"\n(?=### )", text):
            if not chunk.startswith("### "):
                continue
            fid = chunk.split(" ", 2)[1]
            for line in chunk.splitlines():
                if re.match(r"^\s*-\s+\*\*", line):
                    assert re.match(r"^\s*-\s+\*\*[^*]+:\*\*", line), (
                        f"{os.path.basename(path)} {fid}: bold label without a "
                        f"colon will swallow the next field — {line[:70]}")


def test_the_repair_falls_back_to_the_front_id_when_a_title_changes():
    """A tab's name is derived from the front's TITLE, and titles change.

    A25 was renamed "Edition-twin wrong-year picks" -> "Edition-twin
    resolution" on 2026-09-09. The generated tab name stopped matching the
    deployed tab, so its two LIVE cells would have been written nowhere —
    reported under MISSING TABS at best, and silently stale if nobody read the
    line. A front's ID never changes, so the script matches on the "A25 "
    prefix as a fallback and names every tab it reached that way, so the
    operator knows to rename it.
    """
    import tempfile
    fronts = b.parse_register(os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "LOGGER_FRONTS.md"))
    with tempfile.NamedTemporaryFile("r+", suffix=".gs") as fh:
        b.emit_apps_script(fronts, fh.name)
        gs = open(fh.name).read()
    assert "byId" in gs, "no front-ID fallback in the emitted script"
    assert "MATCHED BY FRONT ID" in gs, "the fallback must report itself"
    # the fallback must not silently replace the MISSING TABS report
    assert "MISSING TABS" in gs
