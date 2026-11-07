import numpy as np

from coeus.vision.faces import DetectedFace, Gallery, normalise
from coeus.vision.monitor import CameraConfig, CameraMonitor
from coeus.vision.motion import MotionDetector, ZoneSpec

EMB = normalise(np.random.default_rng(1).normal(size=128).astype(np.float32))


class Backend:
    def __init__(self, faces):
        self.faces = faces

    def detect(self, img):
        return [DetectedFace((10, 10, 40, 40), 0.9, np.zeros(15)) for _ in range(self.faces)]

    def embed(self, img, face):
        return EMB


def monitor(faces, mode="local"):
    gallery = Gallery()
    gallery.load([(7, "Akash", EMB.tobytes())])
    got = []
    cfg = CameraConfig(1, "Main Gate", "rtsp://x", [ZoneSpec("gate", [[0, 0], [1, 0], [1, 1], [0, 1]])],
                       face_mode=mode, cooldown_s=0)
    mon = CameraMonitor(cfg, gallery, lambda: Backend(faces), got.append, lambda: 0.363,
                        dedupe_seconds=60)
    return mon, got


def feed(mon):
    det = MotionDetector(mon.cfg.zones)
    bg = np.full((240, 320, 3), 90, np.uint8)
    for _ in range(12):
        mon.process_frame(det, bg)
    moving = bg.copy()
    moving[60:200, 100:200] = 255
    out = []
    for _ in range(4):
        out += mon.process_frame(det, moving)
    return out


def test_known_face_event_and_dedupe():
    mon, got = monitor(faces=1)
    feed(mon)
    assert [d.event_type for d in got] == ["face_known"]  # repeats suppressed
    assert got[0].match.name == "Akash" and got[0].face_crop is not None


def test_motion_only_when_no_face():
    mon, got = monitor(faces=0)
    feed(mon)
    assert got and all(d.event_type == "motion" for d in got)


def test_face_mode_off():
    mon, got = monitor(faces=1, mode="off")
    feed(mon)
    assert got and got[0].event_type == "motion"
