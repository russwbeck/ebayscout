"""SR-04: the scan log is partitioned by month.

GCS has no append.  As one blob, every processed lot downloaded the entire log
and re-uploaded it with a line added — cost linear in the log's size, per lot,
forever, and serialized behind a lock so concurrent Gem results could not
clobber each other.  Under scan_log/YYYY-MM.jsonl that is bounded at one month.

What must not change while the write path does: which records exist, in what
order, and that a reader can still see the whole history as one stream.

Run: python tests/run_scan_log_tests.py
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from ebayscout import scan_log


def _rec(ts, item_id="i1"):
    return {"ts": ts, "item_id": item_id}


# --- which partition a record belongs to ----------------------------------------

def test_the_month_comes_from_the_records_own_timestamp():
    """Not from the clock at write time — a backfill replayed today must land in
    the months it observed, or a month's file stops meaning that month."""
    assert scan_log.month_of(_rec("2026-09-14T13:02:11+00:00")) == "2026-09"
    assert scan_log.month_of(_rec("2025-01-02T00:00:00Z")) == "2025-01"


def test_a_record_with_no_usable_timestamp_still_goes_somewhere_readable():
    """Dropping it would lose an observation that cost an eBay call and a CLIP
    pass to a formatting problem."""
    for bad in ({}, {"ts": None}, {"ts": ""}, {"ts": "yesterday"},
                {"ts": "20260914"}, {"ts": 20260914}, {"ts": "202x-09-14"}):
        assert scan_log.month_of(bad) == scan_log.UNDATED, bad


def test_partition_keeps_each_months_records_in_arrival_order():
    recs = [_rec("2026-08-31T23:59:00Z", "a"), _rec("2026-09-01T00:00:01Z", "b"),
            _rec("2026-08-01T00:00:00Z", "c"), _rec("2026-09-30T12:00:00Z", "d")]
    parts = scan_log.partition(recs)
    assert sorted(parts) == ["2026-08", "2026-09"]
    assert [r["item_id"] for r in parts["2026-08"]] == ["a", "c"]
    assert [r["item_id"] for r in parts["2026-09"]] == ["b", "d"]


def test_one_lots_write_touches_exactly_one_partition():
    """The ordinary case, and the whole point: a lot pays for one month."""
    assert len(scan_log.partition([_rec("2026-09-14T00:00:00Z")])) == 1


def test_the_blob_name_is_the_month_under_the_prefix():
    assert scan_log.blob_name("2026-09", "ebay_scout/scan_log/") == \
        "ebay_scout/scan_log/2026-09.jsonl"


# --- the append itself ----------------------------------------------------------

def test_records_are_appended_as_json_lines():
    out = scan_log.appended_text("", [{"a": 1}, {"b": 2}])
    assert [json.loads(l) for l in out.splitlines()] == [{"a": 1}, {"b": 2}]
    assert out.endswith("\n")


def test_a_partition_missing_its_final_newline_is_not_welded_to():
    """A truncated upload or a hand edit must not fuse two records into one
    unparseable line — that loses BOTH."""
    out = scan_log.appended_text('{"a": 1}', [{"b": 2}])
    assert [json.loads(l) for l in out.splitlines()] == [{"a": 1}, {"b": 2}]


def test_appending_nothing_leaves_the_partition_byte_identical():
    assert scan_log.appended_text('{"a": 1}\n', []) == '{"a": 1}\n'


def test_an_empty_partition_starts_clean():
    assert scan_log.appended_text("", [{"a": 1}]) == '{"a": 1}\n'


# --- reading it back ------------------------------------------------------------

def _tree(files):
    d = tempfile.mkdtemp()
    for name, records in files.items():
        with open(os.path.join(d, name), "w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
    return d


def test_a_directory_reads_as_one_stream_in_month_order():
    """YYYY-MM names sort chronologically, so the reader sees the log in the
    order it was written without knowing anything about partitioning."""
    d = _tree({"2026-09.jsonl": [_rec("2026-09-01T00:00:00Z", "c")],
               "2026-08.jsonl": [_rec("2026-08-01T00:00:00Z", "b")],
               "2025-12.jsonl": [_rec("2025-12-01T00:00:00Z", "a")]})
    assert [r["item_id"] for r in scan_log.load(d)] == ["a", "b", "c"]


def test_files_and_directories_mix_so_the_legacy_log_still_reads():
    """The single pre-partition scan_log.jsonl is the operator's to keep; it is
    just one more file to a reader."""
    d = _tree({"2026-09.jsonl": [_rec("2026-09-01T00:00:00Z", "new")]})
    legacy = os.path.join(tempfile.mkdtemp(), "scan_log.jsonl")
    with open(legacy, "w") as fh:
        fh.write(json.dumps(_rec("2026-05-01T00:00:00Z", "old")) + "\n")
    assert [r["item_id"] for r in scan_log.load([legacy, d])] == ["old", "new"]


def test_non_jsonl_files_in_the_directory_are_ignored():
    d = _tree({"2026-09.jsonl": [_rec("2026-09-01T00:00:00Z", "keep")]})
    open(os.path.join(d, "README.txt"), "w").write("not a log\n")
    open(os.path.join(d, "2026-09.jsonl.bak"), "w").write("{}\n")
    assert [r["item_id"] for r in scan_log.load(d)] == ["keep"]


def test_a_malformed_line_does_not_hide_the_rest_of_the_log():
    """These files are months of appends; one bad line must cost one record."""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "2026-09.jsonl"), "w") as fh:
        fh.write(json.dumps(_rec("2026-09-01T00:00:00Z", "a")) + "\n")
        fh.write("{ this is not json\n")
        fh.write("\n")
        fh.write(json.dumps(_rec("2026-09-02T00:00:00Z", "b")) + "\n")
    assert [r["item_id"] for r in scan_log.load(d)] == ["a", "b"]


def test_a_single_path_string_is_accepted_as_well_as_a_list():
    d = _tree({"2026-09.jsonl": [_rec("2026-09-01T00:00:00Z", "a")]})
    assert scan_log.load(d) == scan_log.load([d])


def test_an_empty_directory_reads_as_an_empty_log():
    assert scan_log.load(tempfile.mkdtemp()) == []


# --- the writer is wired to it --------------------------------------------------

def test_append_scan_log_writes_partitions_not_one_blob():
    """seen_items imports google.cloud, so its source is read rather than run."""
    src = open(os.path.join(os.path.dirname(HERE), "seen_items.py")).read()
    body = src[src.index("def append_scan_log("):]
    body = body[:body.index("\ndef ", 1)]
    assert "SCAN_LOG_PREFIX" in body, (
        "append_scan_log no longer writes under the monthly prefix")
    assert "SCAN_LOG_BLOB" not in body, (
        "append_scan_log still writes the single unbounded blob")
    assert "partition(" in body and "appended_text(" in body


def test_nothing_writes_the_legacy_blob_any_more():
    pkg = os.path.dirname(HERE)
    writers = []
    for name in ("seen_items.py", "main.py"):
        src = open(os.path.join(pkg, name)).read()
        if "SCAN_LOG_BLOB" in src:
            writers.append(name)
    assert writers == [], (
        f"{writers} still reference the pre-partition blob; it is read-only history")
