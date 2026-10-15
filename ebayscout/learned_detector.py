"""learned_detector — the trained YOLO button detector: its count and its crops.

WHY THIS EXISTS (tools/detector/README.md)
------------------------------------------
Every counting approach in detect.py is hand-written rules.  On 2026-09-28 a
small YOLO model fine-tuned on 566 harvested label sidecars counted 22/26
hand-counted fixtures exactly (today's unguided detector: 13/26) and 66/69
held-out dense lots (the E2 dense stratum reads 65.9%).  Those are offline
numbers on a filtered set.  This module gets the real one: it runs the model on
every pipeline lot's detection image and records what it WOULD have counted.

THE MODEL CUTS THE CROPS ON THREE KINDS OF LOT.  The reading rides in the lot's
label sidecar (``learned_detector`` key) and one stdout line;
tools/detector/grade_shadow.py scores it against the operator-corrected count.
``learned_crops`` cuts the crops from the model's boxes:
- rollout step 2 (2026-10-02): where Hough collapsed to the blind grid, instead
  of at Gemini's guessed centres (the ``2022`` lot: Gemini placed twenty crops
  on blank paper, the model counted the true 13);
- rollout step 3 (2026-10-03): on every dense lot (the model counts 7 or more).
  The table lot it was built for: model 140, Hough 88, ~153 real.  Gated on
  98.4% exact dense counts (Gemini 80.6%) and on tools/detector/replay_crops.py:
  over 2,580 confirmed crops the model's crop ranked the true button higher
  than the lot's crop 45 times and lower 19 times.
- rollout step 4 (2026-10-08): on small lots (1-6) too, whenever the model
  counts at least one button.  Gated on tools/detector/grade_placement.py over
  92 finished small Hough lots: the model's boxes matched the operator's
  answers on 87 (Hough's crops: 74), and of its 5 misses three were Hough
  cutting rings inside one button or the operator skipping a real button, one
  a button back both detectors took for a button, and one an oval it scored
  under the threshold -- which falls back to Hough.
A lot where the model has no qualifying box keeps Hough.  Crops cut this way
pass the same auto-confirm guards as any other; auto_gate ``learned_layout``
refuses them only under BUTTONMATCHER_LEARNED_AUTO_GUARD=1.  Kill switches:
BUTTONMATCHER_LEARNED_LAYOUT_DENSE=0 (step 3 only),
BUTTONMATCHER_LEARNED_LAYOUT_SMALL=0 (step 4 only),
BUTTONMATCHER_LEARNED_LAYOUT=0 (all three).  ``step3_shadow`` records in the
sidecar how far the model's count was from Hough's on each lot.

The model is an ONNX export read from GCS (``MODEL_BLOB``) at first use, so
there is no binary in the repo and no new dependency: onnxruntime already
ships for CLIP.  No model in the bucket means the shadow is simply off.  Kill
switch: BUTTONMATCHER_LEARNED_DET=0.

Pure numpy apart from the onnxruntime session, so the letterbox, decode and
NMS maths are unit-testable without a model (tests/test_learned_detector.py).
numpy is imported where it is used: tools/detector/grade_shadow.py borrows
``count_at`` and must stay runnable on plain stdlib Python.

ONLY BUTTONS AT LEAST 80% IN THE PHOTO COUNT (operator, 2026-10-02).  Of the
model's six small-lot misses on its first live week, four were a button cut off
by the photo edge -- on sequential screenshots, the next screenshot's button --
which the operator does not count.  ``in_frame_fraction`` estimates how much of
a button a box clipped by the border shows, and ``count_at`` skips one under
``MIN_IN_FRAME``.  Every box is still stored, so grading can replay the rule.
The operator asked for 90%; a sweep over the 254 reviewed lots scored 80% best
(249/254 exact): at 90% the rule also dropped partly-cut buttons the operator
had identified and counted, while the strips it is for sit at 20-24%.
"""

from __future__ import annotations

import math
import os
import threading
import time

