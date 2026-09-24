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


def _parse_printed_year(v):
    """The on-button printed year marker as an int, or None.

    Accepts a 4-digit year in [1960, 2035] ("1984", 1984.0, " 2019 ") and
    the two-digit marker forms the Gem prompt acknowledges exist on the
    buttons ("'97", "'19", 97, 19): the marker eras make two digits
    unambiguous — 83/84 are 1983/1984, 97-99 are 19xx, 00-35 are 20xx.
    Everything else (85-96 two-digit, junk, null, hallucinated centuries)
    parses to None so a bad read can never resolve a twin edition."""
    if v is None:
        return None
    try:
        y = int(float(str(v).strip().lstrip("'\u2019")))
    except (TypeError, ValueError):
        return None
    if 0 <= y <= 99:
        # two-digit marker: map through the known marker eras only
        if y in (83, 84):
            y += 1900
        elif 97 <= y <= 99:
            y += 1900
        elif y <= 35:
            y += 2000
        else:
            return None
    return y if 1960 <= y <= 2035 else None


def parse_gemini_response(json_text):
    """Parse the stored ``.response.json`` into a normalized analysis dict.

    ``json_text`` may be a JSON string or an already-parsed dict.  The pipeline
    wraps Gemini's object under a ``"response"`` key alongside metadata
    (``fileName``, ``driveId``, ``processedAt`` …); we read ``response`` and fall
    back to the top level if absent.  Fail-open: any problem returns a copy of
    ``EMPTY_ANALYSIS``.

    Returned ``detected_slogans`` entries are normalized to
    ``{index, slogan, x, y, size, confidence, printed_year}`` with
    ``size``/``confidence`` as ``float`` or ``None`` and ``printed_year`` the
    small on-button year marker (int, 1960-2035) when the Gem reported one —
    None otherwise.  Buttons from 1983, 1984 and 1997-2025 carry the marker;
    the field powers twin-edition resolution and the year-bias audit.
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
                "printed_year": _parse_printed_year(s.get("printed_year")),
            })

    # --- coordinate-scale normalization -------------------------------------
    # The Gem prompt asks for 0-100 PERCENT coordinates, but Gemini INTERMITTENTLY
    # answers on its native 0-1000 normalized scale instead (seen live: one lot's
    # buttons at x/y 224-692, another's at 40/45).  Downstream geometry
    # (gemini_geometry.pct_to_px) divides by 100, so a 0-1000 set lands ~10x off
    # the frame — every point falls outside, gemini_led_crops/reconcile recover
    # nothing, and the lot collapses to a blind projection grid (the navy-8
    # "complete fail"; overlaying the coords / 1000 lands dead on all 8 buttons).
    # A percent value can't exceed 100, so if the MAX coordinate does, that set is
    # 0-1000 -> rescale it (/10) back to percent, leaving downstream unchanged.
    #
    # PER AXIS, because Gemini mixes the two conventions WITHIN ONE RESPONSE.
    # Measured on the 2008 Citizens lot (2026-09-16, job posted 17:28 EDT):
    # x came back as percent (18-69) and y as permille (64-693) in the same
    # object.  A whole-response max() called the set permille and divided BOTH
    # axes by 10: y landed correctly (6.4/21/39.3/69.3%) and x was crushed into
    # 1.8-6.9%, i.e. an 11-41px strip down the left edge of a 600px-wide frame.
    # Every one of the 12 Hough circles then read as unanchored (nearest Gemini
    # point 81-399px away against a 0.75*r = 37.5px gate), so AUTO was refused
    # for all 12 real buttons; anchor recovery then synthesized a crop at each
    # of the 10 mis-placed points, and those ARE anchored by construction
    # (dist ~ 0 to the point that created them), so all 10 phantoms
    # auto-confirmed.  Posted: "22 buttons (Hough 12, +10 recovered) - 10
    # Gemini-confirmed - 12 need review".  Had the operator clicked Inventory,
    # 10 phantom counts would have written to the sheet with no click.
    # tests/test_pipeline_ingest.py carries that response verbatim as a fixture.
    #
    # ``size`` is a length, not a coordinate on either axis, so it is only
    # rescaled when BOTH axes agree it is a permille response.  On a mixed
    # response its scale is unknowable, so it is dropped to None and the
    # callers' median-detected-radius fallback takes over rather than guessing
    # a radius that is 10x off in one direction or the other.
    def _axis_scale(keys):
        vals = [v for sl in slogans for k in keys
                if (v := sl[k]) is not None]
        if not vals:
            return None
        return "permille" if max(vals) > 100 else "percent"

    coord_scale_x = _axis_scale(("x", "edge_x"))
    coord_scale_y = _axis_scale(("y", "edge_y"))
    # Summary for the label sidecar / telemetry: the shared convention when the
    # axes agree, else "mixed" — the one value that says this response needed
    # per-axis handling, and the signal to look for if a lot goes wrong.
    # An axis with no coordinates at all has no opinion (None) — that is not a
    # disagreement, so it defers to the axis that does.
    if coord_scale_x is None or coord_scale_y is None:
        coord_scale = coord_scale_x or coord_scale_y
    elif coord_scale_x == coord_scale_y:
        coord_scale = coord_scale_x
    else:
        coord_scale = "mixed"
    _axis_keys = (("x", "edge_x", coord_scale_x), ("y", "edge_y", coord_scale_y))
    for sl in slogans:
        for _kx, _ke, _sc in _axis_keys:
            if _sc != "permille":
                continue
            for _k in (_kx, _ke):
                if sl[_k] is not None:
                    sl[_k] = sl[_k] / 10.0
        if sl["size"] is not None:
            if coord_scale == "permille":
                sl["size"] = sl["size"] / 10.0
            elif coord_scale == "mixed":
                sl["size"] = None

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
        "coord_scale_x": coord_scale_x,
        "coord_scale_y": coord_scale_y,
    }


# --- Large-lot split-and-merge (2026-09-24) ----------------------------------
# One Gem read of a ~90-button lot came back with 128 buttons listed, its rows
# spread up into bare carpet; the same photo cut into two halves by hand read
# almost exactly right (43 + 52 crops, 9 skipped).  Fewer buttons per read and
# less empty frame for Gemini to spread into.  So a large lot is re-read as
# overlapping strips along its LONG side, and the strip reads are merged back
# into one analysis in the whole photo's percent frame.  A button on a seam is
# read twice (the overlap guarantees it is whole in at least one strip); the
# merge keeps the copy farthest from its own strip's cut edge.
#
# Pure and stdlib-only like the rest of this module; the caller owns the pixels
# and the GCS round-trip.

SPLIT_OVERLAP_FRAC = 0.14


def plan_split_tiles(w, h, n_tiles=2, overlap_frac=SPLIT_OVERLAP_FRAC):
    """Pixel boxes ``(x1, y1, x2, y2)`` for ``n_tiles`` strips along the long
    side of a ``w`` x ``h`` image.  Each seam is widened by ``overlap_frac`` of
    the long side (half on each side), so a button up to that size is whole in
    at least one strip.  Strips never extend past the frame."""
    w, h = int(w), int(h)
    n = max(1, int(n_tiles))
    long_len = h if h >= w else w
    half = int(round(overlap_frac * long_len / 2.0))
    boxes = []
    for i in range(n):
        a = int(round(i * long_len / n))
        b = int(round((i + 1) * long_len / n))
        a = max(0, a - (half if i > 0 else 0))
        b = min(long_len, b + (half if i < n - 1 else 0))
        boxes.append((0, a, w, b) if h >= w else (a, 0, b, h))
    return boxes


def _inner_margin(px, py, box, full_w, full_h):
    """Distance from a point to its strip's nearest CUT edge (an edge that is
    not the photo's own border); inf when the strip has none."""
    x1, y1, x2, y2 = box
    d = []
    if x1 > 0:
        d.append(px - x1)
    if y1 > 0:
        d.append(py - y1)
    if x2 < full_w:
        d.append(x2 - px)
    if y2 < full_h:
        d.append(y2 - py)
    return min(d) if d else float("inf")


