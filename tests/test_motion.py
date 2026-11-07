import numpy as np

from coeus.vision.motion import MotionDetector, ZoneSpec, build_masks, zone_bbox

LEFT = ZoneSpec("left", [[0, 0], [0.5, 0], [0.5, 1], [0, 1]], min_ratio=0.01)


def frames_with_square(x, n=15, size=(240, 320)):
    bg = np.full((*size, 3), 90, np.uint8)
    out = [bg.copy() for _ in range(n)]
    moving = bg.copy()
    moving[80:160, x:x + 60] = 255
    return out, moving


def run(det, still, moving):
    for f in still:
        det.process(f)
    hits = []
    for _ in range(3):
        hits = det.process(moving)
    return hits


def test_motion_inside_zone_detected():
    still, moving = frames_with_square(40)
    hits = run(MotionDetector([LEFT]), still, moving)
    assert [h.zone for h in hits] == ["left"]
    x, y, w, h = hits[0].bbox
    assert 30 <= x <= 45 and w >= 55


def test_motion_outside_zone_ignored():
    still, moving = frames_with_square(220)
    assert run(MotionDetector([LEFT]), still, moving) == []


def test_exclude_zone_cuts_include():
    excl = ZoneSpec("tree", [[0, 0], [0.5, 0], [0.5, 1], [0, 1]], kind="exclude")
    still, moving = frames_with_square(40)
    assert run(MotionDetector([excl]), still, moving) == []  # full frame minus excluded half


def test_masks_and_bbox():
    masks = build_masks((100, 200), [])
    assert masks[0][0].name == "full" and masks[0][1].all()
    assert zone_bbox((100, 200), LEFT, pad=0) == (0, 0, 100, 100)
