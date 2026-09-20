"""Guards on the Progress Trackers repair path.

The workbook is a ONE-TIME build. It already exists in Sheets with the
operator's typed PROGRESS LOG in it, so `emit_apps_script` repairs cells in
place rather than rebuilding — and that makes the LIVE block's row count a
hard constraint, not a style choice.

Run: python ebayscout/tests/run_goal_tracker_repair_tests.py
"""

import json
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
    # B3/B20/B21/B25/B30 were missed again by the 2026-09-12 build: B3 read
    # 3601 fused "lots" and 5774 dense ones against a 100-LOT gate (INDEX
    # showed 3601% complete), B30 7289 Hough "lots" on ~900 images, and B20
    # summed a per-photo recovery count once per crop.  Fused and dense lots
    # are the worst case for this bug, because the weight IS the lot size the
    # front is measuring.
    # B9 and B19 were missed a third time, by the 2026-09-20 sweep: B9's first
    # cell said "Lots" and counted crops while the distribution directly below
    # it counted images, so one two-row block disagreed with itself (67.0% vs
    # ~73%), and both of B19's readings were weighted by lot size.
    for fid in ("B2", "B3", "B4", "B9", "B11", "B14", "B19", "B20", "B21",
                "B22", "B25", "B27", "B28", "B30", "E2"):
        for _label, formula in b.LIVE[fid]:
            assert f'match_log!{crop_col}2:{crop_col}' in formula, (fid, formula)


