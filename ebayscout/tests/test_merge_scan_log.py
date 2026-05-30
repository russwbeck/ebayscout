"""
Tests for tools/merge_scan_log.py — idempotent one-time seed into the live log.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout.tools.merge_scan_log import merge


class TestMerge:
    def test_appends_only_new_ids(self):
        base = [{"item_id": "v1|1|0", "asking": 5.0}]
        add  = [{"item_id": "v1|1|0", "asking": 5.0},      # already present -> skip
                {"item_id": "backfill|x", "asking": None}]  # new -> append
        out, added = merge(base, [add])
        assert added == 1
        assert [r["item_id"] for r in out] == ["v1|1|0", "backfill|x"]

    def test_base_kept_intact_incl_repeat_observations(self):
        # Two legit observations of the same item over time must both survive.
        base = [{"item_id": "v1|7|0", "ts": "t1"}, {"item_id": "v1|7|0", "ts": "t2"}]
        out, added = merge(base, [[]])
        assert added == 0 and len(out) == 2

    def test_idempotent(self):
        base = [{"item_id": "v1|1|0"}]
        add  = [{"item_id": "backfill|a"}, {"item_id": "backfill|b"}]
        once, _ = merge(base, [add])
        twice, added2 = merge(once, [add])     # re-run with same add
        assert added2 == 0 and len(twice) == len(once) == 3

    def test_multiple_add_files(self):
        base = []
        out, added = merge(base, [[{"item_id": "a"}], [{"item_id": "b"}, {"item_id": "a"}]])
        assert added == 2 and [r["item_id"] for r in out] == ["a", "b"]
