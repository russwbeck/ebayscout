import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    from ebayscout import label_harvest as lh
except ImportError:
    import label_harvest as lh


def test_harvest_enabled_env():
    os.environ.pop("BUTTONMATCHER_LABEL_HARVEST", None)
    assert lh.harvest_enabled() is True
    os.environ["BUTTONMATCHER_LABEL_HARVEST"] = "0"
    assert lh.harvest_enabled() is False
    os.environ.pop("BUTTONMATCHER_LABEL_HARVEST", None)


def test_label_blob_names_are_prefixed_and_safe():
    j, i = lh.label_blob_names("abc-123")
    assert j == "pipeline/labels/abc-123.json"
    assert i == "pipeline/labels/abc-123.jpg"
    j, _ = lh.label_blob_names("evil/../path")
    assert "/" not in j[len(lh.LABELS_PREFIX):].replace(".json", "").replace("..", "x")


def test_build_label_record_circles_and_sources():
    rec = lh.build_label_record(
        job_id="j1", service="ebayscout", command="daily-pipeline",
        image_name="ebayscout__x.png", item_id="123", img_w=800, img_h=600,
        circle_info=[
            {"shape": "circle", "x": 100, "y": 120, "r": 40},
            {"shape": "rect", "x1": 10, "y1": 20, "x2": 90, "y2": 100,
             "cx": 50, "cy": 60, "r": 40},
            {"shape": "circle", "x": 300, "y": 300, "r": 41},
        ],
        circle_sources=["hough", "hough"],   # shorter than circles: pads None
        detector_used="hough+blob", mask_path="blue_or_white+satfallback_blue",
        mask_coverage=0.41, ni_selected=3, ni_gate="auto",
        ni_scale_path="scale_first",
        gemini_button_count=3, gemini_flagged_count=0,
        gemini_slogans=[{"index": 1, "text": "Wipe Out", "x": 12.5, "y": 20.0}],
    )
    assert rec["schema"] == lh.SCHEMA
    assert rec["image"] == {"w": 800, "h": 600, "blob": "pipeline/labels/j1.jpg"}
    assert len(rec["circles"]) == 3
    assert rec["circles"][0] == {"i": 0, "source": "hough", "shape": "circle",
                                 "x": 100, "y": 120, "r": 40}
    assert rec["circles"][1]["shape"] == "rect"
    assert rec["circles"][1]["cx"] == 50 and rec["circles"][1]["x2"] == 90
    assert rec["circles"][2]["source"] is None          # padded, fail-open
    assert rec["gemini"]["button_count"] == 3
    assert rec["gemini"]["slogans"][0]["text"] == "Wipe Out"
    assert rec["detection"]["ni_gate"] == "auto"
    assert rec["confirm_join"] == "confirm_log.job_id"


def test_build_label_record_tags_gemini_backed_when_known():
    circle_info = [
        {"shape": "circle", "x": 100, "y": 100, "r": 20},
        {"shape": "circle", "x": 300, "y": 300, "r": 20},
        {"shape": "circle", "x": 700, "y": 700, "r": 20},
    ]
    rec = lh.build_label_record(
        job_id="j3", service="ebayscout", command="daily-pipeline",
        img_w=1000, img_h=1000,
        circle_info=circle_info, circle_sources=["hough", "hough", "hough"],
        unmatched_crop_indices=[2],
    )
    assert [c.get("gemini_backed") for c in rec["circles"]] == [True, True, False]


def test_build_label_record_omits_gemini_backed_when_unknown():
    # unmatched_crop_indices=None (default) → match couldn't meaningfully run;
    # the key must be ABSENT, never guessed as True or False.
    circle_info = [{"shape": "circle", "x": 100, "y": 100, "r": 20}]
    rec = lh.build_label_record(
        job_id="j4", service="ebayscout", command="daily-pipeline",
        img_w=1000, img_h=1000,
        circle_info=circle_info, circle_sources=["hough"],
    )
    assert "gemini_backed" not in rec["circles"][0]


def test_build_label_record_empty_lot_is_fine():
    rec = lh.build_label_record(
        job_id="j2", service="buttonmatcher", command="/pipeline",
        img_w=10, img_h=10, circle_info=[], circle_sources=None,
    )
    assert rec["circles"] == [] and rec["gemini"]["slogans"] == []


def test_gemini_count_inconsistent_helper():
    # total == localized + flagged → consistent
    assert lh._gemini_count_inconsistent(10, [{}] * 8, 2) is False
    # total > localized + flagged → Gemini overcounted (turf/carpet case)
    assert lh._gemini_count_inconsistent(15, [{}] * 8, 2) is True
    assert lh._gemini_count_inconsistent(5, [{}] * 5, 0) is False
    # no usable count → None (not False, so "unknown" is never read as "consistent")
    assert lh._gemini_count_inconsistent(0, [], 0) is None
    assert lh._gemini_count_inconsistent(None, None, None) is None
    # fail-open on bad input
    assert lh._gemini_count_inconsistent("x", [{}], 0) is None


def test_build_label_record_logs_count_inconsistent():
    # Gemini claims 15 but itemised only 8 slogans + 2 flagged = 10 → inconsistent.
    rec = lh.build_label_record(
        job_id="j3", service="buttonmatcher", command="/pipeline",
        img_w=10, img_h=10, circle_info=[], circle_sources=None,
        gemini_button_count=15, gemini_flagged_count=2, gemini_slogans=[{"x": 1}] * 8,
    )
    assert rec["gemini"]["count_inconsistent"] is True
    # A consistent lot logs False, and a countless lot logs None.
    rec2 = lh.build_label_record(
        job_id="j4", service="buttonmatcher", command="/pipeline",
        img_w=10, img_h=10, circle_info=[], circle_sources=None,
        gemini_button_count=5, gemini_flagged_count=0, gemini_slogans=[{"x": 1}] * 5,
    )
    assert rec2["gemini"]["count_inconsistent"] is False
    rec3 = lh.build_label_record(
        job_id="j5", service="buttonmatcher", command="/pipeline",
        img_w=10, img_h=10, circle_info=[], circle_sources=None,
    )
    assert rec3["gemini"]["count_inconsistent"] is None
