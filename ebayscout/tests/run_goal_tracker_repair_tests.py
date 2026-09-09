"""Harness for test_goal_tracker_repair.py."""
import os, sys, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_goal_tracker_repair as t
passed = failed = 0
for name in sorted(dir(t)):
    if not name.startswith("test_"):
        continue
    try:
        getattr(t, name)(); passed += 1; print(f"PASS {name}")
    except Exception:
        failed += 1; print(f"FAIL {name}"); traceback.print_exc()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
