"""bank_cache -- the CLIP banks as .npz twins of their torch pickles, so a cold
start can read them without unpickling (2026-10-25).

vectors.pt, text_features.pt and text_variants.pt live in the shared bucket
and are torch pickles.  torch.load(weights_only=True) was meant to stop a
planted file from running code, but on the pinned torch (2.2.2) it does not
(CVE-2025-32434, fixed in 2.6; CVE-2026-24747, fixed in 2.10), and torch
cannot move without moving the CLIP encoders.  An .npz read with
allow_pickle=False holds plain arrays only: a planted one is wrong data at
worst, never code.

Phase 1 (now): every save of a bank's .pt also writes ``<name>.npz``, its
twin, stamped with the .pt's GCS generation (``TWIN_KEY``).  A reader takes
the .npz only when it is the twin of the .pt in the bucket now; otherwise it
reads the .pt as before (and buttonmatcher then writes the twin).  So a .pt
written without one -- by an older build or a tool -- is still read, and a
.pt deleted to force a rebuild still forces it.  Phase 2, once every bank
has its twin: read and write the .npz alone, and drop the .pt.

Byte-identical in buttonmatcher and ebayscout (ebayscout reads the banks
buttonmatcher writes).  Pure numpy.
"""

from __future__ import annotations

import io

import numpy as np

TWIN_KEY = "pt_generation"      # GCS metadata on the .npz: its .pt's generation

# Each bank holds one tensor (``features`` or ``vectors``) and lists beside
# it.  A reader turns these keys back into tensors and every other field
# into the list the .pt held.
TENSOR_KEYS = frozenset({"features", "vectors"})


def npz_name(pt_name: str) -> str:
    """``vectors.pt`` -> ``vectors.npz``."""
    if not str(pt_name).endswith(".pt"):
        raise ValueError(f"not a .pt name: {pt_name!r}")
    return str(pt_name)[:-3] + ".npz"


def pack(fields: dict) -> bytes:
    """``fields`` -- numpy arrays, or lists of str / int / float -- as .npz
    bytes.  ValueError when a field could only be stored with pickle, or
    would not read back exactly as given (a list mixing ints and strings
    comes back all strings), so a twin is never a lossy copy."""
    arrays = {}
    for name, value in fields.items():
        arr = value if isinstance(value, np.ndarray) else np.asarray(list(value))
        if arr.dtype.hasobject:
            raise ValueError(f"{name}: needs pickle ({arr.dtype})")
        arrays[name] = arr
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    data = buf.getvalue()
    back = unpack(data)
    for name, value in fields.items():
        if not _same(value, back[name]):
            raise ValueError(f"{name}: does not read back as written")
    return data


def unpack(data: bytes) -> dict:
    """The arrays in .npz bytes, read with allow_pickle=False."""
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        return {name: z[name] for name in z.files}


def as_fields(arrays: dict) -> dict:
    """``unpack``'s arrays as a bank's fields: ``TENSOR_KEYS`` stay arrays (the
    caller makes them tensors), every other field the list the .pt held."""
    return {k: (v if k in TENSOR_KEYS else v.tolist()) for k, v in arrays.items()}


def _same(value, arr) -> bool:
    if isinstance(value, np.ndarray):
        return (value.dtype == arr.dtype and value.shape == arr.shape
                and value.tobytes() == arr.tobytes())
    return arr.tolist() == list(value)


def twin_metadata(pt_generation) -> dict:
    """The GCS metadata that makes an .npz the twin of the .pt at
    ``pt_generation``."""
    return {TWIN_KEY: str(pt_generation)}


def is_twin(npz_metadata, pt_generation) -> bool:
    """Was this .npz written from the .pt at ``pt_generation``?"""
    return (pt_generation is not None and bool(npz_metadata)
            and str(npz_metadata.get(TWIN_KEY, "")) == str(pt_generation))