MODEL_BLOB = "models/detector/button_yolo.onnx"
IMGSZ = 640               # the export size; tools/detector trains at 640
DEFAULT_CONF = 0.40       # the 2026-09-28 validation sweep's pick
FLOOR_CONF = 0.15         # boxes kept for the record, so grading can re-sweep
NMS_IOU = 0.7             # Ultralytics' predict default -- grade.py's numbers used it
MIN_IN_FRAME = 0.80       # a button less than 80% inside the photo is not counted
EDGE_PX = 2.0             # a box side this close to the border was cut by it
MAX_DET = 500
RETRY_AFTER_S = 600       # after a failed load, do not retry on every lot

_lock = threading.Lock()
_state = {"session": None, "input": None, "generation": None,
          "failed_at": None, "error": None}


def enabled() -> bool:
    return os.environ.get("BUTTONMATCHER_LEARNED_DET", "1").strip() not in (
        "0", "false", "False", "")


def layout_enabled() -> bool:
    """May the model's boxes replace the Gemini-led layout?  Needs the model
    on too: with no reading there is no layout."""
    return enabled() and os.environ.get(
        "BUTTONMATCHER_LEARNED_LAYOUT", "1").strip() not in ("0", "false", "False", "")


def dense_layout_enabled() -> bool:
    """Rollout step 3: may the model cut the crops on every dense lot, not only
    where Hough collapsed?  Kill switch BUTTONMATCHER_LEARNED_LAYOUT_DENSE=0
    (step 2 stays on); BUTTONMATCHER_LEARNED_LAYOUT=0 turns both off."""
    return layout_enabled() and os.environ.get(
        "BUTTONMATCHER_LEARNED_LAYOUT_DENSE", "1").strip() not in (
        "0", "false", "False", "")


def small_layout_enabled() -> bool:
    """Rollout step 4: may the model cut the crops on small lots (1-6) too?
    Kill switch BUTTONMATCHER_LEARNED_LAYOUT_SMALL=0 (steps 2 and 3 stay on);
    BUTTONMATCHER_LEARNED_LAYOUT=0 turns all three off."""
    return layout_enabled() and os.environ.get(
        "BUTTONMATCHER_LEARNED_LAYOUT_SMALL", "1").strip() not in (
        "0", "false", "False", "")


def count_conf() -> float:
    try:
        return float(os.environ.get("BUTTONMATCHER_LEARNED_DET_CONF", DEFAULT_CONF))
    except (TypeError, ValueError):
        return DEFAULT_CONF


# --- maths (pure) -----------------------------------------------------------------

def letterbox(img, size=IMGSZ):
    """Resize keeping aspect, pad to size x size with grey 114 -- as Ultralytics.

    Returns (tensor [1,3,size,size] float32 RGB 0..1, scale, (pad_x, pad_y)).
    """
    import numpy as np
    h, w = img.shape[:2]
    r = min(size / h, size / w)
    nw, nh = int(round(w * r)), int(round(h * r))
    import cv2  # local: keeps the maths importable where cv2 is absent
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    px, py = (size - nw) // 2, (size - nh) // 2
    canvas[py:py + nh, px:px + nw] = resized
    x = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    return np.ascontiguousarray(x), r, (px, py)


def nms(boxes, scores, iou=NMS_IOU):
    """Greedy NMS; boxes [N,4] xyxy.  Returns kept indices, best score first."""
    import numpy as np
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    area = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = np.argsort(-scores)
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        if len(keep) >= MAX_DET:
            break
        rest = order[1:]
        ix1 = np.maximum(x1[i], x1[rest])
        iy1 = np.maximum(y1[i], y1[rest])
        ix2 = np.minimum(x2[i], x2[rest])
        iy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0, ix2 - ix1) * np.maximum(0, iy2 - iy1)
        union = area[i] + area[rest] - inter
        ov = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
        order = rest[ov <= iou]
    return keep


