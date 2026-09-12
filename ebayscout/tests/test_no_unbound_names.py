"""Static guard: no undefined names, no use-before-assignment.

WHY THIS EXISTS
---------------
The label-harvest block in `main.py` carried TWO of these at once, for two
months, and produced nothing:

  1. `pipeline_command` was read at step 4b and assigned ~230 lines later at
     step 10.  Because the name is assigned somewhere in the function, Python
     resolves it as a local, so the read raised UnboundLocalError on every lot.
  2. The next statement called `storage.Client()`, but `storage` is not a
     module-level name in `main.py` — every other user imports it inside the
     function.  NameError.

Neither is a syntax error, so `py_compile` passes and the module imports fine.
The block is deliberately fail-open — it logs and continues so a sidecar
failure never costs a valuation — which meant both bugs only ever surfaced as
a stdout line nobody was reading:

    !!! PIPELINE: label sidecar failed for <job_id>: cannot access local
    variable 'pipeline_command' where it is not associated with a value

Cost: ebayscout wrote ZERO training labels from 2026-07-08 to 2026-09-12,
while being ~75% of pipeline volume.  All 523 sidecars in GCS were
buttonmatcher's, whose copy passes a literal command.  Front C5's gate is
"is every pipeline lot leaving a labeled example"; the honest answer was
"none of ebayscout's", and nothing in the Logger could have said so.

pyflakes catches both in milliseconds.  This test is that check, pinned to the
two classes that bite hardest in fail-open code, so the next one fails here
instead of in production.

Run: python ebayscout/tests/run_no_unbound_names_tests.py
"""

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Undefined-name and unbound-local reports are always real bugs.  Unused
# locals and unused imports are style, and this file does not police them.
FATAL = ("undefined name", "local variable", "referenced before assignment")

# Unused-local reports that are known and deliberate, so the guard stays
# readable rather than being switched off wholesale.
ALLOW = (
    "local variable 'img' is assigned to but never used",
    "local variable 'raw_by_scale' is assigned to but never used",
    "local variable 'count_meta' is assigned to but never used",
)


def _pyflakes(paths):
    try:
        out = subprocess.run([sys.executable, "-m", "pyflakes", *paths],
                             capture_output=True, text=True, timeout=180)
    except FileNotFoundError:                      # pragma: no cover
        return None
    if "No module named" in (out.stderr or ""):
        return None
    return (out.stdout or "") + (out.stderr or "")


def test_no_undefined_names_or_unbound_locals():
    files = sorted(
        os.path.join(REPO, f) for f in os.listdir(REPO) if f.endswith(".py"))
    report = _pyflakes(files)
    if report is None:
        print("    SKIP: pyflakes not installed (pip install pyflakes)")
        return
    bad = []
    for line in report.splitlines():
        low = line.lower()
        if any(k in low for k in FATAL) and not any(a in line for a in ALLOW):
            bad.append(line)
    assert not bad, ("undefined names / unbound locals:\n  "
                     + "\n  ".join(bad))


def test_pipeline_command_is_bound_before_the_label_sidecar_reads_it():
    """The exact 2026-07-08 defect, pinned by line order.

    Kept alongside the pyflakes sweep because it names the failure instead of
    reporting it generically, and it still fails if pyflakes is unavailable.
    """
    src = open(os.path.join(REPO, "main.py")).read().splitlines()
    assigns = [i for i, l in enumerate(src) if l.strip().startswith("pipeline_command =")]
    reads = [i for i, l in enumerate(src)
             if "pipeline_command" in l and not l.strip().startswith("#")
             and not l.strip().startswith("pipeline_command =")]
    assert assigns, "pipeline_command is never assigned"
    assert min(assigns) < min(reads), (
        f"pipeline_command assigned at line {min(assigns) + 1} but first read at "
        f"line {min(reads) + 1} — UnboundLocalError on every lot")


def test_the_label_sidecar_block_imports_storage():
    """`storage` is function-local in this module, never module-level."""
    src = open(os.path.join(REPO, "main.py")).read()
    block = src.split("if lharv.harvest_enabled():")[1].split("# 5) CLIP matching")[0]
    assert "storage.Client()" in block
    assert "from google.cloud import storage" in block, (
        "storage.Client() with no local import — NameError, fail-open, silent")


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_"):
            _fn()
    print("all unbound-name tests passed")