def _median(vals):
    v = sorted(vals)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2.0


def merge_tile_analyses(tiles, full_w, full_h):
    """Merge per-strip analyses into one analysis for the whole photo.

    ``tiles`` is a list of ``{"box": (x1, y1, x2, y2), "analysis": <parsed>}``
    where each analysis is ``parse_gemini_response`` output for that strip (so
    each strip's percent/permille scale was already normalized on its own).

    Returns ``(response, telemetry)``: ``response`` is a raw Gem-shaped object
    (percent coordinates in the WHOLE photo's frame, 1-based indices in reading
    order) that ``parse_gemini_response`` reads back unchanged; ``telemetry``
    says what the merge did.

    Duplicates: two points from DIFFERENT strips closer than about one button
    radius (half the median nearest-neighbour spacing — adjacent real buttons
    sit about a diameter apart) are one button read twice.  The copy farther
    from its own strip's cut edge is kept, since the other one is the more
    likely to be cut off.  Points without x/y cannot be matched and are kept.
    Flagged entries follow their slogan's index (to the survivor, for a dropped
    duplicate); a flagged index that names no located slogan is dropped, since
    it cannot be placed on the photo and a seam cut-off produces exactly that.
    """
    fw, fh = float(full_w), float(full_h)
    pts = []          # located: dict(tile, s, px, py, margin)
    unlocated = []    # (tile, s)
    spacings = []
    for ti, t in enumerate(tiles):
        x1, y1, x2, y2 = t["box"]
        tw, th = float(x2 - x1), float(y2 - y1)
        an = t.get("analysis") or {}
        tile_pts = []
        for s in an.get("detected_slogans") or []:
            if s.get("x") is None or s.get("y") is None:
                unlocated.append((ti, s))
                continue
            px = x1 + s["x"] / 100.0 * tw
            py = y1 + s["y"] / 100.0 * th
            e = {"tile": ti, "s": s, "px": px, "py": py,
                 "margin": _inner_margin(px, py, t["box"], fw, fh)}
            if s.get("edge_x") is not None and s.get("edge_y") is not None:
                e["ex"] = x1 + s["edge_x"] / 100.0 * tw
                e["ey"] = y1 + s["edge_y"] / 100.0 * th
            if s.get("size") is not None:
                e["r"] = s["size"] / 100.0 * min(tw, th)
            tile_pts.append(e)
        if len(tile_pts) >= 2:
            nn = [min(((a["px"] - b["px"]) ** 2 + (a["py"] - b["py"]) ** 2) ** 0.5
                      for b in tile_pts if b is not a) for a in tile_pts]
            spacings.append(_median(nn))
        pts.extend(tile_pts)

    sp = _median([v for v in spacings if v])
    dup_px = 0.5 * sp if sp else 0.04 * min(fw, fh)

    # Cross-strip pairs within dup_px, closest first, one-to-one.
    cands = []
    for i, a in enumerate(pts):
        for j in range(i + 1, len(pts)):
            b = pts[j]
            if a["tile"] == b["tile"]:
                continue
            d = ((a["px"] - b["px"]) ** 2 + (a["py"] - b["py"]) ** 2) ** 0.5
            if d <= dup_px:
                cands.append((d, i, j))
    cands.sort()
    survivor = {}     # dropped pts index -> kept pts index
    used = set()
    for _d, i, j in cands:
        if i in used or j in used:
            continue
        used.update((i, j))
        keep, drop = (i, j) if pts[i]["margin"] >= pts[j]["margin"] else (j, i)
        survivor[drop] = keep

    kept = [k for k in range(len(pts)) if k not in survivor]
    kept.sort(key=lambda k: (pts[k]["py"], pts[k]["px"]))
    new_index = {k: n + 1 for n, k in enumerate(kept)}
    for drop, keep in survivor.items():
        new_index[drop] = new_index[keep]

    def _pct(v, full):
        return round(min(100.0, max(0.0, v / full * 100.0)), 3)

    out = []
    for k in kept:
        e = pts[k]
        s = e["s"]
        o = {"index": new_index[k], "slogan": s.get("slogan"),
             "x": _pct(e["px"], fw), "y": _pct(e["py"], fh)}
        if "ex" in e:
            o["edge_x"] = _pct(e["ex"], fw)
            o["edge_y"] = _pct(e["ey"], fh)
        if "r" in e:
            o["radius"] = round(e["r"] / min(fw, fh) * 100.0, 3)
        for key in ("size_class", "confidence", "printed_year"):
            if s.get(key) is not None:
                o[key] = s[key]
        out.append(o)
    nxt = len(out)
    for _ti, s in unlocated:
        nxt += 1
        o = {"index": nxt, "slogan": s.get("slogan")}
        for key in ("size_class", "confidence", "printed_year"):
            if s.get(key) is not None:
                o[key] = s[key]
        out.append(o)

    # Flagged entries: strip-local index -> merged index via that strip's slogan.
    by_tile_index = {}
    for k, e in enumerate(pts):
        by_tile_index.setdefault((e["tile"], e["s"].get("index")), k)
    flagged, n_flag_dropped = [], 0
    for ti, t in enumerate(tiles):
        for f in (t.get("analysis") or {}).get("flagged_problem_slogans") or []:
            if not isinstance(f, dict):
                continue
            k = by_tile_index.get((ti, f.get("index")))
            if k is None:
                n_flag_dropped += 1
                continue
            nf = dict(f)
            nf["index"] = new_index[k]
            if nf not in flagged:
                flagged.append(nf)

    response = {
        "total_button_count": len(out),
        "blue_background_count": sum(
            int((t.get("analysis") or {}).get("blue_background_count") or 0)
            for t in tiles),
        "white_background_count": sum(
            int((t.get("analysis") or {}).get("white_background_count") or 0)
            for t in tiles),
        "detected_slogans": out,
        "flagged_problem_slogans": flagged,
    }
    telemetry = {
        "n_tiles": len(tiles),
        "per_tile": [len((t.get("analysis") or {}).get("detected_slogans") or [])
                     for t in tiles],
        "coord_scale": [(t.get("analysis") or {}).get("coord_scale") for t in tiles],
        "n_merged": len(out),
        "n_seam_dupes": len(survivor),
        "dup_px": round(dup_px, 2),
        "n_flagged_dropped": n_flag_dropped,
        "blank_tiles": [i for i, t in enumerate(tiles)
                        if not (t.get("analysis") or {}).get("detected_slogans")],
    }
    return response, telemetry
