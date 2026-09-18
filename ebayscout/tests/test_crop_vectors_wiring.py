"""§10.2 step 1 + RS-04 in ebayscout: the vectors are kept, and the staged crop
says where it came from.

ebayscout is the larger source of staged crops — 92% of the review queue's
volume — and until now it named every one of them ``<ts>.jpg``.  No lot id, so
buttonmatcher's "+N more from this lot" collapse was blind on almost the whole
queue and six crops of one photo read as six independent candidates; no crop
number, so nothing could join a staged crop to the embedding the matcher had
just computed for it.  Both fields are the same fix, and RS-04 is a prerequisite
of the value function rather than the nicety the ticket called it.

``seen_items`` imports google-cloud at module scope, so the functions under test
are lifted out by ast and executed against fakes — the same technique
test_seen_writer.py uses.  What runs IS the shipped source.

Run: python tests/run_crop_vectors_wiring_tests.py
"""

import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

from ebayscout import crop_vectors as cvec

MAIN_PY = os.path.join(PKG, "main.py")
SEEN_PY = os.path.join(PKG, "seen_items.py")
CLIP_PY = os.path.join(PKG, "clip_matcher.py")

_MAIN_SRC = open(MAIN_PY).read()
_MAIN_TREE = ast.parse(_MAIN_SRC)
_SEEN_SRC = open(SEEN_PY).read()
_SEEN_TREE = ast.parse(_SEEN_SRC)
_CLIP_SRC = open(CLIP_PY).read()
_CLIP_TREE = ast.parse(_CLIP_SRC)


def _node(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"no function {name}() found")


def _source_of(tree, src, name):
    return ast.get_source_segment(src, _node(tree, name))


# --- the sink, in clip_matcher ---------------------------------------------------

def test_the_matcher_can_hand_back_the_vectors_it_computes():
    f = _node(_CLIP_TREE, "match_crops_with_diagnostics")
    names = [a.arg for a in f.args.args] + [a.arg for a in f.args.kwonlyargs]
    assert "vec_sink" in names


def test_the_sink_is_the_encode_not_a_second_pass():
    """Re-encoding to get the vectors would double the CPU of every crawl on a
    service whose budget is scale-to-zero."""
    src = _source_of(_CLIP_TREE, _CLIP_SRC, "match_crops_with_diagnostics")
    assert "vec_sink.extend" in src
    assert src.count("_model.encode_image(") == 1
    assert src.index("vecs = vecs / vecs.norm") < src.index("vec_sink.extend"), (
        "the sink takes the un-normalised vectors")


def test_the_sink_fill_is_fail_open():
    src = _source_of(_CLIP_TREE, _CLIP_SRC, "match_crops_with_diagnostics")
    tail = src[src.index("if vec_sink is not None"):][:400]
    assert "try:" in tail and "except Exception" in tail


# --- the pipeline lane writes the sidecar ---------------------------------------

def test_the_pipeline_collects_and_persists_its_vectors():
    src = _MAIN_SRC
    assert "vec_sink=_vec_sink" in src, "the pipeline does not collect its vectors"
    assert "seen_items.write_crop_vectors(" in src, "and does not persist them"


def test_the_sidecar_is_keyed_on_the_job_id():
    """job_id is what confirm_log logs per crop and what pipeline/labels/ is named
    for; any other key turns the join into a mapping table to keep in step."""
    at = _MAIN_SRC.index("seen_items.write_crop_vectors(")
    assert "job_id," in _MAIN_SRC[at:at + 120]


def test_the_writer_refuses_a_payload_it_cannot_key():
    """A sidecar whose numbering does not line up with its vectors attributes one
    button's embedding to another, and nothing raises."""
    src = _source_of(_SEEN_TREE, _SEEN_SRC, "write_crop_vectors")
    assert "nums.shape[0] != arr.shape[0]" in src and "return None" in src
    assert "cvec.vectors_enabled()" in src
    assert "dtype=np.float32" in src


def test_the_writer_imports_numpy_lazily():
    """Every heavy import in this service is function-local so a web session can
    still import the module — and so a missing .so cannot kill startup."""
    assert "import numpy as np" not in _SEEN_SRC.split("def ")[0]
    assert "import numpy as np" in _source_of(_SEEN_TREE, _SEEN_SRC,
                                             "write_crop_vectors")


# --- RS-04: the crop number reaches the manifest --------------------------------

def test_the_manifest_carries_the_crop_number():
    """b["n"] is crop_idx + 1 — the same number the match and confirm rows log."""
    at = _MAIN_SRC.index('"gcs_name": gcs_name')
    block = _MAIN_SRC[at:at + 500]
    assert '"crop_num": b["n"]' in block


