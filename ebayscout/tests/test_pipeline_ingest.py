"""Unit tests for pipeline_ingest — pure-python, no cloud/flask needed.

    python tests/run_pipeline_ingest_tests.py
"""

import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline_ingest as pi


# --- parse_pubsub_envelope ---------------------------------------------------

def _push_body(attributes=None, data_obj=None):
    msg = {}
    if attributes is not None:
        msg["attributes"] = attributes
    if data_obj is not None:
        msg["data"] = base64.b64encode(json.dumps(data_obj).encode()).decode()
    return {"message": msg, "subscription": "projects/x/subscriptions/y"}


def test_envelope_from_attributes():
    body = _push_body(attributes={
        "bucketId": "60d488c5-9c8e-4acc-aac-button-data",
        "objectId": "pipeline/output/a.png.response.json",
        "eventType": "OBJECT_FINALIZE",
    })
    out = pi.parse_pubsub_envelope(body)
    assert out["bucket"] == "60d488c5-9c8e-4acc-aac-button-data"
    assert out["name"] == "pipeline/output/a.png.response.json"
    assert out["event_type"] == "OBJECT_FINALIZE"


def test_envelope_falls_back_to_data_payload():
    body = _push_body(data_obj={"bucket": "b", "name": "pipeline/output/x.png"})
    out = pi.parse_pubsub_envelope(body)
    assert out["name"] == "pipeline/output/x.png"
    assert out["bucket"] == "b"


def test_envelope_accepts_raw_json_string():
    body = json.dumps(_push_body(attributes={"objectId": "pipeline/output/z.png.response.json"}))
    out = pi.parse_pubsub_envelope(body)
    assert out["name"] == "pipeline/output/z.png.response.json"


def test_envelope_malformed_returns_none():
    assert pi.parse_pubsub_envelope("not json") is None
    assert pi.parse_pubsub_envelope({}) is None
    assert pi.parse_pubsub_envelope({"message": {}}) is None
    assert pi.parse_pubsub_envelope({"message": {"attributes": {}}}) is None


# --- is_response_json / image_name_for_response ------------------------------

def test_is_response_json():
    assert pi.is_response_json("pipeline/output/a.png.response.json")
    assert pi.is_response_json("pipeline/output/sub/a.jpg.response.json")
    # the bare image must NOT trigger
    assert not pi.is_response_json("pipeline/output/a.png")
    # wrong prefix
    assert not pi.is_response_json("other/a.png.response.json")
    assert not pi.is_response_json("")
    assert not pi.is_response_json(None)


def test_image_name_for_response():
    assert (pi.image_name_for_response("pipeline/output/a.png.response.json")
            == "pipeline/output/a.png")
    # round-trips an arbitrary extension
    assert (pi.image_name_for_response("pipeline/output/b.jpg.response.json")
            == "pipeline/output/b.jpg")
    assert pi.image_name_for_response("pipeline/output/a.png") is None


# --- parse_gemini_response ---------------------------------------------------

FULL = {
    "fileName": "buttons.jpg",
    "driveId": "1-abc",
    "response": {
        "total_button_count": 3,
        "blue_background_count": 2,
        "white_background_count": 1,
        "detected_slogans": [
            {"index": 1, "slogan": "Stop Stanford", "x": 12, "y": 8, "radius": 9, "confidence": "high"},
            {"index": 2, "slogan": "Whip the Wolfpack", "x": 34, "y": 22},
        ],
        "flagged_problem_slogans": [
            {"index": 3, "reason": "text cut off at right edge", "partial_text": "Beat the..."}
        ],
    },
}


