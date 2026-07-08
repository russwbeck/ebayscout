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


def test_build_label_record_empty_lot_is_fine():
    rec = lh.build_label_record(
        job_id="j2", service="buttonmatcher", command="/pipeline",
        img_w=10, img_h=10, circle_info=[], circle_sources=None,
    )
    assert rec["circles"] == [] and rec["gemini"]["slogans"] == []