def decode(raw, scale, pad, img_w, img_h, floor=FLOOR_CONF):
    """YOLO11 single-class head output -> [[x1,y1,x2,y2,score], ...] in image px.

    ``raw`` is the ONNX output [1, 5, A] (cx, cy, w, h, score per anchor) in
    letterboxed coordinates.  Boxes under ``floor`` are discarded before NMS.
    """
    import numpy as np
    p = np.asarray(raw)[0]
    if p.shape[0] != 5 and p.shape[-1] == 5:
        p = p.T
    scores = p[4]
    m = scores >= floor
    if not m.any():
        return []
    cx, cy, w, h = p[0][m], p[1][m], p[2][m], p[3][m]
    s = scores[m]
    px, py = pad
    boxes = np.stack([(cx - w / 2 - px) / scale, (cy - h / 2 - py) / scale,
                      (cx + w / 2 - px) / scale, (cy + h / 2 - py) / scale], 1)
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, img_w)
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, img_h)
    keep = nms(boxes, s)
    return [[round(float(v), 1) for v in boxes[k]] + [round(float(s[k]), 4)]
            for k in keep]


def _cap(t):
    """Share of a disc's area beyond a chord ``t`` diameters in from its edge."""
    d = 0.5 - min(max(t, 0.0), 1.0)          # chord to centre, in diameters
    return (0.25 * math.acos(d / 0.5) - d * math.sqrt(max(0.25 - d * d, 0.0))) / (
        math.pi * 0.25)


def in_frame_fraction(box, img_w, img_h):
    """Estimated share of a round button inside the photo, from its box.

    A box the border cut is short on that axis; its other side is the button's
    diameter, and the missing cap follows from the shortfall.  A corner cut
    hides the diameter on both axes, so it is estimated from the longer side
    and errs toward counting.  A box clear of the border is 1.0.
    """
    x1, y1, x2, y2 = box[:4]
    bw, bh = x2 - x1, y2 - y1
    if bw <= 0 or bh <= 0:
        return 0.0
    dia = max(bw, bh)
    missing = 0.0
    if x1 <= EDGE_PX or x2 >= img_w - EDGE_PX:
        missing += _cap(1 - bw / dia)
    if y1 <= EDGE_PX or y2 >= img_h - EDGE_PX:
        missing += _cap(1 - bh / dia)
    return max(0.0, 1.0 - missing)


def count_at(boxes, conf, img_w=None, img_h=None):
    """Boxes at ``conf`` or better; given the image size, only those at least
    ``MIN_IN_FRAME`` inside it."""
    return sum(1 for b in boxes if b[4] >= conf and (
        img_w is None or in_frame_fraction(b, img_w, img_h) >= MIN_IN_FRAME))


# --- model ------------------------------------------------------------------------

def _load(fetch):
    """Build the session once per container.  ``fetch(dest) -> generation|None``
    downloads the model blob to ``dest``; None means there is no model."""
    with _lock:
        if _state["session"] is not None:
            return _state["session"]
        if (_state["failed_at"] is not None
                and time.time() - _state["failed_at"] < RETRY_AFTER_S):
            return None
        try:
            import tempfile
            import onnxruntime as ort
            dest = os.path.join(tempfile.gettempdir(), "button_yolo.onnx")
            gen = fetch(dest)
            if gen is None:
                raise FileNotFoundError(f"no model at {MODEL_BLOB}")
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1   # a shadow: never compete with CLIP
            sess = ort.InferenceSession(dest, opts,
                                        providers=["CPUExecutionProvider"])
            _state.update(session=sess, input=sess.get_inputs()[0].name,
                          generation=str(gen), failed_at=None, error=None)
            print(f">>> LEARNED_DET: model loaded (generation {gen}).", flush=True)
            return sess
        except Exception as e:
            _state.update(failed_at=time.time(), error=f"{type(e).__name__}: {e}")
            print(f">>> LEARNED_DET: model unavailable -- shadow off "
                  f"({_state['error']}).", flush=True)
            return None


