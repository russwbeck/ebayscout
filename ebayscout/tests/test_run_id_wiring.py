"""Watcher run id wiring (2026-09-30): process_pipeline_lot reads the watcher's
run id from the .response.json (pipeline_ingest.run_id_of, unit-tested in
test_pipeline_ingest.py) and puts it where a lot can be found from watcher.log:
the deal alert (notifier._trace_text, tested in test_notifier.py) and, for the
many lots that post nothing, the scan_log row.  main.py imports the heavy
stack, so it is read via ast.

Run: python tests/run_run_id_wiring_tests.py
"""

import ast
import os

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(PKG, "main.py")).read()
_TREE = ast.parse(_SRC)


def _src(name):
    f = next(n for n in ast.walk(_TREE)
             if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_SRC, f) or ""


def test_run_id_read_from_the_same_json_the_lot_is_built_from():
    src = _src("process_pipeline_lot")
    assert "ping.parse_gemini_response(_rsp_text)" in src
    assert "run_id = ping.run_id_of(_rsp_text)" in src


def test_alert_listing_carries_file_and_run():
    src = _src("process_pipeline_lot")
    at = src.index("listing = {")
    block = src[at:src.index("}", src.index('"run_id"', at)) + 1]
    assert '"lot_file"' in block and '"run_id": run_id' in block
    # built before either alert can fire
    assert at < src.index("send_needed_alert(") and at < src.index("send_undervalued_alert(")


def test_scan_log_row_carries_the_run_id_before_it_is_written():
    src = _src("process_pipeline_lot")
    assert 'record["run_id"] = run_id' in src
    assert src.index('record["run_id"] = run_id') < src.index("append_scan_log([record])")


# --- the watcher gave up (2026-09-30) -----------------------------------------

def test_gave_up_notice_is_handled_before_any_image_work():
    """A give-up notice has no image: it must be caught before the download,
    or the lot errors out and the listing is fed again tomorrow."""
    src = _src("process_pipeline_lot")
    gave = src.index("ping.watcher_gave_up(_rsp_text)")
    assert gave < src.index("tempfile.NamedTemporaryFile")
    assert gave < src.index("_gcs_blob_to_file(")


def test_gave_up_listing_is_marked_seen_and_forgotten():
    src = _src("process_pipeline_lot")
    seg = src[src.index("ping.watcher_gave_up(_rsp_text)"):]
    seg = seg[:seg.index("return") + len("return")]
    assert "_mark_item_seen_now(" in seg          # never fed again
    assert "delete_pending_context(key)" in seg    # context cleaned up
    assert "_delete_pipeline_output(response_name)" in seg
    # and nothing is posted to #ebay-checker — the watcher already posted it
    assert "notifier." not in seg