def _crop_num():
    ns = {}
    exec(_source_of(_SEEN_TREE, _SEEN_SRC, "_staged_crop_num"), ns)
    return ns["_staged_crop_num"]


def test_the_crop_number_comes_from_the_manifest_when_it_is_there():
    assert _crop_num()({"crop_num": 7, "gcs_name": "pipeline_crops/j/7.jpg"}) == 7


def test_the_crop_number_survives_a_manifest_that_lacks_the_field():
    """stage_pipeline_crop writes the temp object as <job>/<n>.jpg, so the number
    is recoverable — which matters for a manifest in flight across the deploy."""
    assert _crop_num()({"gcs_name": "pipeline_crops/job-1/12.jpg"}) == 12


def test_an_unrecoverable_crop_number_is_absent_rather_than_guessed():
    for crop in ({}, {"gcs_name": ""}, {"gcs_name": "pipeline_crops/j/x.jpg"},
                 {"crop_num": None, "gcs_name": None}):
        assert _crop_num()(crop) is None


# --- RS-04: the staged name, from the shipped promote() -------------------------

class _FakeBlob:
    def __init__(self, name, exists=True):
        self.name = name
        self._exists = exists

    def exists(self):
        return self._exists

    def upload_from_string(self, *a, **k):
        pass


class _FakeBucket:
    def __init__(self):
        self.copies = []

    def blob(self, name):
        return _FakeBlob(name)

    def copy_blob(self, src, bucket, dest):
        self.copies.append(dest)


def _promote(crops, job_id="job-9"):
    """The shipped promote_crops_to_reference_staging, against fakes."""
    bucket = _FakeBucket()

    class _Client:
        @staticmethod
        def bucket(_name):
            return bucket

    ns = {
        "config": type("C", (), {"BUCKET_NAME": "b",
                                 "REFERENCE_STAGING_PREFIX": "reference/_staging/"}),
        "storage": type("S", (), {"Client": _Client}),
        "cvec": cvec,
        "time": __import__("time"),
        "json": json,
        "print": lambda *a, **k: None,
        "load_staging_policy": lambda _b: (set(), True),
        "delete_pipeline_crops": lambda *a, **k: None,
        "pipeline_classify": type("P", (), {
            "filter_stopped_crops": staticmethod(lambda cs, st: (cs, []))}),
        "_staged_crop_num": _crop_num(),
    }
    exec(_source_of(_SEEN_TREE, _SEEN_SRC,
                    "promote_crops_to_reference_staging"), ns)
    n = ns["promote_crops_to_reference_staging"](
        job_id, {"job_id": job_id, "crops": crops})
    return n, bucket.copies


def test_a_staged_crop_lands_with_its_lot_and_its_crop_number():
    n, dests = _promote([
        {"gcs_name": "pipeline_crops/job-9/3.jpg", "entry_id": "421", "crop_num": 3},
    ])
    assert n == 1 and len(dests) == 1
    dest = dests[0]
    assert dest.startswith("reference/_staging/421/")
    assert cvec.source_lot(dest) == "job-9", dest
    assert cvec.source_crop(dest) == 3, dest


def test_two_crops_of_one_lot_now_read_as_one_lot():
    """THE queue defect RS-04 fixes: unkeyed names made every crop its own lot,
    so the collapse had nothing to collapse and the reviewer saw all of them."""
    _n, dests = _promote([
        {"gcs_name": "pipeline_crops/job-9/1.jpg", "entry_id": "421", "crop_num": 1},
        {"gcs_name": "pipeline_crops/job-9/2.jpg", "entry_id": "421", "crop_num": 2},
    ])
    assert len({cvec.source_lot(d) for d in dests}) == 1
    assert {cvec.source_crop(d) for d in dests} == {1, 2}


def test_the_timestamps_still_differ_so_no_crop_overwrites_another():
    """The ms timestamp plus the staged counter is what keeps two crops of one
    lot, staged in the same millisecond, from being one object."""
    _n, dests = _promote([
        {"gcs_name": "pipeline_crops/job-9/1.jpg", "entry_id": "421", "crop_num": 1},
        {"gcs_name": "pipeline_crops/job-9/2.jpg", "entry_id": "421", "crop_num": 2},
    ])
    assert len(set(dests)) == 2


def test_a_crop_with_no_recoverable_number_still_stages():
    """Losing the join must cost the measurement, never the crop: this is a
    curated library and an image not staged is an image gone."""
    n, dests = _promote([{"gcs_name": "", "entry_id": "421"}])
    assert n == 0 and dests == []   # no source blob name at all — nothing to copy
    n2, dests2 = _promote([{"gcs_name": "pipeline_crops/job-9/x.jpg",
                            "entry_id": "421"}])
    assert n2 == 1 and cvec.source_crop(dests2[0]) is None
    assert cvec.source_lot(dests2[0]) == "job-9"
