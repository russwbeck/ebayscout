"""
pipeline_ingest — parse the Gemini → GCS pipeline's Pub/Sub notifications and
the structured JSON it writes alongside each button photo.

A separate Gemini pipeline (PIPELINE.md) drops two objects per photo into the
shared GCS bucket under ``pipeline/output/``:

    pipeline/output/<f>.png                  — the original image
    pipeline/output/<f>.png.response.json    — metadata + Gemini's analysis

A GCS object-finalize notification on the ``.response.json`` (written last)
arrives as a Pub/Sub push.  This module turns that push into a small, typed
job description and parses the Gemini analysis into a normalized shape.

Everything here is PURE python (no flask / torch / cv2 / cloud imports) so it is
unit-testable in any environment.  All parsing is FAIL-OPEN: malformed input
yields ``None`` (envelope) or the empty-analysis shape (response), never a raise.

The Gemini analysis ``detected_slogans`` entries carry, per the (user-updated)
Gem prompt:

    index       1-based reading order (left→right, top→bottom)
    slogan      central slogan text (border manufacturer text excluded)
    x, y        button CENTER as a percent of width / height, top-left origin
    radius      button radius as a percent of the image (0–100)           (optional)
    confidence  categorical: "low" / "medium" / "high"                    (optional)

The Gem emits ``radius`` (we keep it under the internal key ``size`` so the pure
geometry module is unchanged) and a categorical ``confidence`` mapped to 0–1 by
``_parse_confidence``.  Both are optional: absent (old Gem output) → ``None``, and
downstream falls back to the median detected radius / skips the confidence gate.

``flagged_problem_slogans`` entries are ``{index, reason, partial_text}`` — buttons
whose text is cut off / smudged / unmatchable.  They carry no slogan or x/y, so they
suppress auto-resolve by ``index`` (see main.process_pipeline_grid).
"""

from __future__ import annotations

import base64
import json


PIPELINE_PREFIX = "pipeline/output/"
RESPONSE_SUFFIX = ".response.json"


EMPTY_ANALYSIS = {
    "total_button_count": 0,
    "blue_background_count": 0,
    "white_background_count": 0,
    "detected_slogans": [],
    "flagged_problem_slogans": [],
}


# --- Pub/Sub push envelope ---------------------------------------------------

def parse_pubsub_envelope(body):
    """Extract ``{bucket, name, event_type}`` from a Pub/Sub push request body.

    ``body`` may be the already-parsed dict or a raw JSON string.  A GCS
    notification carries the object id in the message ``attributes``
    (``bucketId`` / ``objectId`` / ``eventType``) and *also* base64-encodes the
    object metadata JSON (``{bucket, name, ...}``) in ``message.data``.  We
    prefer attributes and fall back to decoding ``data``.

    Returns ``None`` (fail-closed) when the body is malformed or no object name
    can be determined.
    """
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except Exception:
            return None
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return None
    if not isinstance(body, dict):
        return None

    message = body.get("message")
    if not isinstance(message, dict):
        return None

    attrs = message.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}

    bucket = attrs.get("bucketId")
    name = attrs.get("objectId")
    event_type = attrs.get("eventType")

    if not name:
        # Fall back to the base64 object-metadata payload.
        data_b64 = message.get("data")
        if data_b64:
            try:
                decoded = base64.b64decode(data_b64).decode("utf-8")
                meta = json.loads(decoded)
                if isinstance(meta, dict):
                    name = name or meta.get("name")
                    bucket = bucket or meta.get("bucket")
            except Exception:
                pass

    if not name:
        return None

    return {"bucket": bucket, "name": name, "event_type": event_type}


# --- Object-name routing -----------------------------------------------------

def is_response_json(name):
    """True iff ``name`` is a Gemini ``.response.json`` under the pipeline prefix.

    Only the response JSON triggers a build — it is written *after* the image and
    names it — so a bare ``.png`` finalize is ignored.
    """
    if not name or not isinstance(name, str):
        return False
    return name.startswith(PIPELINE_PREFIX) and name.endswith(RESPONSE_SUFFIX)


def image_name_for_response(name):
    """Map ``pipeline/output/<f>.png.response.json`` → ``pipeline/output/<f>.png``.

    Returns ``None`` if ``name`` is not a response-json object.
    """
    if not is_response_json(name):
        return None
    return name[: -len(RESPONSE_SUFFIX)]


# --- Gemini analysis parsing -------------------------------------------------

def _as_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v, default=0):
    try:
        if v is None or v == "":
            return default
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


# Gemini emits confidence as a categorical label (low/medium/high), not a number.
# Translate to our 0–1 scale so the resolver's gate (conf_min=0.70) keeps working:
# only "high" (0.90) clears the gate and auto-resolves; "medium"/"low" still ask.
_CONFIDENCE_LABELS = {"low": 0.30, "medium": 0.60, "high": 0.90}


def _parse_confidence(v):
    """low/medium/high → 0.30/0.60/0.90 (case-insensitive); numeric 0–1 passes
    through (back-compat); anything else → None (gate skipped, fail-open)."""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        key = v.strip().lower()
        if key in _CONFIDENCE_LABELS:
            return _CONFIDENCE_LABELS[key]
        return _as_float(v)  # numeric string ("0.7"); non-numeric label → None
    return _as_float(v)


_SIZE_CLASSES = {"small", "medium", "large"}


def _parse_size_class(v):
    """small/medium/large (case-insensitive) → that label; anything else (numeric,
    blank, unknown) → None.  Gemini gives a RELATIVE per-button size judgment; the
    detector turns it into pixels by scaling the lot's spacing-derived radius, so
    we never trust Gemini for an absolute size.  Reads the new ``size_class`` field
    but falls back to a categorical ``size`` value for prompt-transition tolerance.
    """
    if isinstance(v, str):
        k = v.strip().lower()
        if k in _SIZE_CLASSES:
            return k
    return None


