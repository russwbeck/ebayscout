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


def demote_auto_on_detector_bailout(gate, detector_used):
    """Close the Logger_10 trust-gate loophole: an unguided pass that collapses
    to a single circle self-certifies (scale_conf carries the 0.6 sentinel and
    layout_conf is trivially 1.0 at n=1) — two 66/69-button lots gated "auto"
    this way. On both, the GUIDED detector had already bailed to the projection
    grid, and that bailout is the reliable tell (Logger_10: demotes 2/2
    collapses while keeping 40/43 genuine singles; dt-peak corroboration was
    tested and failed — the fused mask fools it too, 0/2). Callers apply this
    where both facts meet — after the guided pass — since gate_decision runs
    inside the unguided detector, which cannot see the guided outcome.

    AUTO survives only when the guided detector actually engaged
    (detector_used starts with "hough": "hough" / "hough+blob").
    """
    if gate == GATE_AUTO and not str(detector_used or "").startswith("hough"):
        print(f">>> GATE: auto demoted to suggest — guided detector bailed "
              f"(detector_used={detector_used!r}).", flush=True)
        return GATE_SUGGEST
    return gate


def gate_decision(*, confidence, layout_conf, selected, est_rows, est_cols,
                  scale_path=None):
    """Map unguided-detector diagnostics to "auto" | "suggest" | "manual".

    ``scale_path`` (optional): which radius estimator produced the winning set —
    "scale_first" | "scale_second_chance" | "sweep_fallback".  Logger_4
    (2026-07-02, 265 graded lots) split gate=auto accuracy cleanly on this:
    scale_first autos were 40/40 exact (100%) while ALL six wrong autos came
    from the fallback paths (sweep_fallback 76% exact, the one
    scale_second_chance auto was wrong).  So AUTO now additionally requires
    scale_first; fallback-path detections cap at SUGGEST.  Callers that don't
    pass it (None) keep the old behavior.
    """
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
        and (scale_path is None or scale_path == "scale_first")
    ):
        return GATE_AUTO
    if confidence >= GATE_SUGGEST_CONFIDENCE and selected >= 1:
        return GATE_SUGGEST
    return GATE_MANUAL
