"""bank_cache -- the CLIP banks' .npz twins, read without unpickling.

SHARED, byte-identical in ebayscout/ebayscout/tests/ and buttonmatcher/tests/,
like the module it tests.  It finds the module whichever layout it is in.
numpy only; each service's wiring is tested in its own repo.

Run: python tests/run_bank_cache_tests.py
"""

import io
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(os.path.dirname(HERE), "bank_cache.py")):
    sys.path.insert(0, os.path.dirname(HERE))                    # buttonmatcher
    import bank_cache as bc
else:
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))   # ebayscout
    from ebayscout import bank_cache as bc


def _raises(fn, *args):
    try:
        fn(*args)
    except ValueError:
        return True
    return False


def test_the_reference_bank_reads_back_exactly():
    vecs = np.random.default_rng(7).standard_normal((5, 512)).astype(np.float32)
    bank = {"vectors": vecs, "labels": [1984, 1985, 1985, 2001, 1900],
            "entry_ids": ["412", "_year_1985", "77", "938", "1291"]}
    got = bc.as_fields(bc.unpack(bc.pack(bank)))
    assert got["vectors"].dtype == np.float32
    assert got["vectors"].tobytes() == vecs.tobytes()          # bit for bit
    assert got["labels"] == bank["labels"]
    assert all(type(y) is int for y in got["labels"])
    assert got["entry_ids"] == bank["entry_ids"]
    assert all(type(e) is str for e in got["entry_ids"])


def test_the_text_bank_reads_back_exactly_in_any_float_width():
    for dtype in (np.float16, np.float32):
        feats = np.random.default_rng(1).standard_normal((3, 512)).astype(dtype)
        bank = {"features": feats,
                "phrases": ["We're Out for Blood! (scarf)", "Lions' Tea Party",
                            "Penn State — Beat the Buckeyes"],
                "years": [1900, 1991, 2024],
                "types": ["Football", "Football", "Basketball"]}
        got = bc.as_fields(bc.unpack(bc.pack(bank)))
        assert got["features"].dtype == dtype
        assert got["features"].tobytes() == feats.tobytes()
        for k in ("phrases", "years", "types"):
            assert got[k] == bank[k], k


def test_an_empty_list_is_kept_empty():
    assert bc.as_fields(bc.unpack(bc.pack({"texts": []})))["texts"] == []


def test_nothing_is_packed_that_needs_pickle_or_reads_back_changed():
    assert _raises(bc.pack, {"labels": [1984, "1985"]})        # comes back all str
    assert _raises(bc.pack, {"labels": [{"year": 1984}]})      # an object
    assert _raises(bc.pack, {"phrases": ["ends in a NUL\x00"]})  # numpy drops it


def test_a_planted_object_array_is_never_unpickled():
    buf = io.BytesIO()
    np.savez(buf, labels=np.array([{"x": 1}], dtype=object))
    assert _raises(bc.unpack, buf.getvalue())


def test_a_twin_belongs_to_the_generation_it_was_written_from():
    md = bc.twin_metadata(1729)
    assert bc.is_twin(md, 1729) and bc.is_twin(md, "1729")
    assert not bc.is_twin(md, 1730)          # the .pt was written again since
    assert not bc.is_twin(None, 1729) and not bc.is_twin({}, 1729)
    assert not bc.is_twin(md, None)          # the .pt's generation is unknown


def test_names():
    assert bc.npz_name("vectors.pt") == "vectors.npz"
    assert bc.npz_name("text_features.pt") == "text_features.npz"
    assert bc.npz_name("text_variants.pt") == "text_variants.npz"
    assert _raises(bc.npz_name, "vectors.npz")