def _edge_coord(s, axis):
    """Read a rim-point coordinate (percent) for ``axis`` ("x"/"y") from either a
    nested ``edge: {x, y}`` object or flat ``edge_x``/``edge_y`` keys.  The rim
    point lies on the button's edge radially out from its center; the detector
    turns center+edge into a per-button radius (distance), so Gemini only ever
    supplies positions — never an absolute size.  Missing → None."""
    e = s.get("edge")
    if isinstance(e, dict) and e.get(axis) is not None:
        return _as_float(e.get(axis))
    return _as_float(s.get(f"edge_{axis}"))


def _loads_loose(s):
    """json.loads tolerant of markdown fences (```json … ```) and stray text.
    Returns {} on failure (fail-open)."""
    t = str(s).strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    t = t.strip()
    try:
        return json.loads(t)
    except Exception:
        # last resort: grab the outermost {...}
        a, b = t.find("{"), t.rfind("}")
        if a != -1 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except Exception:
                return {}
        return {}


def parse_gemini_response(json_text):
    """Parse the stored ``.response.json`` into a normalized analysis dict.

    ``json_text`` may be a JSON string or an already-parsed dict.  The pipeline
    wraps Gemini's object under a ``"response"`` key alongside metadata
    (``fileName``, ``driveId``, ``processedAt`` …); we read ``response`` and fall
    back to the top level if absent.  Fail-open: any problem returns a copy of
    ``EMPTY_ANALYSIS``.

    Returned ``detected_slogans`` entries are normalized to
    ``{index, slogan, x, y, size, confidence}`` with ``size``/``confidence`` as
    ``float`` or ``None``.
    """
    data = json_text
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8")
        except Exception:
            return dict(EMPTY_ANALYSIS, detected_slogans=[], flagged_problem_slogans=[])
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return dict(EMPTY_ANALYSIS, detected_slogans=[], flagged_problem_slogans=[])
    if not isinstance(data, dict):
        return dict(EMPTY_ANALYSIS, detected_slogans=[], flagged_problem_slogans=[])

    resp = data.get("response")
    if isinstance(resp, str):
        resp = _loads_loose(resp)   # Gemini sometimes stores JSON-as-text
    # Some Gem outputs nest the analysis as text under response.raw_response
    # (with a chat preamble like "Button Identifier said\n\n{...}").  Dig it out.
    if isinstance(resp, dict) and "total_button_count" not in resp \
            and "detected_slogans" not in resp:
        for _k in ("raw_response", "text", "output", "content", "raw"):
            _v = resp.get(_k)
            if isinstance(_v, str):
                _parsed = _loads_loose(_v)
                if _parsed:
                    resp = _parsed
                    break
    if not isinstance(resp, dict):
        resp = data  # tolerate a bare analysis object

    raw_slogans = resp.get("detected_slogans") or []
    slogans = []
    if isinstance(raw_slogans, list):
        for i, s in enumerate(raw_slogans):
            if not isinstance(s, dict):
                continue
            slogan = str(s.get("slogan", "")).strip()
            if not slogan:
                continue
            slogans.append({
                "index": _as_int(s.get("index"), default=i + 1),
                "slogan": slogan,
                "x": _as_float(s.get("x")),
                "y": _as_float(s.get("y")),
                "size": _as_float(s.get("radius", s.get("size"))),
                "size_class": _parse_size_class(s.get("size_class", s.get("size"))),
                "edge_x": _edge_coord(s, "x"),
                "edge_y": _edge_coord(s, "y"),
                "confidence": _parse_confidence(s.get("confidence")),
            })

    # --- coordinate-scale normalization -------------------------------------
    # The Gem prompt asks for 0-100 PERCENT coordinates, but Gemini INTERMITTENTLY
    # answers on its native 0-1000 normalized scale instead (seen live: one lot's
    # buttons at x/y 224-692, another's at 40/45).  Downstream geometry
    # (gemini_geometry.pct_to_px) divides by 100, so a 0-1000 set lands ~10x off
    # the frame — every point falls outside, gemini_led_crops/reconcile recover
    # nothing, and the lot collapses to a blind projection grid (the navy-8
    # "complete fail"; overlaying the coords ÷1000 lands dead on all 8 buttons).
    # A percent value can't exceed 100, so if the response's MAX coordinate does,
    # the set is 0-1000 → rescale it (÷10) back to percent, leaving everything
    # downstream unchanged.  ``coord_scale`` records which convention the Gem used
    # so we can measure how often it ignores the percent instruction.
    coord_scale = None
    _coords = [v for sl in slogans
               for v in (sl["x"], sl["y"], sl["edge_x"], sl["edge_y"])
               if v is not None]
    if _coords:
        coord_scale = "permille" if max(_coords) > 100 else "percent"
        if coord_scale == "permille":
            for sl in slogans:
                for _k in ("x", "y", "edge_x", "edge_y", "size"):
                    if sl[_k] is not None:
                        sl[_k] = sl[_k] / 10.0

    flagged = resp.get("flagged_problem_slogans") or []
    if not isinstance(flagged, list):
        flagged = []

    return {
        "total_button_count": _as_int(resp.get("total_button_count")),
        "blue_background_count": _as_int(resp.get("blue_background_count")),
        "white_background_count": _as_int(resp.get("white_background_count")),
        "detected_slogans": slogans,
        "flagged_problem_slogans": flagged,
        "coord_scale": coord_scale,
    }