def test_no_live_cell_can_spill_out_of_its_one_row():
    """A grouped QUERY returns a row per value; a LIVE block budgets a fixed
    number of rows. The two cannot both be true.

    Three cells shipped with that contradiction and failed two different
    ways: A24 and B9 were refused and rendered `#REF!` from the day the
    workbook was built, while B29 FIT — so it spilled instead, writing its
    counts down column C over the "Pooled over…" note and into the PROGRESS
    LOG's `n` column. B29 is the reason this is a test and not a comment: a
    spill that currently fits is one new value away from `#REF!`, and in the
    meantime it is silently overwriting the log.

    TEXTJOIN consumes an array rather than spilling it, so a distribution
    collapsed that way is safe at any number of values.
    """
    for fid, rows in b.LIVE.items():
        for label, formula in rows:
            if "QUERY(" not in formula:
                continue
            assert "group by" not in formula, (
                f"{fid} {label!r}: a grouped QUERY spills into the rows "
                f"below it — collapse it with TEXTJOIN (see `dist`)")


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
    prefix as a fallback. It then RENAMES the tab to the canonical name
    rather than just reporting it: every formula the script writes refers to
    tabs by that name, Sheets rewrites existing references when a sheet is
    renamed, and a mismatch left in place comes back on the next run.
    """
    import tempfile
    fronts = b.parse_register(os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "LOGGER_FRONTS.md"))
    with tempfile.NamedTemporaryFile("r+", suffix=".gs") as fh:
        b.emit_apps_script(fronts, fh.name)
        gs = open(fh.name).read()
    assert "byId" in gs, "no front-ID fallback in the emitted script"
    assert "setName(want)" in gs, "the fallback must fix the name, not just find it"
    assert "TABS RENAMED" in gs, "the fallback must report itself"
    # the fallback must not silently replace the MISSING TABS report
    assert "MISSING TABS" in gs


def test_the_repair_syncs_what_the_register_owns():
    """The drift this whole path exists to fix.

    The workbook built 2026-09-12 froze every Status, Stage, Gate and
    Standing at build time, because the repair wrote LIVE formulas and
    nothing else. Nineteen fronts had drifted by 2026-09-20 and three had no
    tab at all, and the only fix anyone had was a rebuild — which costs the
    pasted Logger corpus.
    """
    import tempfile
    fronts = b.parse_register(os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "LOGGER_FRONTS.md"))
    with tempfile.NamedTemporaryFile("r+", suffix=".gs") as fh:
        b.emit_apps_script(fronts, fh.name)
        gs = open(fh.name).read()
    for f in fronts:
        for cell, value in b.register_fields(f):
            if isinstance(value, str) and value:
                assert json.dumps(value, ensure_ascii=False) in gs, (
                    f"{f['id']} {cell} is not written by the repair script")
    # and a front the workbook has never seen must be raisable from nothing
    assert "insertSheet(want)" in gs and "scaffoldFront" in gs


def test_the_repair_never_touches_the_operators_rows():
    """A resync that ate a typed PROGRESS LOG line, an Owner or a Next action
    would be a worse bug than the drift it fixes. Those rows belong to the
    operator; the register owns everything else."""
    fronts = b.parse_register(os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "LOGGER_FRONTS.md"))
    owned = {c for f in fronts for c, _v in b.register_fields(f)}
    assert not owned & {"B13", "B14"}, "Owner/Next action are not the register's"
    for f in fronts:
        log_row = b.log_row_for(f)
        written = [int(c[1:]) for c, _v in b.register_fields(f)]
        written += [b.LIVE_FIRST_ROW + k
                    for k in range(len(b.LIVE.get(f["id"], [])))]
        assert max(written) < log_row, (
            f"{f['id']}: the repair writes row {max(written)}, at or below "
            f"the PROGRESS LOG header on row {log_row}")


def test_a8_measures_a_per_crop_rule_with_the_only_instrument_there_is():
    """A8 is the one front where a crop-weighted cell is CORRECT, so it must
    be excluded from the blanket rule above rather than quietly pinned.

    Its shipped rule acts on one crop — downgrade `gemini_auto` when that
    crop's rendered diameter is under ~64px — but MATCH_HEADER carries no
    per-crop radius: `det_radius_min/max/mean/std` are all lot-level. So the
    floor count is crops under a LOT-level proxy, and its caption has to say
    so. The mean beside it is a per-photo fact and must be pinned, or dense
    lots (more crops, smaller buttons) drag it down.
    """
    crop = f'match_log!{b.M["crop_num"]}2:{b.M["crop_num"]}'
    mean_label, mean_f = b.LIVE["A8"][0]
    floor_label, floor_f = b.LIVE["A8"][1]
    assert crop in mean_f, "A8's mean is a per-photo fact and must be pinned"
    assert crop not in floor_f, (
        "A8's floor count is deliberately per-crop — pinning it would report "
        "lots, which is not what the guard acts on")
    assert "proxy" in floor_label.lower() or "MEAN" in floor_label, (
        f"A8's floor caption must name the lot-level proxy: {floor_label!r}")
    # and the day a per-crop radius column exists, this test should fail
    assert not [c for c in b.ml.MATCH_HEADER
                if "radius" in c and "crop" in c], (
        "a per-crop radius column now exists — measure A8's rule directly")


def test_bookkeeping_rows_are_not_counted_as_confirmations():
    """`gemini_count` rows record a Gemini count, not a decision anybody made.

    B23 already excluded them from its tap denominator. E4's gate counted
    them anyway (5358 against 4829 real), A10 reported the same inflated
    total beside it, and A16 counted them into the wrong-#1 pool because a
    row with no `chosen_phrase` cannot match its own top-1. A workbook that
    answers "how many confirmations are there" two different ways on two tabs
    is worse than one that is wrong the same way everywhere.
    """
    for fid in ("A10", "E4", "B23"):
        joined = " ".join(f for _l, f in b.LIVE[fid])
        assert '"gemini_count"' in joined, (
            f"{fid} counts bookkeeping rows as confirmations")
    wrong = dict((l, f) for l, f in b.LIVE["A16"])
    pool = [f for l, f in b.LIVE["A16"] if "swap-pair" in l]
    assert pool and '"<>gemini_count"' in pool[0], (
        "A16's wrong-#1 pool still counts bookkeeping rows")


def test_e4_and_a10_agree_on_what_a_confirmation_is():
    """They read the same population and are shown side by side; if they ever
    diverge, one of the two tabs is lying about the same number."""
    e4 = [f for l, f in b.LIVE["E4"] if "accrued" in l][0]
    a10 = [f for l, f in b.LIVE["A10"] if "Confirms total" in l][0]
    assert e4 == a10, f"E4 and A10 disagree:\n  {e4}\n  {a10}"


def test_e2_splits_its_gate_by_lot_shape_and_the_two_halves_partition():
    """E2's ≥98% gate sat at ~78% pooled across two different failure
    regimes — 43% of dense lots fuse against 5.3% of small ones, and scale
    confidence fails on the small ones instead. Pooled, the harder half can
    hold the gate down forever while the easier half is already shippable.

    The strata must cover the gated population exactly once: 1-6 and 7+,
    both guarded by ISNUMBER so an un-scored row (an empty paste is TEXT,
    which Sheets ranks above every number) lands in neither.
    """
    labels = [l for l, _f in b.LIVE["E2"]]
    assert any("small lots" in l for l in labels), "E2 has no small stratum"
    assert any("dense lots" in l for l in labels), "E2 has no dense stratum"
    g = f'match_log!{b.M["gemini_button_count"]}2:{b.M["gemini_button_count"]}'
    small = [f for l, f in b.LIVE["E2"] if "small lots" in l]
    dense = [f for l, f in b.LIVE["E2"] if "dense lots" in l]
    for f in small:
        assert f'({g}>=1)*({g}<=6)' in f, f"small stratum is not 1-6: {f}"
    for f in dense:
        assert f'({g}>=7)' in f, f"dense stratum is not 7+: {f}"
    for f in small + dense:
        assert f'ISNUMBER({g})' in f, (
            f"stratum counts un-scored rows: {f}")
        # every stratum cell must still be inside the gated population
        assert '"auto"' in f and '"scale_first"' in f, (
            f"stratum is not restricted to gated lots: {f}")


def test_the_repair_can_move_a_log_that_changed_height_but_refuses_to_strand():
    """Growing a LIVE block moves the PROGRESS LOG header down.

    That used to be impossible in place, which is what made LIVE_ROW_BUDGET a
    freeze rather than a declaration: the only way to change a block was a
    rebuild, and a rebuild costs the pasted Logger corpus. The repair now
    relays a tab out — but a typed log row above the new header would be
    silently orphaned, so it must check and refuse instead, and say so.
    """
    import tempfile
    fronts = b.parse_register(os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "LOGGER_FRONTS.md"))
    with tempfile.NamedTemporaryFile("r+", suffix=".gs") as fh:
        b.emit_apps_script(fronts, fh.name)
        gs = open(fh.name).read()
    assert "function relayout(" in gs
    assert "'occupied'" in gs, "the guard has no refusal path"
    assert "LAYOUT STALE" in gs, "a refusal must be reported, not swallowed"
    # and it must not relayout a tab it just created from scratch
    assert "created.indexOf(SCAFFOLD[i][0]) >= 0" in gs