def shadow_count(img, fetch):
    """The shadow reading for one detection-space BGR image, or None when off.

    Never raises.  Returns {model_generation, conf, count, boxes, ms} or
    {error} -- the caller stores it as-is in the label sidecar.
    """
    if not enabled() or img is None:
        return None
    sess = _load(fetch)
    if sess is None:
        return None
    try:
        t0 = time.time()
        h, w = img.shape[:2]
        x, scale, pad = letterbox(img)
        raw = sess.run(None, {_state["input"]: x})[0]
        boxes = decode(raw, scale, pad, w, h)
        conf = count_conf()
        return {"model_generation": _state["generation"], "conf": conf,
                "count": count_at(boxes, conf, w, h), "boxes": boxes,
                "min_in_frame": MIN_IN_FRAME,
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# --- the learned layout (rollout step 2) --------------------------------------------

LAYOUT_SOURCE = "learned"


NESTED_FRAC = 0.5         # a box centred this many radii from a stronger one
                          # is the same button


def drop_nested(boxes, frac=NESTED_FRAC):
    """Drop a box whose centre sits within ``frac`` radii of a stronger box's
    centre: two boxes on one button.  NMS merges boxes only above IoU 0.7, and
    a weak box inside a strong one can overlap less -- the 1984 Mellon fixture
    (dense_mellon_11) boxed *Wild Irish Woes* twice, 0.86 and 0.42, centres a
    quarter radius apart, IoU 0.69.  Two different round buttons sit about two
    radii apart, so this never merges real neighbours.  Strongest first."""
    kept = []
    for b in sorted(boxes, key=lambda b: -b[4]):
        cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
        if all(math.hypot(cx - (k[0] + k[2]) / 2.0, cy - (k[1] + k[3]) / 2.0)
               > frac * max(k[2] - k[0], k[3] - k[1]) / 2.0 for k in kept):
            kept.append(b)
    return kept


def layout_boxes(reading, img_w, img_h):
    """The boxes ``count_at`` would count in ``reading``: score at
    ``count_conf()`` or better and at least ``MIN_IN_FRAME`` inside the photo,
    less any box nested on a stronger one (``drop_nested``).  An absent,
    errored or malformed reading has none."""
    if not isinstance(reading, dict) or reading.get("error"):
        return []
    conf = count_conf()
    out = []
    for b in reading.get("boxes") or []:
        try:
            if len(b) >= 5 and b[4] >= conf and \
                    in_frame_fraction(b, img_w, img_h) >= MIN_IN_FRAME:
                out.append(b)
        except (TypeError, ValueError):
            continue
    return drop_nested(out)


GRID_MIN_BOXES = 3        # a lattice needs this many counted boxes to read
GRID_ALIGN_FRAC = 0.5     # on a row / column: centre within this many radii
GRID_SIZE_RATIO = 1.4     # a fill box is at most this much bigger/smaller


def grid_fill_enabled() -> bool:
    """Slash lots promote weaker boxes that sit on the lattice when the model
    counts fewer buttons than were typed (``grid_fill``).  Off with
    BUTTONMATCHER_LEARNED_GRID_FILL=0."""
    return os.environ.get("BUTTONMATCHER_LEARNED_GRID_FILL", "1").strip() != "0"


def _geom(b):
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0, max(b[2] - b[0], b[3] - b[1]) / 2.0)


