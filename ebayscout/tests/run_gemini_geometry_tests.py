"""Standalone runner for test_gemini_geometry (works without pytest).

    python tests/run_gemini_geometry_tests.py
Exits non-zero if any test fails.
"""

import os
import sys
import traceback
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location(
    "test_gemini_geometry", os.path.join(HERE, "test_gemini_geometry.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

passed = failed = 0
for tname in sorted(dir(mod)):
    if not tname.startswith("test_"):
        continue
    try:
        getattr(mod, tname)()
        passed += 1
        print(f"PASS {tname}")
    except Exception:
        failed += 1
        print(f"FAIL {tname}")
        traceback.print_exc()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