def test_parse_full_response():
    out = pi.parse_gemini_response(FULL)
    assert out["total_button_count"] == 3
    assert out["blue_background_count"] == 2
    assert len(out["detected_slogans"]) == 2
    s0 = out["detected_slogans"][0]
    assert s0["slogan"] == "Stop Stanford"
    assert s0["x"] == 12.0 and s0["y"] == 8.0
    # Gem emits "radius"; stored under internal key "size". "high" → 0.90.
    assert s0["size"] == 9.0 and s0["confidence"] == 0.90
    # optional fields absent → None (back-compat with old Gem output)
    s1 = out["detected_slogans"][1]
    assert s1["size"] is None and s1["confidence"] is None
    assert s1["index"] == 2
    # flagged entries pass through with the new {index, reason, partial_text} shape
    assert out["flagged_problem_slogans"][0]["index"] == 3
    assert out["flagged_problem_slogans"][0]["partial_text"] == "Beat the..."


def test_parse_accepts_json_string_and_bare_object():
    out = pi.parse_gemini_response(json.dumps(FULL))
    assert out["total_button_count"] == 3
    # bare analysis (no "response" wrapper)
    out2 = pi.parse_gemini_response(FULL["response"])
    assert out2["total_button_count"] == 3


def test_parse_skips_blank_slogans_and_defaults_index():
    blob = {"response": {"total_button_count": 2, "detected_slogans": [
        {"slogan": "", "x": 1, "y": 1},
        {"slogan": "Beat Pitt", "x": 5, "y": 6},
    ]}}
    out = pi.parse_gemini_response(blob)
    assert len(out["detected_slogans"]) == 1
    assert out["detected_slogans"][0]["slogan"] == "Beat Pitt"
    # index defaults to the raw reading position (blank entry occupied position 1)
    assert out["detected_slogans"][0]["index"] == 2


def test_parse_stringified_response_object():
    # Some pipeline outputs store the Gemini analysis as JSON-as-text under "response".
    inner = '{"total_button_count": 13, "detected_slogans": [{"index":1,"slogan":"Stop Stanford","x":5,"y":5,"radius":4,"confidence":"high"}]}'
    out = pi.parse_gemini_response({"fileName": "x.png", "response": inner})
    assert out["total_button_count"] == 13
    assert out["detected_slogans"][0]["slogan"] == "Stop Stanford"
    assert out["detected_slogans"][0]["size"] == 4.0


def test_parse_response_raw_response_with_preamble():
    # Real bucket format: response.raw_response holds the Gem reply as text with a
    # chat preamble before the JSON object, and slogans without radius/confidence.
    raw = ('Button Identifier\nCustom Gem\nButton Identifier said\n\n'
           '{\n"total_button_count": 13,\n"blue_background_count": 11,\n'
           '"white_background_count": 2,\n"detected_slogans": [\n'
           '{ "index": 1, "slogan": "Wolf Pack Folds", "x": 14, "y": 14 },\n'
           '{ "index": 13, "slogan": "Lions De-Stripe Tigers", "x": 50, "y": 86 }\n'
           '],\n"flagged_problem_slogans": []\n}')
    blob = {"fileName": "Screenshot.png", "response": {"raw_response": raw}}
    out = pi.parse_gemini_response(blob)
    assert out["total_button_count"] == 13
    assert out["blue_background_count"] == 11
    assert len(out["detected_slogans"]) == 2
    assert out["detected_slogans"][0]["slogan"] == "Wolf Pack Folds"
    assert out["detected_slogans"][1]["index"] == 13
    # no radius/confidence in this format → None (fail-open: median radius, no gate)
    assert out["detected_slogans"][0]["size"] is None
    assert out["detected_slogans"][0]["confidence"] is None


def test_parse_stringified_response_with_markdown_fence():
    inner = '```json\n{"total_button_count": 2, "detected_slogans": []}\n```'
    out = pi.parse_gemini_response({"response": inner})
    assert out["total_button_count"] == 2


def test_parse_failopen_on_garbage():
    out = pi.parse_gemini_response("{ not valid")
    assert out["total_button_count"] == 0
    assert out["detected_slogans"] == []
    out2 = pi.parse_gemini_response(None)
    assert out2["detected_slogans"] == []


