"""Unit tests for detect._grid_hole_cells — pure lattice geometry, no cv2.

The white-on-white miss (2026-07-18 Mildcats/Minnesota lot): a button whose
faint rim vanishes at the working resolution is invisible to Hough AND
white-rescue, but the grid still shows its empty INTERIOR cell.  This helper
finds those cells so detect_buttons can force a crop there — geometry, not rim
visibility.  The function is stdlib-only, so it is ast-extracted from ebayscout/detect_pipeline.py
and exec'd in isolation (same pattern as the main.py pure-fn tests).

Run: python tests/run_grid_hole_fill_tests.py
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DETECT_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "detect_pipeline.py")


def _load(*names):
    src = open(DETECT_PY).read()
    tree = ast.parse(src)
    ns = {}
    for want in names:
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == want)
        exec(compile(ast.get_source_segment(src, node), DETECT_PY, "exec"), ns)
    return [ns[w] for w in names]


_grid_hole_cells = _load("_grid_hole_cells")[0]


def _row(y, xs, r=50):
    return [(x, y, r) for x in xs]


def test_minnesota_interior_hole():
    """The exact lot: 4x3 grid, row 3 missing its 2nd column (Minnesota).
    Cols ~ 60/200/340/475; row 3 has 60, 340, 475 → hole at col ~200."""
    rows = [
        _row(170, [60, 200, 340, 475]),
        _row(310, [60, 200, 340, 475]),
        _row(465, [60, 340, 475]),          # Minnesota (col ~200) missing
    ]
    holes = _grid_hole_cells(rows, expected=12, r_est=50.0, max_fill=1)
    assert len(holes) == 1, holes
    hx, hy = holes[0]
    assert abs(hx - 200) < 15 and abs(hy - 465) < 5, holes


def test_no_holes_when_complete():
    rows = [_row(170, [60, 200, 340, 475]), _row(310, [60, 200, 340, 475]),
            _row(465, [60, 200, 340, 475])]
    assert _grid_hole_cells(rows, expected=12, r_est=50.0, max_fill=1) == []


def test_trailing_gap_is_never_filled():
    """A short LAST row missing its rightmost button is a genuinely-absent
    button, not an interior hole — must NOT be invented (col 475 unflanked)."""
    rows = [_row(170, [60, 200, 340, 475]), _row(310, [60, 200, 340, 475]),
            _row(465, [60, 200, 340])]      # col 475 missing at the END
    assert _grid_hole_cells(rows, expected=12, r_est=50.0, max_fill=1) == []


def test_leading_gap_is_never_filled():
    """Symmetric: a missing FIRST column in a row is unflanked on the left."""
    rows = [_row(170, [60, 200, 340, 475]), _row(310, [60, 200, 340, 475]),
            _row(465, [200, 340, 475])]     # col 60 missing at the START
    assert _grid_hole_cells(rows, expected=12, r_est=50.0, max_fill=1) == []


def test_max_fill_caps_output():
    rows = [_row(170, [60, 200, 340, 475]),
            _row(310, [60, 340, 475]),      # hole at 200
            _row(465, [60, 200, 475])]      # hole at 340
    assert len(_grid_hole_cells(rows, 12, 50.0, max_fill=1)) == 1
    assert len(_grid_hole_cells(rows, 12, 50.0, max_fill=2)) == 2


def test_irregular_lattice_returns_empty():
    """Staggered / non-grid arrangement: a circle far from every inferred
    column centre → 'interior hole' is meaningless, fail closed."""
    rows = [_row(170, [60, 200, 340, 475]),
            _row(310, [130, 340, 475]),     # 130 is between cols → off-grid
            _row(465, [60, 340, 475])]
    assert _grid_hole_cells(rows, 12, 50.0, max_fill=2) == []


def test_guards_fail_closed():
    good = [_row(170, [60, 200, 340, 475]), _row(310, [60, 340, 475])]
    assert _grid_hole_cells(good, expected=8, r_est=50.0, max_fill=0) == []   # nothing to add
    assert _grid_hole_cells(good, expected=6, r_est=50.0, max_fill=1) == []   # n>=expected
    assert _grid_hole_cells([_row(170, [60, 340])], 4, 50.0, 1) == []         # <2 rows
    assert _grid_hole_cells(good, expected=8, r_est=0, max_fill=1) == []      # no radius


if __name__ == "__main__":
    for _n in sorted(list(globals())):
        if _n.startswith("test_"):
            globals()[_n]()
    print("all grid-hole-fill tests passed")
