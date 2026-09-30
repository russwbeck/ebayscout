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