def test_parse_radius_field_with_size_fallback():
    blob = {"response": {"total_button_count": 2, "detected_slogans": [
        {"index": 1, "slogan": "Radius", "x": 1, "y": 1, "radius": 7},
        {"index": 2, "slogan": "OldSize", "x": 2, "y": 2, "size": 4},  # back-compat
    ]}}
    out = pi.parse_gemini_response(blob)
    assert out["detected_slogans"][0]["size"] == 7.0   # radius → internal "size"
    assert out["detected_slogans"][1]["size"] == 4.0   # legacy size still read


def test_parse_categorical_confidence_labels():
    blob = {"response": {"total_button_count": 3, "detected_slogans": [
        {"index": 1, "slogan": "A", "x": 1, "y": 1, "confidence": "high"},
        {"index": 2, "slogan": "B", "x": 2, "y": 2, "confidence": "Medium"},
        {"index": 3, "slogan": "C", "x": 3, "y": 3, "confidence": "LOW"},
    ]}}
    out = pi.parse_gemini_response(blob)
    conf = [s["confidence"] for s in out["detected_slogans"]]
    # only "high" (0.90) clears the resolver gate at 0.70; case-insensitive
    assert conf == [0.90, 0.60, 0.30]


def test_parse_confidence_numeric_passthrough_and_unknown():
    blob = {"response": {"total_button_count": 2, "detected_slogans": [
        {"index": 1, "slogan": "Num", "x": 1, "y": 1, "confidence": 0.83},
        {"index": 2, "slogan": "Bad", "x": 2, "y": 2, "confidence": "very sure"},
    ]}}
    out = pi.parse_gemini_response(blob)
    assert out["detected_slogans"][0]["confidence"] == 0.83  # numeric still works
    assert out["detected_slogans"][1]["confidence"] is None  # unknown label → None (gate skipped)


def test_parse_confidence_helper_direct():
    assert pi._parse_confidence("high") == 0.90
    assert pi._parse_confidence("  Medium ") == 0.60
    assert pi._parse_confidence("low") == 0.30
    assert pi._parse_confidence(0.5) == 0.5
    assert pi._parse_confidence(None) is None
    assert pi._parse_confidence("") is None
    assert pi._parse_confidence("unknown") is None


def test_parse_coerces_string_numbers():
    blob = {"response": {"total_button_count": "4", "detected_slogans": [
        {"index": "2", "slogan": "X", "x": "10.5", "y": "20", "size": "8", "confidence": "0.7"},
    ]}}
    out = pi.parse_gemini_response(blob)
    assert out["total_button_count"] == 4
    s = out["detected_slogans"][0]
    assert s["index"] == 2 and s["x"] == 10.5 and s["confidence"] == 0.7


def test_parse_size_class():
    # categorical small/medium/large (case-insensitive); numeric/blank/unknown → None
    assert pi._parse_size_class("Large") == "large"
    assert pi._parse_size_class("medium") == "medium"
    assert pi._parse_size_class(12.5) is None
    assert pi._parse_size_class("huge") is None
    assert pi._parse_size_class(None) is None
    # numeric size still parses to "size"; a categorical "size" is caught as a class
    blob = {"response": {"detected_slogans": [
        {"slogan": "Num", "x": 1, "y": 1, "size": 12.5},
        {"slogan": "Cat", "x": 2, "y": 2, "size": "LARGE"},
        {"slogan": "Cls", "x": 3, "y": 3, "size_class": "small"},
    ]}}
    s = pi.parse_gemini_response(blob)["detected_slogans"]
    assert s[0]["size"] == 12.5 and s[0]["size_class"] is None
    assert s[1]["size_class"] == "large"
    assert s[2]["size_class"] == "small"


