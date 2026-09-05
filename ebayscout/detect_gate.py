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


#: Trust order, least to most.  A detector variant may not trade a gate down
#: for a better score — Logger_4's finding is that a fallback-path AUTO is
#: wrong 24% of the time, so a marginally higher confidence reached off
#: scale_first is worth less than the gate it costs.
_GATE_RANK = {GATE_MANUAL: 0, GATE_SUGGEST: 1, GATE_AUTO: 2}


def gate_rank(gate):
    """Rank a gate for comparison; unknown values sort lowest."""
    return _GATE_RANK.get(gate, 0)


def variant_is_improvement(old_gate, old_confidence, new_gate, new_confidence,
                           old_scale_path=None, new_scale_path=None):
    """Whether a re-run's result should replace the original.

    A better gate always wins.  At the same gate, a better score wins.  Trust is
    never traded away for score, on either axis Logger_4 identified:

      * a worse GATE never wins, however much better it scores — the fixture
        battery caught the colour-cast retry trading pure_blue_23's scale_first
        AUTO (0.942) for a sweep_fallback SUGGEST (0.950) at an identical count;
      * losing SCALE_FIRST never wins at an equal gate — case5_frame_display_13
        swapped scale_first for sweep_fallback at the same count of 13, giving
        up the estimator that graded 40/40 exact for nothing.

    Scale paths are optional; callers that omit them keep the gate-only rule.
    """
    old_rank, new_rank = gate_rank(old_gate), gate_rank(new_gate)
    if new_rank != old_rank:
        return new_rank > old_rank

    if (old_scale_path is not None and new_scale_path is not None
            and old_scale_path == "scale_first"
            and new_scale_path != "scale_first"):
        return False

    try:
        return float(new_confidence) > float(old_confidence)
    except (TypeError, ValueError):
        return False


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


def cap_auto_on_corrected_frame(gate):
    """A count read off a synthetically corrected frame may not reach AUTO.

    AUTO's evidence base (Logger_4: scale_first autos 40/40 exact) was built on
    unmodified photos, and the colour-cast fixtures show why it does not carry
    over: of the four cast lots the retry rescues, one (cast_mellon98_12) lands
    on 11 of 12 because the white button on white paper never enters the blue
    mask, and 11 in a 3x4 grid is indistinguishable from a legitimately short
    last row — so grid_is_consistent passes it and the count self-certifies.
    Auto-proceeding there crops 11 buttons and drops the twelfth silently,
    which is worse than the manual gate the photo used to get.

    Capping at SUGGEST keeps the whole benefit — the operator gets the count
    prefilled instead of typing it — without betting a silent drop on it.
    Mirrors the existing rule that fallback-path radii cap at SUGGEST.
    """
    if gate == GATE_AUTO:
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
