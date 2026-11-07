import numpy as np
import pytest

from coeus.vision.faces import DetectedFace, Gallery, enroll_embedding, normalise


def vec(seed):
    return normalise(np.random.default_rng(seed).normal(size=128).astype(np.float32))


def test_gallery_match_and_unknown():
    g = Gallery()
    a, b = vec(1), vec(2)
    g.load([(1, "Akash", a.tobytes()), (2, "Riya", b.tobytes())])
    noisy = normalise(a + 0.2 * vec(3))
    m = g.match(noisy)
    assert m.known and m.name == "Akash" and m.score > 0.8
    assert not g.match(vec(99)).known
    assert not Gallery().match(a).known


class FakeBackend:
    def __init__(self, boxes):
        self.boxes = boxes

    def detect(self, img):
        return [DetectedFace(b, 0.9, np.zeros(15)) for b in self.boxes]

    def embed(self, img, face):
        return vec(face.bbox[0])


def test_enroll_rules():
    img = np.zeros((10, 10, 3), np.uint8)
    with pytest.raises(ValueError, match="no face"):
        enroll_embedding(FakeBackend([]), img)
    with pytest.raises(ValueError, match="more than one"):
        enroll_embedding(FakeBackend([(0, 0, 50, 50), (60, 0, 48, 48)]), img)
    # a small background face next to a big one is fine
    emb = enroll_embedding(FakeBackend([(1, 0, 10, 10), (5, 0, 100, 100)]), img)
    assert np.allclose(emb, vec(5))