def test_parse_edge_point_flat_and_nested():
    blob = {"response": {"detected_slogans": [
        {"slogan": "Flat", "x": 50, "y": 50, "edge_x": 60, "edge_y": 50},
        {"slogan": "Nested", "x": 50, "y": 50, "edge": {"x": 50, "y": 40}},
        {"slogan": "None", "x": 50, "y": 50},
    ]}}
    s = pi.parse_gemini_response(blob)["detected_slogans"]
    assert s[0]["edge_x"] == 60.0 and s[0]["edge_y"] == 50.0
    assert s[1]["edge_x"] == 50.0 and s[1]["edge_y"] == 40.0
    assert s[2]["edge_x"] is None and s[2]["edge_y"] is None


def test_coord_scale_percent_left_unchanged():
    # 0-100 percent coords (max <= 100): coord_scale="percent", values unchanged.
    blob = {"response": {"total_button_count": 1, "detected_slogans": [
        {"slogan": "No Free Launch Here", "x": 40.0, "y": 45.0,
         "edge_x": 40.0, "edge_y": 30.0},
    ]}}
    a = pi.parse_gemini_response(blob)
    assert a["coord_scale"] == "percent"
    s = a["detected_slogans"][0]
    assert s["x"] == 40.0 and s["y"] == 45.0 and s["edge_y"] == 30.0


def test_coord_scale_permille_rescaled_to_percent():
    # 0-1000 native Gemini scale (a coord > 100): rescaled by /10 back to percent,
    # so downstream pct_to_px is unchanged.  Real navy-8 lot values.
    blob = {"response": {"total_button_count": 8, "detected_slogans": [
        {"slogan": "Needle the Rams", "x": 242.0, "y": 322.0,
         "edge_x": 242.0, "edge_y": 268.0, "size": 540.0},
        {"slogan": "Stuff 'N' Puff", "x": 424.0, "y": 692.0,
         "edge_x": 424.0, "edge_y": 636.0},
    ]}}
    a = pi.parse_gemini_response(blob)
    assert a["coord_scale"] == "permille"
    s0 = a["detected_slogans"][0]
    assert s0["x"] == 24.2 and s0["y"] == 32.2          # /10
    assert s0["edge_x"] == 24.2 and s0["edge_y"] == 26.8
    assert s0["size"] == 54.0                            # size rescaled too
    # /1000 * a 449-wide frame lands on the button (was 108px in the real image)
    assert round(s0["x"] / 100 * 449) == 109


