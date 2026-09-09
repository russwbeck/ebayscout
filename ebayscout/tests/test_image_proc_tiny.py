"""Tiny-circle-syndrome regression for the ebayscout SCAN path (image_proc).

THE SYNDROME.  The scan sweep runs Hough across a ~4x radius range (base_r x
config.HOUGH_RADIUS_SCALES, floored at HOUGH_MIN_RADIUS_PX), so on a lot photo
it also fires on every round thing PRINTED ON a button — the bowls of o/e/g/0/9,
the Mellon/Citizens/cb logo roundel, the two-digit year — and on the half-radius
accumulator ghost inside a real rim.  Nothing downstream removed them:

  * the fill-ratio filter PASSES them at ~1.0 (a small disc inside a blue button
    is entirely "blue"), which also biases _score_solution's fill_mean term
    toward the pass that found the most of them;
  * the overlap dedup rejects on 0.7 x min(r_candidate, r_accepted) — for a
    small circle inside a big one that is 0.7 x its own tiny radius, so anything
    more than a few px off-centre survives;
  * the inner-circle rule needs the centre within 0.3 x r_big, which an
    off-centre logo or year misses;
  * scan mode skips the radius-consistency filter on purpose, so that a
    size-outlier needed button is not pruned.

Measured on the founding lots, the winning scan pass came back with radius
spreads of 3.5x-6.4x (banded_quad_4: 86 crops for 4 buttons).

THE FIXTURE is drawn in code rather than committed as a photo: deterministic
bytes, no binary in the repo, and it isolates the mechanism — blue discs on a
pale ground, each printed with white letter-bowl rings and a logo roundel, mild
blur so the rims are soft enough that the loose/standard pass wins (which is
exactly when the syndrome reaches production crops).

Requires cv2 (installs fine in web sessions); skipped if unavailable.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    import cv2
    import numpy as np
    from ebayscout import image_proc
    from ebayscout import detect_scale as dscale
    HAVE_CV2 = True
except Exception:  # pragma: no cover - env without the vision stack
    HAVE_CV2 = False

ROWS, COLS, R = 5, 4, 70
TRUTH = ROWS * COLS


def _lot_bytes():
    """A 5x4 lot of blue buttons on a pale ground, each carrying the round
    printing Hough mistakes for buttons.  Fixed seed + PNG => fixed bytes."""
    rng = np.random.RandomState(7)
    pad, step = int(R * 1.35), int(2 * R * 1.15)
    h = pad * 2 + step * (ROWS - 1) + 2 * R
    w = pad * 2 + step * (COLS - 1) + 2 * R
    img = np.full((h, w, 3), (140, 120, 110), np.uint8)
    img = np.clip(img.astype(np.int16) + rng.randint(-10, 11, img.shape),
                  0, 255).astype(np.uint8)
    centres = []
    for r in range(ROWS):
        for c in range(COLS):
            cx, cy = pad + R + step * c, pad + R + step * r
            cv2.circle(img, (cx, cy), R, (170, 60, 30), -1)
            cv2.circle(img, (cx, cy), R, (185, 75, 45), 2)
            # letter bowls, then the bank-logo roundel low on the face
            for dx, dy, rr in [(-28, -22, 11), (2, -22, 11), (30, -22, 11),
                               (-30, 26, 9), (0, 26, 9), (-34, 48, 16)]:
                cv2.circle(img, (cx + dx, cy + dy), rr, (255, 255, 255), 2)
            centres.append((cx, cy))
    img = cv2.GaussianBlur(img, (9, 9), 0)
    return cv2.imencode(".png", img)[1].tobytes(), centres


def _scan(guard):
    prev = os.environ.get("EBAYSCOUT_TINY_GUARD")
    os.environ["EBAYSCOUT_TINY_GUARD"] = guard
    try:
        data, centres = _lot_bytes()
        crops, diag = image_proc.detect_and_crop(data, return_diag=True)
    finally:
        if prev is None:
            os.environ.pop("EBAYSCOUT_TINY_GUARD", None)
        else:
            os.environ["EBAYSCOUT_TINY_GUARD"] = prev
    return crops, diag, centres


def test_fixture_still_reproduces_the_syndrome():
    """Guard OFF must still show the disease, or this fixture has stopped
    testing anything and needs re-tuning rather than deleting."""
    if not HAVE_CV2:
        print("SKIP test_fixture_still_reproduces_the_syndrome (no cv2)")
        return
    crops, diag, _c = _scan("0")
    assert len(crops) > TRUTH, (
        f"guard off no longer over-counts ({len(crops)} of {TRUTH}) — "
        f"re-tune the fixture, don't delete the test")
    r_min, r_max = diag["radius_min"], diag["radius_max"]
    assert r_max / r_min >= 2.0, (
        f"guard off no longer returns a split radius population "
        f"({r_min}..{r_max}) — the syndrome signature is a wide spread")


def test_guard_drops_the_printing_and_keeps_every_button():
    """Guard ON (the default): exactly the buttons, one tight radius band."""
    if not HAVE_CV2:
        print("SKIP test_guard_drops_the_printing_and_keeps_every_button (no cv2)")
        return
    crops, diag, _c = _scan("1")
    assert len(crops) == TRUTH, f"got {len(crops)} crops, want {TRUTH}"
    r_min, r_max = diag["radius_min"], diag["radius_max"]
    assert r_max / r_min <= 1.6, (
        f"radius spread {r_min}..{r_max} — sub-button circles still in the crops")
    # Nothing the guard keeps may be printing on something else it keeps.
    assert r_min >= R * dscale.BAND_LO, (
        f"smallest kept radius {r_min} is below the cohort band floor")


def test_default_is_guarded():
    """No env var set => guard on.  The kill switch is opt-out, not opt-in."""
    if not HAVE_CV2:
        print("SKIP test_default_is_guarded (no cv2)")
        return
    prev = os.environ.pop("EBAYSCOUT_TINY_GUARD", None)
    try:
        assert image_proc._tiny_guard_enabled()
        data, _c = _lot_bytes()
        crops, _d = image_proc.detect_and_crop(data, return_diag=True)
        assert len(crops) == TRUTH, f"default run gave {len(crops)} crops"
    finally:
        if prev is not None:
            os.environ["EBAYSCOUT_TINY_GUARD"] = prev


def test_drop_subfeatures_is_purely_subtractive():
    """The guard may only remove circles, never add or move one — it runs after
    pass selection precisely so it cannot change which circles were found."""
    if not HAVE_CV2:
        print("SKIP test_drop_subfeatures_is_purely_subtractive (no cv2)")
        return
    circles = [(200, 200, 100), (500, 200, 100), (200, 500, 100),
               (500, 500, 100), (228, 178, 12), (500, 232, 14)]
    kept, dropped = image_proc._drop_subfeatures(circles)
    assert dropped == 2
    assert set(kept) <= set(circles)
    assert len(kept) == 4


def test_an_off_band_phantom_may_not_swallow_real_buttons():
    """The ordering hazard the two stages create, and why they iterate.

    Containment runs before the band, so a giant phantom — a glare ring
    spanning several buttons — can be accepted as an anchor (it is the largest
    circle, so it goes first), swallow every real button whose centre falls
    inside it, and only THEN be deleted by the band.  Net effect: those buttons
    are lost and nothing is gained.  Measured on case1_wood_glare_37 before the
    fix: a 55px phantom swallowed three 24px circles sitting at the dominant
    radius (r*=29).

    The guard therefore iterates — off-band circles leave the pool and
    containment is redone without them — so anything a phantom was hiding comes
    back.  Here: a 12-button grid plus one giant covering four of them.
    """
    if not HAVE_CV2:
        print("SKIP test_an_off_band_phantom_may_not_swallow_real_buttons (no cv2)")
        return
    step = 220
    buttons = [(200 + step * c, 200 + step * r, 100)
               for r in range(3) for c in range(4)]
    giant = (200 + step // 2, 200 + step // 2, 250)   # covers the 2x2 block
    kept, dropped = image_proc._drop_subfeatures([giant] + buttons)
    assert giant not in kept, "the off-band giant survived"
    missing = [b for b in buttons if b not in kept]
    assert not missing, (
        f"the giant swallowed {len(missing)} real button(s) before the band "
        f"removed it: {missing}")
    assert dropped == 1 and len(kept) == len(buttons)


def test_kill_switch_is_a_no_op_path():
    if not HAVE_CV2:
        print("SKIP test_kill_switch_is_a_no_op_path (no cv2)")
        return
    circles = [(200, 200, 100), (228, 178, 12)]
    prev = os.environ.get("EBAYSCOUT_TINY_GUARD")
    os.environ["EBAYSCOUT_TINY_GUARD"] = "0"
    try:
        kept, dropped = image_proc._drop_subfeatures(circles)
    finally:
        if prev is None:
            os.environ.pop("EBAYSCOUT_TINY_GUARD", None)
        else:
            os.environ["EBAYSCOUT_TINY_GUARD"] = prev
    assert dropped == 0 and kept == circles


if __name__ == "__main__":
    if not HAVE_CV2:
        print("cv2 not available; nothing to run")
        sys.exit(0)
    test_fixture_still_reproduces_the_syndrome()
    test_guard_drops_the_printing_and_keeps_every_button()
    test_default_is_guarded()
    test_drop_subfeatures_is_purely_subtractive()
    test_an_off_band_phantom_may_not_swallow_real_buttons()
    test_kill_switch_is_a_no_op_path()
    print("all tiny-circle scan-path tests passed")
