"""Pure confidence-gate logic for auto-detection rollout.

Decides, from the unguided detector's diagnostics, whether the bot should:
    "auto"     proceed with the detected count/grid (one-tap override offered)
    "suggest"  show the count prompt prefilled with the detected count
    "manual"   show today's count prompt unchanged

Thresholds are seeds pending calibration via tools/calibrate_from_logs.py
(sweep on match_log: det_user_count as truth vs ni_selected/ni_confidence/
ni_layout_conf, pick the knee at >= 95% count-exact precision).

No cv2/numpy/torch imports — unit-tested with plain values.
"""

GATE_AUTO_CONFIDENCE = 0.70     # _score_solution composite for the winning set
GATE_AUTO_LAYOUT = 0.85         # fraction of circles fitting the inferred grid
GATE_SUGGEST_CONFIDENCE = 0.55

GATE_AUTO = "auto"
GATE_SUGGEST = "suggest"
GATE_MANUAL = "manual"


def grid_is_consistent(selected, est_rows, est_cols):
    """Mirror of get_valid_counts: a rows×cols grid may be missing at most the
    tail of its last row, so a plausible count n satisfies
        rows*cols - (cols-1) <= n <= rows*cols.
    """
    try:
        selected = int(selected)
        est_rows = int(est_rows)
        est_cols = int(est_cols)
    except (TypeError, ValueError):
        return False
    if selected < 1 or est_rows < 1 or est_cols < 1:
        return False
    total = est_rows * est_cols
    return total - (est_cols - 1) <= selected <= total


def gate_decision(*, confidence, layout_conf, selected, est_rows, est_cols):
    """Map unguided-detector diagnostics to "auto" | "suggest" | "manual"."""
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return GATE_MANUAL
    try:
        layout_conf = float(layout_conf)
    except (TypeError, ValueError):
        layout_conf = 0.0
    try:
        selected = int(selected)
    except (TypeError, ValueError):
        selected = 0

    if (
        confidence >= GATE_AUTO_CONFIDENCE
        and layout_conf >= GATE_AUTO_LAYOUT
        and selected >= 1
        and grid_is_consistent(selected, est_rows, est_cols)
    ):
        return GATE_AUTO
    if confidence >= GATE_SUGGEST_CONFIDENCE and selected >= 1:
        return GATE_SUGGEST
    return GATE_MANUAL