def test_coord_scale_mixed_axes_2008_citizens_lot():
    """The real response that broke the 2026-09-16 2008 Citizens lot.

    Gemini answered with x in PERCENT (18-69) and y in PERMILLE (64-693) in the
    SAME object.  The old whole-response max() saw 693, called the set permille
    and divided both axes by 10 — y landed right, x was crushed into a 1.8-6.9%
    strip down the left edge.  Every real circle went unanchored, anchor
    recovery synthesized 10 crops at the mis-placed points, and because a
    synthesized crop is anchored to the point that created it, all 10 phantoms
    auto-confirmed while all 12 real buttons were demoted to manual cards
    ("22 buttons (Hough 12, +10 recovered) - 10 Gemini-confirmed - 12 need
    review").  Verbatim, so a regression reproduces the live failure exactly.
    """
    blob = {"response": {
        "total_button_count": 10,
        "blue_background_count": 9,
        "white_background_count": 1,
        "detected_slogans": [
            {"index": 1, "slogan": "Defeaticus Sparticus", "x": 19, "y": 64,
             "edge_x": 19, "edge_y": 50, "size": "medium",
             "printed_year": 2008, "confidence": "high"},
            {"index": 2, "slogan": "U Hoose U Lose", "x": 42, "y": 64,
             "edge_x": 42, "edge_y": 50, "size": "medium",
             "printed_year": 2008, "confidence": "high"},
            {"index": 3, "slogan": "Cheese Puffs", "x": 68, "y": 64,
             "edge_x": 68, "edge_y": 50, "size": "medium",
             "printed_year": 2008, "confidence": "high"},
            {"index": 4, "slogan": "Owl Shook Up", "x": 19, "y": 210,
             "edge_x": 19, "edge_y": 196, "size": "medium",
             "printed_year": 2008, "confidence": "high"},
            {"index": 5, "slogan": "Rule The Rooster", "x": 42, "y": 210,
             "edge_x": 42, "edge_y": 196, "size": "medium",
             "printed_year": 2008, "confidence": "high"},
            {"index": 6, "slogan": "I-O-Wasn't", "x": 68, "y": 210,
             "edge_x": 68, "edge_y": 196, "size": "medium",
             "printed_year": 2008, "confidence": "high"},
            {"index": 7, "slogan": "It's Fruitless, Orange", "x": 19, "y": 393,
             "edge_x": 19, "edge_y": 379, "size": "medium",
             "printed_year": 2008, "confidence": "high"},
            {"index": 8, "slogan": "In Our House Now", "x": 44, "y": 393,
             "edge_x": 44, "edge_y": 379, "size": "medium",
             "printed_year": 2008, "confidence": "high"},
            {"index": 9, "slogan": "Gee Wiz Wally", "x": 69, "y": 393,
             "edge_x": 69, "edge_y": 379, "size": "medium",
             "printed_year": 2008, "confidence": "high"},
            {"index": 10, "slogan": "USC-U-Later", "x": 18, "y": 693,
             "edge_x": 18, "edge_y": 679, "size": "medium",
             "printed_year": 2009, "confidence": "high"},
        ],
        "flagged_problem_slogans": [],
    }}
    a = pi.parse_gemini_response(blob)
    assert a["coord_scale"] == "mixed"
    assert a["coord_scale_x"] == "percent"
    assert a["coord_scale_y"] == "permille"

    s = a["detected_slogans"]
    # x is ALREADY percent and must survive untouched — this is the whole bug.
    assert [sl["x"] for sl in s] == [19, 42, 68, 19, 42, 68, 19, 44, 69, 18]
    assert [sl["edge_x"] for sl in s] == [19, 42, 68, 19, 42, 68, 19, 44, 69, 18]
    # y is permille and is rescaled to percent.
    assert [sl["y"] for sl in s] == [6.4, 6.4, 6.4, 21.0, 21.0, 21.0,
                                     39.3, 39.3, 39.3, 69.3]
    assert s[0]["edge_y"] == 5.0 and s[9]["edge_y"] == 67.9

    # In the 600x800 detection frame the points now land ON the buttons: three
    # columns near 114/252/408px (real centres ~122/265/410) instead of the
    # 11-41px left-edge strip the old code produced.
    xs = sorted({round(sl["x"] / 100 * 600) for sl in s})
    assert xs == [108, 114, 252, 264, 408, 414]
    # USC-U-Later — the white button Hough cannot find, and the one this bug
    # actually cost: right row, and now the right column too.
    assert round(s[9]["x"] / 100 * 600) == 108
    assert round(s[9]["y"] / 100 * 800) == 554


def test_coord_scale_mixed_drops_ambiguous_size():
    """A numeric ``size`` on a mixed-axis response is unknowable, so it is
    dropped and callers fall back to the median detected radius.  Guessing
    would put the synthesized crop 10x off in one direction or the other."""
    blob = {"response": {"detected_slogans": [
        {"slogan": "Mixed", "x": 40, "y": 400, "size": 6.0},
    ]}}
    s = pi.parse_gemini_response(blob)["detected_slogans"][0]
    assert s["x"] == 40 and s["y"] == 40.0
    assert s["size"] is None
    assert s["size_class"] is None          # 6.0 is not small/medium/large


def test_coord_scale_axis_with_no_coords_does_not_force_mixed():
    """y absent is silence, not disagreement — it must not flip the response to
    "mixed" and drop the size."""
    blob = {"response": {"detected_slogans": [
        {"slogan": "X only", "x": 240.0, "size": 50.0},
    ]}}
    a = pi.parse_gemini_response(blob)
    assert a["coord_scale"] == "permille"
    assert a["coord_scale_y"] is None
    assert a["detected_slogans"][0]["x"] == 24.0
    assert a["detected_slogans"][0]["size"] == 5.0


