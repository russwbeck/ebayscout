"""Staging copies never overwrite (2026-09-27).

promote_crops_to_reference_staging copied each pipeline crop into
reference/_staging/ with a bare copy_blob: a name collision replaced a staged
crop silently.  It now goes through _copy_to_new_name, which passes
if_generation_match=0 so GCS refuses (412) and the next name is tried.  The
same fix covers buttonmatcher's library and retire copies.

seen_items imports google.cloud at module level (absent in CI), so the source
is read via ast and the helper is exec'd against a fake bucket that enforces
the precondition like GCS.

Run: python tests/run_no_clobber_staging_tests.py
"""

import ast
import os

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(PKG, "seen_items.py")).read()
_TREE = ast.parse(_SRC)


class PreconditionFailed(Exception):
    pass


class _Blob:
    def __init__(self, name):
        self.name = name


class _Bucket:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.copies = []

    def copy_blob(self, src, bucket, name, if_generation_match=None):
        self.copies.append((name, if_generation_match))
        if if_generation_match == 0 and name in self.objects:
            raise PreconditionFailed(name)
        self.objects[name] = self.objects[src.name]


def _helper():
    fn = next(n for n in ast.walk(_TREE)
              if isinstance(n, ast.FunctionDef) and n.name == "_copy_to_new_name")
    g = {"PreconditionFailed": PreconditionFailed}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<seen_items>", "exec"), g)
    return g["_copy_to_new_name"]


def test_a_taken_name_is_skipped_never_overwritten():
    copy = _helper()
    b = _Bucket({"tmp/c1.jpg": b"NEW", "stg/100.jpg": b"OLD"})
    used = copy(b, _Blob("tmp/c1.jpg"), lambda k: f"stg/{100 + k}.jpg")
    assert used == "stg/101.jpg"
    assert b.objects["stg/100.jpg"] == b"OLD"
    assert all(igm == 0 for _, igm in b.copies)


def test_no_free_name_raises_instead_of_overwriting():
    copy = _helper()
    b = _Bucket({"tmp/c1.jpg": b"NEW", "stg/a.jpg": b"OLD"})
    try:
        copy(b, _Blob("tmp/c1.jpg"), lambda k: "stg/a.jpg", max_tries=3)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    assert b.objects["stg/a.jpg"] == b"OLD"


def test_every_copy_in_seen_items_refuses_to_overwrite():
    calls = [n for n in ast.walk(_TREE)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "copy_blob"]
    assert calls, "no copy_blob found; the test is looking at the wrong file"
    for node in calls:
        kw = {k.arg: k.value for k in node.keywords}
        seg = ast.get_source_segment(_SRC, node)
        assert "if_generation_match" in kw, f"copy may overwrite: {seg}"
        assert kw["if_generation_match"].value == 0, seg


def test_promotion_uses_the_helper():
    fn = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)
              and n.name == "promote_crops_to_reference_staging")
    body = ast.get_source_segment(_SRC, fn)
    assert "_copy_to_new_name(" in body and "bucket.copy_blob(" not in body
