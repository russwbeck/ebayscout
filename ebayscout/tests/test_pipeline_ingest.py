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