def test_coord_scale_none_when_no_coords():
    blob = {"response": {"total_button_count": 3, "detected_slogans": []}}
    assert pi.parse_gemini_response(blob)["coord_scale"] is None


def test_coord_scale_boundary_100_is_percent():
    # exactly 100 is a valid percent edge; only > 100 flips to permille.
    blob = {"response": {"detected_slogans": [
        {"slogan": "Edge", "x": 100.0, "y": 50.0},
    ]}}
    assert pi.parse_gemini_response(blob)["coord_scale"] == "percent"


def test_printed_year_parses_valid_and_rejects_junk():
    """printed_year (2026-07-16): the on-button year marker, strict by design
    — a bad read must never resolve a twin edition."""
    import pipeline_ingest as pi
    assert pi._parse_printed_year(1984) == 1984
    assert pi._parse_printed_year("2019") == 2019
    assert pi._parse_printed_year(" 1997 ") == 1997
    assert pi._parse_printed_year(2019.0) == 2019
    assert pi._parse_printed_year(None) is None
    assert pi._parse_printed_year("198") is None         # 3-digit junk maps to no era
    assert pi._parse_printed_year(3019) is None          # out of range
    assert pi._parse_printed_year("next year") is None


def test_printed_year_flows_through_parse_gemini_response():
    import json as _json
    import pipeline_ingest as pi
    resp = {"response": {"total_button_count": 2, "detected_slogans": [
        {"index": 1, "slogan": "Crush the Orange", "x": 20, "y": 30,
         "confidence": 0.9, "printed_year": 1973},
        {"index": 2, "slogan": "No Marker", "x": 60, "y": 30,
         "confidence": 0.9},
    ]}}
    out = pi.parse_gemini_response(_json.dumps(resp))
    assert out["detected_slogans"][0]["printed_year"] == 1973
    assert out["detected_slogans"][1]["printed_year"] is None


def test_printed_year_two_digit_marker_forms():
    """The Gem prompt acknowledges two-digit markers ('97, '19, '26) and asks
    for four digits — but when Gemini echoes the marker form anyway, the
    known marker eras make it unambiguous: 83/84 -> 19xx, 97-99 -> 19xx,
    00-35 -> 20xx.  85-96 two-digit stays None (no such markers — a misread)."""
    import pipeline_ingest as pi
    assert pi._parse_printed_year("'97") == 1997
    assert pi._parse_printed_year("'19") == 2019
    assert pi._parse_printed_year("'26") == 2026
    assert pi._parse_printed_year(83) == 1983
    assert pi._parse_printed_year("84") == 1984
    assert pi._parse_printed_year("00") == 2000
    assert pi._parse_printed_year(90) is None      # no 1990 marker exists
    assert pi._parse_printed_year("'86") is None


# --- Large-lot split-and-merge (2026-09-24) ----------------------------------

def _split_an(slogans, flagged=None):
    """A parse_gemini_response-shaped analysis (percent coords, tile frame)."""
    return {"detected_slogans": slogans, "flagged_problem_slogans": flagged or [],
            "blue_background_count": len(slogans), "white_background_count": 0,
            "coord_scale": "percent"}


def _sl(i, slogan, x, y, **kw):
    d = {"index": i, "slogan": slogan, "x": x, "y": y, "size": None,
         "size_class": None, "edge_x": None, "edge_y": None,
         "confidence": 0.9, "printed_year": None}
    d.update(kw)
    return d


