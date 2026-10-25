"""clip_matcher reads buttonmatcher's CLIP banks from their .npz twins
(bank_cache.py, 2026-10-25).

vectors.pt and text_features.pt are torch pickles, and
torch.load(weights_only=True) does not stop a planted one running code on the
pinned torch.  buttonmatcher writes an .npz twin beside each, stamped with
the .pt's generation; ``_read_bank`` takes the twin only when it matches the
.pt in the bucket, and otherwise reads the .pt as before.

clip_matcher imports torch and CLIP, so ``_read_bank`` is extracted with
``ast`` and run on a fake bucket and a fake torch.
Run: python -m pytest tests/test_bank_twins.py
"""

import ast
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

from ebayscout import bank_cache  # noqa: E402

_SRC = open(os.path.join(PKG, "clip_matcher.py"), encoding="utf-8").read()


class _Torch:
    def __init__(self, saved):
        self.saved, self.loads = saved, []

    def load(self, path, weights_only=None, map_location=None):
        assert weights_only is True
        self.loads.append(path)
        return self.saved

    @staticmethod
    def from_numpy(a):
        return a


class _Blob:
    def __init__(self, name, data=b"", generation=None, metadata=None):
        self.name, self.data = name, data
        self.generation, self.metadata = generation, metadata

    def download_as_bytes(self):
        return self.data

    def download_to_filename(self, _path):
        pass


class _Bucket:
    def __init__(self, *blobs):
        self.blobs = {b.name: b for b in blobs}

    def get_blob(self, name):
        return self.blobs.get(name)

    def blob(self, name):
        return self.blobs.setdefault(name, _Blob(name))


def _read_bank(torch):
    node = next(n for n in ast.walk(ast.parse(_SRC))
                if isinstance(n, ast.FunctionDef) and n.name == "_read_bank")
    ns = {"bank_cache": bank_cache, "torch": torch, "os": os,
          "print": lambda *a, **k: None}
    exec(compile(ast.get_source_segment(_SRC, node), "clip_matcher.py", "exec"), ns)
    return ns["_read_bank"]


def _vectors():
    return {"vectors": np.ones((2, 4), dtype=np.float32),
            "labels": [1984, 1985], "entry_ids": ["412", "77"]}


def test_a_matching_twin_is_read_and_the_pickle_left_alone():
    torch = _Torch(None)
    pt = _Blob("vectors.pt", generation=11)
    twin = _Blob("vectors.npz", bank_cache.pack(_vectors()),
                 metadata=bank_cache.twin_metadata(11))
    got = _read_bank(torch)(_Bucket(pt, twin), "vectors.pt", "/tmp")
    assert got["vectors"].tobytes() == _vectors()["vectors"].tobytes()
    assert got["labels"] == [1984, 1985] and got["entry_ids"] == ["412", "77"]
    assert torch.loads == []


def test_without_a_current_twin_the_pt_is_read_as_before():
    for twin in (None,
                 _Blob("vectors.npz", bank_cache.pack(_vectors()),
                       metadata=bank_cache.twin_metadata(10)),       # stale
                 _Blob("vectors.npz", b"junk", metadata=bank_cache.twin_metadata(11))):
        torch = _Torch({"vectors": "from the pickle"})
        blobs = [_Blob("vectors.pt", generation=11)] + ([twin] if twin else [])
        got = _read_bank(torch)(_Bucket(*blobs), "vectors.pt", "/tmp")
        assert got == {"vectors": "from the pickle"} and len(torch.loads) == 1


def test_init_reads_both_banks_through_it():
    init = _SRC[_SRC.index("\ndef init("):]
    init = init[:init.index("\ndef ", 1)]
    assert '_read_bank(bucket, "text_features.pt", tmpdir)' in init
    assert '_read_bank(bucket, "vectors.pt", tmpdir)' in init
    assert "torch.load(" not in init
