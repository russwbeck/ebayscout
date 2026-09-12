"""Runner: python ebayscout/tests/run_no_unbound_names_tests.py"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from ebayscout.tests import test_no_unbound_names as mod

passed = failed = 0
for name in sorted(n for n in dir(mod) if n.startswith("test_")):
    try:
        getattr(mod, name)()
        print(f"PASS {name}")
        passed += 1
    except Exception:
        print(f"FAIL {name}")
        traceback.print_exc()
        failed += 1
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