def test_split_tiles_cut_the_long_side_and_overlap_the_seam():
    import pipeline_ingest as pi
    # portrait 1500x2000: strips stacked top/bottom, 14% of 2000 = 280 overlap
    boxes = pi.plan_split_tiles(1500, 2000, 2)
    assert boxes == [(0, 0, 1500, 1140), (0, 860, 1500, 2000)]
    # landscape: strips side by side
    boxes = pi.plan_split_tiles(2000, 1500, 2)
    assert boxes == [(0, 0, 1140, 1500), (860, 0, 2000, 1500)]
    # never past the frame, whole frame covered
    b3 = pi.plan_split_tiles(900, 1600, 3)
    assert b3[0][1] == 0 and b3[-1][3] == 1600
    assert all(0 <= y1 < y2 <= 1600 for _x1, y1, _x2, y2 in b3)
    assert all(b3[i][3] > b3[i + 1][1] for i in range(2))   # every seam overlaps


def _lot_tiles(W=1000, H=1000, rows=6, cols=5, jitter=0.0):
    """A synthetic lot read perfectly by two strips — the seam row(s) are read
    by BOTH strips.  Returns (tiles, true_centres_px)."""
    import pipeline_ingest as pi
    # row 3 sits at y=430, inside the 430..570 seam band: both strips read it
    true = [(100 + c * 190, 100 + r * 165) for r in range(rows) for c in range(cols)]
    boxes = pi.plan_split_tiles(W, H, 2)
    tiles = []
    for ti, (x1, y1, x2, y2) in enumerate(boxes):
        sl = []
        for k, (x, y) in enumerate(true):
            if y1 <= y < y2:
                j = jitter if ti else -jitter
                sl.append(_sl(len(sl) + 1, f"S{k}", (x - x1) / (x2 - x1) * 100,
                              (y + j - y1) / (y2 - y1) * 100))
        tiles.append({"box": boxes[ti], "analysis": _split_an(sl)})
    return tiles, true


def test_merge_maps_back_to_the_whole_photo_and_drops_seam_duplicates():
    import pipeline_ingest as pi
    tiles, true = _lot_tiles(jitter=6.0)       # the two reads disagree by 12px
    per_tile = [len(t["analysis"]["detected_slogans"]) for t in tiles]
    assert sum(per_tile) > len(true)           # the seam row really was read twice
    resp, tel = pi.merge_tile_analyses(tiles, 1000, 1000)
    got = resp["detected_slogans"]
    assert len(got) == len(true) == resp["total_button_count"]
    assert tel["n_seam_dupes"] == sum(per_tile) - len(true)
    assert sorted(s["slogan"] for s in got) == sorted(f"S{k}" for k in range(len(true)))
    for s in got:
        k = int(s["slogan"][1:])
        tx, ty = true[k]
        assert abs(s["x"] * 10 - tx) < 1.0 and abs(s["y"] * 10 - ty) < 7.0
    assert [s["index"] for s in got] == list(range(1, len(got) + 1))


def test_merge_keeps_the_seam_copy_farther_from_its_cut_edge():
    """A button near a strip's cut edge may be cut off in that strip; the other
    strip sees it whole.  The whole read is the one to keep."""
    import pipeline_ingest as pi
    boxes = pi.plan_split_tiles(1000, 1000, 2)          # cut edges at y=570 / 430
    (ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2) = boxes
    y = 555.0                                           # 15px above A's cut edge
    a = _split_an([_sl(1, "Cut Off", 50, (y - ay1) / (ay2 - ay1) * 100)])
    b = _split_an([_sl(1, "Whole Read", 50, (y + 4 - by1) / (by2 - by1) * 100),
                   _sl(2, "Far", 50, 90)])
    resp, tel = pi.merge_tile_analyses(
        [{"box": boxes[0], "analysis": a}, {"box": boxes[1], "analysis": b}],
        1000, 1000)
    names = [s["slogan"] for s in resp["detected_slogans"]]
    assert "Whole Read" in names and "Cut Off" not in names
    assert tel["n_seam_dupes"] == 1


