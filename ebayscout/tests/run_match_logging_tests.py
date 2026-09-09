"""Standalone test runner for test_match_logging (works without pytest).

    python tests/run_match_logging_tests.py
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
    "test_match_logging", os.path.join(HERE, "test_match_logging.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

passed = failed = 0
for name in sorted(dir(mod)):
    if not name.startswith("test_"):
        continue
    try:
        getattr(mod, name)()
        passed += 1
        print(f"PASS {name}")
    except Exception:
        failed += 1
        print(f"FAIL {name}")
        traceback.print_exc()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
