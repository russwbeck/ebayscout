"""SR-02: one writer for seen_items.json.

The pipeline marks each lot seen the instant its Gem result confirms
(_mark_item_seen_now: load -> add -> save under _seen_lock).  The legacy CLIP
scan used to load the whole dict once at the top of the run and upload it back
wholesale at the end and every 50 listings.  Those two writers are not
compatible: a pipeline confirmation that lands between the scan's load and its
save is erased by the scan's snapshot, the lot is re-fed on the next run, and
the operator gets a second alert for a listing they already saw.  That is the
exact failure seen_items.json exists to prevent.

main.py imports flask/torch/google-cloud at module import time, so the two
functions under test are lifted out by ast and executed against fakes — the
same technique tests/test_buttonmatcher_parity.py uses to run buttonmatcher's
scoring here.  What runs IS the shipped source, not a restatement of it.

Run: python tests/run_seen_writer_tests.py
"""

import ast
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

MAIN_PY = os.path.join(PKG, "main.py")
SEEN_PY = os.path.join(PKG, "seen_items.py")

_MAIN_SRC = open(MAIN_PY).read()
_MAIN_TREE = ast.parse(_MAIN_SRC)
_SEEN_SRC = open(SEEN_PY).read()
_SEEN_TREE = ast.parse(_SEEN_SRC)


def _source_of(tree, src, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(src, n)
    raise AssertionError(f"no function {name}() found")


class FakeStore:
    """A GCS blob that behaves like one: every load is a fresh copy, every save
    replaces the object.  Aliasing the dict instead would hide the whole bug."""

    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.loads = 0
        self.saves = 0
        self.fail_save = False

    # --- the seen_items surface main.py uses --------------------------------
    def load_seen(self):
        self.loads += 1
        return {k: (list(v) if isinstance(v, list) else v)
                for k, v in self.data.items()}

    def save_seen(self, seen):
        self.saves += 1
        if self.fail_save:
            return False
        self.data = {k: (list(v) if isinstance(v, list) else v)
                     for k, v in seen.items()}
        return True

    mark_seen = staticmethod(lambda *a, **k: None)   # replaced below


# The real mark_seen, lifted from seen_items.py — the date-accumulating
# behaviour is part of what a merge has to preserve, so a stand-in would test
# the wrong thing.
_ns = {"date": __import__("datetime").date}
exec(_source_of(_SEEN_TREE, _SEEN_SRC, "mark_seen"), _ns)
FakeStore.mark_seen = staticmethod(_ns["mark_seen"])


def _flusher(store):
    """main.py's _flush_seen_marks, bound to a fake store and a real lock."""
    ns = {"seen_items": store, "_seen_lock": threading.Lock(), "print": print}
    exec(_source_of(_MAIN_TREE, _MAIN_SRC, "_flush_seen_marks"), ns)
    return ns["_flush_seen_marks"]


# --- the race the fix exists to close -------------------------------------------

def test_a_mark_made_during_the_scan_survives_the_scans_checkpoint():
    """The pipeline confirms a lot while the scan is running.  Under the old
    wholesale save that mark was overwritten by a snapshot older than itself."""
    store = FakeStore({"old_lot": "2026-09-01"})
    flush = _flusher(store)

    # the scan has been marking locally...
    marks = ["scan_a", "scan_b"]
    # ...and a pipeline result lands before the checkpoint fires
    pipeline_view = store.load_seen()
    FakeStore.mark_seen("pipeline_lot", pipeline_view)
    store.save_seen(pipeline_view)

    flushed, ok = flush(marks, 0)

    assert ok is True and flushed == 2
    assert "pipeline_lot" in store.data, (
        "the scan's checkpoint erased a mark made while it was running")
    assert store.data["scan_a"] and store.data["scan_b"]
    assert store.data["old_lot"] == "2026-09-01"


def test_the_checkpoint_reloads_rather_than_uploading_its_own_view():
    store = FakeStore()
    flush = _flusher(store)
    flush(["a"], 0)
    assert store.loads == 1, "the flush must read the live store, not trust a cache"
    assert store.saves == 1


# --- the cursor ------------------------------------------------------------------

def test_only_marks_since_the_last_checkpoint_are_applied():
    """Re-applying an already-flushed id would promote its value to a list of
    dates and inflate seen_count — which is_crawl_unseen reads."""
    store = FakeStore()
    flush = _flusher(store)
    flushed, _ = flush(["a", "b"], 0)
    marks = ["a", "b", "c"]
    flushed, ok = flush(marks, flushed)
    assert ok and flushed == 3
    assert store.data["a"] == store.data["b"], "a re-applied mark accumulated a date"
    assert isinstance(store.data["a"], str)


def test_nothing_to_flush_touches_nothing():
    store = FakeStore()
    flush = _flusher(store)
    flushed, ok = flush([], 0)
    assert (flushed, ok) == (0, True)
    assert store.loads == 0 and store.saves == 0

    flushed, ok = flush(["a"], 1)
    assert (flushed, ok) == (1, True)
    assert store.saves == 0


def test_a_failed_save_does_not_advance_the_cursor():
    """The marks ride along with the next checkpoint instead of being lost."""
    store = FakeStore()
    store.fail_save = True
    flush = _flusher(store)
    flushed, ok = flush(["a", "b"], 0)
    assert (flushed, ok) == (0, False)

    store.fail_save = False
    flushed, ok = flush(["a", "b"], flushed)
    assert (flushed, ok) == (2, True)
    assert set(store.data) == {"a", "b"}


def test_a_raising_store_is_not_fatal_to_the_scan():
    """A seen-store outage must not end a scan that is otherwise working."""
    class Boom(FakeStore):
        def load_seen(self):
            raise RuntimeError("GCS is down")

    flush = _flusher(Boom())
    flushed, ok = flush(["a"], 0)
    assert (flushed, ok) == (0, False)


# --- the scan is wired to it -----------------------------------------------------

def _scan_src():
    return _source_of(_MAIN_TREE, _MAIN_SRC, "_run_daily_scan")


def test_the_legacy_scan_no_longer_uploads_its_own_snapshot():
    src = _scan_src()
    assert "seen_store.save_seen(seen)" not in src, (
        "_run_daily_scan still writes the whole dict it loaded at the top — "
        "that is the clobber SR-02 is about")


def test_the_legacy_scan_records_what_it_marked_and_flushes_that():
    src = _scan_src()
    assert "_marked_here.append(item_id)" in src, (
        "the scan does not record which ids IT marked, so a merge cannot know "
        "which marks are its own")
    assert src.count("_flush_seen_marks(") >= 2, (
        "both the every-50 checkpoint and the end-of-run save must go through "
        "the merging writer")


def test_the_pipeline_writer_is_unchanged_and_still_locked():
    src = _source_of(_MAIN_TREE, _MAIN_SRC, "_mark_item_seen_now")
    assert "_seen_lock" in src and "load_seen()" in src and "save_seen(" in src


def test_both_writers_hold_the_same_lock():
    """Load and save are one operation only while nobody else can interleave."""
    for fn in ("_mark_item_seen_now", "_flush_seen_marks"):
        src = _source_of(_MAIN_TREE, _MAIN_SRC, fn)
        assert "with _seen_lock:" in src, fn


def test_a_dry_run_still_writes_nothing():
    src = _scan_src()
    assert "[DRY RUN] Skipping save_seen()." in src, (
        "the dry-run branch that skips persistence was lost")
