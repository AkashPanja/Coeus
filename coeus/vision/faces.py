"""On-device face detection (YuNet) and recognition (SFace) with OpenCV."""
from __future__ import annotations

import logging
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

MODELS = {
    "yunet": ("face_detection_yunet_2023mar.onnx", [
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
        "face_detection_yunet_2023mar.onnx",
        "https://huggingface.co/opencv/face_detection_yunet/resolve/main/"
        "face_detection_yunet_2023mar.onnx",
    ]),
    "sface": ("face_recognition_sface_2021dec.onnx", [
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
        "face_recognition_sface_2021dec.onnx",
        "https://huggingface.co/opencv/face_recognition_sface/resolve/main/"
        "face_recognition_sface_2021dec.onnx",
    ]),
}
DEFAULT_THRESHOLD = 0.363  # SFace cosine-similarity threshold recommended by OpenCV


@dataclass
class DetectedFace:
    bbox: tuple[int, int, int, int]  # x, y, w, h
    score: float
    raw: np.ndarray  # YuNet row (bbox + 5 landmarks + score), needed for alignment


@dataclass
class Match:
    person_id: int | None
    name: str
    score: float

    @property
    def known(self) -> bool:
        return self.person_id is not None


def ensure_model(models_dir: Path, key: str) -> Path:
    fname, urls = MODELS[key]
    path = models_dir / fname
    if path.exists() and path.stat().st_size > 0:
        return path
    models_dir.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for url in urls:
        try:
            log.info("downloading %s", url)
            tmp = path.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)  # noqa: S310 - fixed https URLs
            tmp.replace(path)
            return path
        except Exception as err:  # noqa: BLE001
            last_err = err
    raise RuntimeError(f"Could not download model {fname}; place it in {models_dir} manually "
                       f"({last_err})")


class SFaceBackend:
    name = "sface"

    def __init__(self, models_dir: Path, score_threshold: float = 0.8):
        self.models_dir = models_dir
        self.score_threshold = score_threshold
        self._lock = threading.Lock()
        self._detector = None
        self._recognizer = None

    def _load(self):
        if self._detector is None:
            det = ensure_model(self.models_dir, "yunet")
            rec = ensure_model(self.models_dir, "sface")
            self._detector = cv2.FaceDetectorYN.create(str(det), "", (320, 320),
                                                       self.score_threshold, 0.3, 5000)
            self._recognizer = cv2.FaceRecognizerSF.create(str(rec), "")

    def detect(self, image: np.ndarray) -> list[DetectedFace]:
        with self._lock:
            self._load()
            h, w = image.shape[:2]
            self._detector.setInputSize((w, h))
            _, faces = self._detector.detect(image)
        out = []
        if faces is None:
            return out
        for row in faces:
            x, y, fw, fh = (int(v) for v in row[:4])
            out.append(DetectedFace((max(0, x), max(0, y), fw, fh), float(row[-1]), row))
        return out

    def embed(self, image: np.ndarray, face: DetectedFace) -> np.ndarray:
        with self._lock:
            self._load()
            aligned = self._recognizer.alignCrop(image, face.raw)
            feat = self._recognizer.feature(aligned)
        return normalise(feat.flatten().astype(np.float32))


def normalise(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class Gallery:
    """In-memory set of known embeddings."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ids: list[int] = []
        self._names: list[str] = []
        self._matrix = np.zeros((0, 128), dtype=np.float32)

    def load(self, rows: list[tuple[int, str, bytes]]) -> None:
        ids, names, vecs = [], [], []
        for pid, name, blob in rows:
            v = np.frombuffer(blob, dtype=np.float32)
            if v.size == 0:
                continue
            ids.append(pid)
            names.append(name)
            vecs.append(normalise(v))
        with self._lock:
            self._ids, self._names = ids, names
            self._matrix = (np.vstack(vecs) if vecs
                            else np.zeros((0, 128), dtype=np.float32))

    def __len__(self) -> int:
        return len(self._ids)

    def match(self, emb: np.ndarray, threshold: float = DEFAULT_THRESHOLD) -> Match:
        with self._lock:
            if not self._ids:
                return Match(None, "unknown", 0.0)
            sims = self._matrix @ normalise(emb.astype(np.float32))
            i = int(np.argmax(sims))
            best = float(sims[i])
            if best >= threshold:
                return Match(self._ids[i], self._names[i], best)
            return Match(None, "unknown", best)


def decode_image(data: bytes) -> np.ndarray | None:
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def crop(image: np.ndarray, bbox: tuple[int, int, int, int], pad: float = 0.25) -> np.ndarray:
    x, y, w, h = bbox
    H, W = image.shape[:2]
    px, py = int(w * pad), int(h * pad)
    return image[max(0, y - py):min(H, y + h + py), max(0, x - px):min(W, x + w + px)]


def enroll_embedding(backend, image: np.ndarray) -> np.ndarray:
    """Return the embedding of the single face in `image`, or raise ValueError."""
    faces = backend.detect(image)
    if not faces:
        raise ValueError("no face found in the photo")
    if len(faces) > 1:
        faces.sort(key=lambda f: f.bbox[2] * f.bbox[3], reverse=True)
        a0 = faces[0].bbox[2] * faces[0].bbox[3]
        a1 = faces[1].bbox[2] * faces[1].bbox[3]
        if a1 > 0.5 * a0:
            raise ValueError("more than one face in the photo; use a photo of one person")
    return backend.embed(image, faces[0])