def test_merge_never_collapses_two_real_neighbours_in_one_strip():
    """Dedup is cross-strip only: two touching buttons read by the SAME strip
    are two buttons, however close."""
    import pipeline_ingest as pi
    boxes = pi.plan_split_tiles(1000, 1000, 2)
    a = _split_an([_sl(1, "L", 40, 50), _sl(2, "R", 42, 50), _sl(3, "Far", 90, 10)])
    b = _split_an([_sl(1, "Z", 50, 90)])
    resp, tel = pi.merge_tile_analyses(
        [{"box": boxes[0], "analysis": a}, {"box": boxes[1], "analysis": b}],
        1000, 1000)
    assert len(resp["detected_slogans"]) == 4 and tel["n_seam_dupes"] == 0


def test_merged_response_round_trips_through_the_parser():
    """The merged object is written to GCS and read back by the normal build —
    it must parse to the same points (percent scale, not re-scaled), and keep
    confidence / printed year / radius / rim point."""
    import json as _json
    import pipeline_ingest as pi
    boxes = pi.plan_split_tiles(1000, 2000, 2)          # portrait: top/bottom
    a = _split_an([_sl(1, "Top", 30, 20, size=5.0, edge_x=35.0, edge_y=20.0,
                       printed_year=1998, confidence=0.6, size_class="large")])
    b = _split_an([_sl(1, "Bottom", 70, 80)])
    resp, _tel = pi.merge_tile_analyses(
        [{"box": boxes[0], "analysis": a}, {"box": boxes[1], "analysis": b}],
        1000, 2000)
    out = pi.parse_gemini_response(_json.dumps({"response": resp}))
    assert out["coord_scale"] == "percent"
    top, bottom = out["detected_slogans"]
    assert (top["slogan"], bottom["slogan"]) == ("Top", "Bottom")
    th = boxes[0][3] - boxes[0][1]                      # 1140
    assert abs(top["y"] - 0.20 * th / 2000 * 100) < 0.01
    assert abs(top["x"] - 30) < 0.01
    assert top["printed_year"] == 1998 and top["confidence"] == 0.6
    assert top["size_class"] == "large"
    # radius 5% of the strip's min side (1000) = 50px = 5% of the photo's 1000
    assert abs(top["size"] - 5.0) < 0.01
    assert abs(top["edge_x"] - 35.0) < 0.01
    by1, bh = boxes[1][1], boxes[1][3] - boxes[1][1]
    assert abs(bottom["y"] - (by1 + 0.8 * bh) / 2000 * 100) < 0.01


def test_merge_remaps_flagged_indices_and_drops_unplaceable_ones():
    import pipeline_ingest as pi
    boxes = pi.plan_split_tiles(1000, 1000, 2)
    a = _split_an([_sl(1, "A1", 50, 10), _sl(2, "A2", 20, 10)],
                  flagged=[{"index": 2, "reason": "smudged"},
                           {"index": 7, "reason": "cut off at edge"}])
    b = _split_an([_sl(1, "B1", 50, 90)], flagged=[{"index": 1, "reason": "glare"}])
    resp, tel = pi.merge_tile_analyses(
        [{"box": boxes[0], "analysis": a}, {"box": boxes[1], "analysis": b}],
        1000, 1000)
    idx = {s["slogan"]: s["index"] for s in resp["detected_slogans"]}
    flagged = {f["reason"]: f["index"] for f in resp["flagged_problem_slogans"]}
    assert flagged == {"smudged": idx["A2"], "glare": idx["B1"]}
    assert tel["n_flagged_dropped"] == 1


def test_merge_reports_a_blank_strip():
    """A strip whose read came back empty must be visible to the caller, which
    then falls back to the whole-photo read instead of posting half a lot."""
    import pipeline_ingest as pi
    boxes = pi.plan_split_tiles(1000, 1000, 2)
    _resp, tel = pi.merge_tile_analyses(
        [{"box": boxes[0], "analysis": _split_an([_sl(1, "A", 50, 50)])},
         {"box": boxes[1], "analysis": pi.parse_gemini_response("not json")}],
        1000, 1000)
    assert tel["blank_tiles"] == [1]