def grid_fill(reading, img_w, img_h, target):
    """Weaker boxes to add when the model counts fewer than ``target`` buttons.

    The learned-detector form of Hough's grid-hole fill (2026-07-18): on
    2026-10-12 a 12-button /inventory remove (a 3 x 4 frame) counted 11, the
    top-right button missed.  A box under the live threshold (down to
    ``FLOOR_CONF``, which every reading keeps) is promoted when all hold:

    - the counted boxes (``layout_boxes``) number at least ``GRID_MIN_BOXES``
      and fewer than ``target`` (the count the operator typed);
    - it is at least ``MIN_IN_FRAME`` inside the photo;
    - its radius is within ``GRID_SIZE_RATIO`` of the counted boxes' median;
    - it sits on the lattice: its centre is within ``GRID_ALIGN_FRAC`` median
      radii of a counted box's row (y) AND of a counted box's column (x);
    - it is not on a counted box (centres more than one median radius apart)
      or on another promoted box.

    Strongest first, at most ``target`` minus the count.  Alignment is read
    against the counted boxes only, so promoted boxes never chain off each
    other.  Returns the boxes as stored ([x1, y1, x2, y2, score]).
    """
    if not isinstance(reading, dict) or reading.get("error") or not target:
        return []
    kept = layout_boxes(reading, img_w, img_h)
    deficit = int(target) - len(kept)
    if len(kept) < GRID_MIN_BOXES or deficit <= 0:
        return []
    geo = [_geom(b) for b in kept]
    radii = sorted(g[2] for g in geo)
    med_r = radii[len(radii) // 2]
    if med_r <= 0:
        return []
    tol = GRID_ALIGN_FRAC * med_r
    conf = count_conf()
    cands = []
    for b in reading.get("boxes") or []:
        try:
            if len(b) < 5 or not (FLOOR_CONF <= b[4] < conf):
                continue
            if in_frame_fraction(b, img_w, img_h) < MIN_IN_FRAME:
                continue
            cx, cy, r = _geom(b)
        except (TypeError, ValueError):
            continue
        if r <= 0 or not (1 / GRID_SIZE_RATIO <= r / med_r <= GRID_SIZE_RATIO):
            continue
        if not (any(abs(cy - g[1]) <= tol for g in geo)
                and any(abs(cx - g[0]) <= tol for g in geo)):
            continue
        cands.append(b)
    out, taken = [], list(geo)
    for b in sorted(cands, key=lambda b: -b[4]):
        cx, cy, _r = _geom(b)
        if all(math.hypot(cx - g[0], cy - g[1]) > med_r for g in taken):
            out.append(b)
            taken.append(_geom(b))
            if len(out) >= deficit:
                break
    return out


def is_dense(reading, img_w, img_h):
    """Does the model count ``DENSE_MIN`` or more buttons (``count_at``'s rule)?"""
    return len(layout_boxes(reading, img_w, img_h)) >= DENSE_MIN


def is_small(reading, img_w, img_h):
    """Does the model count at least one button but fewer than ``DENSE_MIN``?
    Zero is not small: with no qualifying box the lot keeps Hough's crops."""
    return 1 <= len(layout_boxes(reading, img_w, img_h)) < DENSE_MIN


def learned_crops(reading, image_bgr, img_w, img_h, target=None):
    """One crop per counted box, cut the way ``detect.gemini_led_crops`` cuts.

    Centre = box centre, radius = half the longer side (a box the border clipped
    keeps its uncut diameter), padded by ``gemini_geometry.synth_box``.
    Returns ``(crops, circle_info)`` as parallel lists; each circle_info entry
    is ``{"shape": "circle", "x", "y", "r", "source": "learned", "conf"}``.
    Empty lists when no box qualifies -- the caller then keeps today's layout.

    ``target`` (the typed count, slash lots only) adds ``grid_fill``'s boxes
    when its switch is on; their entries carry ``"grid_fill": True``.
    """
    # Package-relative in ebayscout, flat here; one file for both (2026-10-15).
    try:
        from . import gemini_geometry as ggeo
    except ImportError:
        import gemini_geometry as ggeo
    crops, circle_info = [], []
    if image_bgr is None:
        return crops, circle_info
    fill = (grid_fill(reading, img_w, img_h, target)
            if target and grid_fill_enabled() else [])
    for b in layout_boxes(reading, img_w, img_h) + fill:
        x1, y1, x2, y2, score = b[:5]
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        r = max(x2 - x1, y2 - y1) / 2.0
        if r <= 0:
            continue
        bx1, by1, bx2, by2 = ggeo.synth_box(cx, cy, r, img_w, img_h)
        crop = image_bgr[int(by1):int(by2), int(bx1):int(bx2)]
        if crop is None or crop.size == 0:
            continue
        crops.append(crop)
        circle_info.append({
            "shape": "circle", "x": int(round(cx)), "y": int(round(cy)),
            "r": int(round(r)), "source": LAYOUT_SOURCE,
            "conf": round(float(score), 4),
        })
        if any(b is f for f in fill):
            circle_info[-1]["grid_fill"] = True
    return crops, circle_info


def restrict_reconcile(n_crops, crop_to_slogan, telemetry, gemini_slogans):
    """Keep reconcile's associations on the learned layout, and nothing else.

    ``reconcile_with_gemini`` ran on the ``n_crops`` learned circles.  It may
    have dropped some as phantoms and recovered crops at Gemini points; on this
    layout neither happens -- the model's set is the crop set (operator
    2026-10-02: the model is not punished for Gemini's misreads).  Its indices
    are post-drop, recovered crops last, so they are mapped back onto the
    learned order here.

    Returns ``(crop_to_slogan, unmatched_crop_indices, n_gemini_uncropped)``:
    associations keyed by learned index; the learned circles no Gemini point
    backs (a crop reconcile would have dropped had no point either); and how
    many of Gemini's localized points ended with no crop, for the banner.
    """
    telemetry = telemetry or {}
    dropped = set(telemetry.get("dropped_crop_indices") or [])
    kept = [i for i in range(n_crops) if i not in dropped]
    out = {}
    for j, assoc in (crop_to_slogan or {}).items():
        if isinstance(j, int) and 0 <= j < len(kept):
            out[kept[j]] = assoc
    um = telemetry.get("unmatched_crop_indices")
    unmatched = None
    if um is not None:
        unmatched = sorted({kept[j] for j in um if 0 <= j < len(kept)} | dropped)
    localized = sum(1 for s in (gemini_slogans or [])
                    if isinstance(s, dict)
                    and s.get("x") is not None and s.get("y") is not None)
    cropped = {a.get("gemini_idx") for a in out.values()
               if a.get("gemini_idx") is not None}
    return out, unmatched, max(0, localized - len(cropped))


# --- placement: the model's boxes against the crops that were cut ------------------
#
# Step 4 (small lots) asks whether the model's BOXES, not just its count, can
# replace Hough's circles.  A count can be right with a box on the wrong thing,
# so each crop is paired with the model box that sits on it, and the box's
# score rides that crop's match_log row.  Joined to the operator's answer for
# the crop (confirm_log), every pipeline lot then says, per threshold: which
# phantoms the model would have dropped, which real buttons it would have lost,
# and which of its boxes no crop covered (a button Hough missed, or a model
# false positive).  tools/detector/grade_placement.py reads it.

PLACE_MAX_FRAC = 1.0      # a box centre within this many crop radii is "on" it;
                          # recorded loosely so grading can re-sweep tighter


def _centre_radius(c):
    """(cx, cy, r) of a crop -- a circle_info entry or a label-sidecar circle,
    circle or rect -- or None when it carries no usable geometry."""
    if not isinstance(c, dict):
        return None
    x, y, r = c.get("x", c.get("cx")), c.get("y", c.get("cy")), c.get("r")
    bbox = [c.get(k) for k in ("x1", "y1", "x2", "y2")]
    try:
        if (x is None or y is None) and None not in bbox:
            x, y = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
        if r is None and None not in bbox:
            r = max(bbox[2] - bbox[0], bbox[3] - bbox[1]) / 2.0
        x, y, r = float(x), float(y), float(r)
    except (TypeError, ValueError):
        return None
    return (x, y, r) if r > 0 else None


def crop_placement(reading, crops, img_w, img_h, max_frac=PLACE_MAX_FRAC):
    """Pair the model's boxes with the crops, one to one, nearest first.

    Every stored box counts (down to ``FLOOR_CONF``), whatever the live
    threshold, so a grader can re-sweep it.  Returns None for an absent or
    errored reading, else::

        {"per_crop": [{"conf", "dist", "in_frame"} | None, ...],   # crop order
         "extra":    [[cx, cy, r, score, in_frame], ...]}          # no crop

    ``dist`` is the centre distance in crop radii.  ``None`` for a crop: no
    box within ``max_frac`` radii -- the model saw no button there.
    """
    if not isinstance(reading, dict) or reading.get("error"):
        return None
    boxes = []
    for b in reading.get("boxes") or []:
        try:
            if len(b) >= 5:
                boxes.append([float(v) for v in b[:5]])
        except (TypeError, ValueError):
            continue
    geo = [_centre_radius(c) for c in crops or []]
    pairs = []
    for i, g in enumerate(geo):
        if g is None:
            continue
        for j, b in enumerate(boxes):
            d = math.hypot((b[0] + b[2]) / 2.0 - g[0], (b[1] + b[3]) / 2.0 - g[1]) / g[2]
            if d <= max_frac:
                pairs.append((d, -b[4], i, j))
    pairs.sort()
    per_crop = [None] * len(geo)
    used = set()
    for d, _, i, j in pairs:
        if per_crop[i] is None and j not in used:
            used.add(j)
            per_crop[i] = {"conf": round(boxes[j][4], 4), "dist": round(d, 3),
                           "in_frame": round(in_frame_fraction(boxes[j], img_w, img_h), 3)}
    extra = []
    for j, b in enumerate(boxes):
        if j in used:
            continue
        extra.append([round((b[0] + b[2]) / 2.0, 1), round((b[1] + b[3]) / 2.0, 1),
                      round(max(b[2] - b[0], b[3] - b[1]) / 2.0, 1), round(b[4], 4),
                      round(in_frame_fraction(b, img_w, img_h), 3)])
    return {"per_crop": per_crop, "extra": extra}


def lot_record(reading, placement, img_w, img_h, layout):
    """The lot-level match_log blob: the reading's identity and live rule, the
    layout that cut the crops, and the boxes no crop covered.  None for an
    absent or errored reading."""
    if not isinstance(reading, dict) or reading.get("error") or placement is None:
        return None
    return {"gen": reading.get("model_generation"), "conf": count_conf(),
            "count": len(layout_boxes(reading, img_w, img_h)),
            "min_in_frame": MIN_IN_FRAME, "dense_min": DENSE_MIN,
            "max_frac": PLACE_MAX_FRAC, "layout": layout,
            "n_boxes": len(reading.get("boxes") or []),
            "extra": placement["extra"]}


# --- step 3 in shadow (rollout step 3, record-only) --------------------------------

DENSE_MIN = 7             # "dense" as the E2 gate and grade_shadow read it
STEP3_MARGIN = 2          # counts this far apart are a change worth making


def step3_shadow(reading, img_w, img_h, n_crops, layout):
    """The model's count against Hough's on this lot, for the record.

    Rollout step 3 (live since 2026-10-03) lets the model cut the crops on a
    dense lot.  This records, per lot, the model's count (``count_at``, the
    live rule) beside the crops Hough cut (``n_crops``, with Hough's
    ``layout``), and ``would_switch``: the model counts 7 or more, Hough's
    layout was not already the model's, and the counts differ by at least
    ``STEP3_MARGIN`` -- the lots where step 3 changes the outcome.  The table
    lot of 2026-10-02 (model 140, Hough 88) is one.  Recorded only.

    Returns ``None`` for an absent or errored reading, else
    ``{learned, crops, delta, dense, layout, margin, would_switch}``.
    ``tools/detector/grade_shadow.py`` scores those lots against truth.
    """
    if not isinstance(reading, dict) or reading.get("error"):
        return None
    try:
        learned = count_at(reading.get("boxes") or [], count_conf(), img_w, img_h)
        n_crops = int(n_crops)
    except (TypeError, ValueError, IndexError):
        return None
    dense = learned >= DENSE_MIN
    delta = learned - n_crops
    return {"learned": learned, "crops": n_crops, "delta": delta,
            "dense": dense, "layout": layout, "margin": STEP3_MARGIN,
            "would_switch": bool(dense and layout != LAYOUT_SOURCE
                                 and abs(delta) >= STEP3_MARGIN)}
