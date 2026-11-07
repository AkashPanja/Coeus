"""Zone masks and background-subtraction motion detection."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

DETECT_WIDTH = 640


@dataclass
class ZoneSpec:
    name: str
    points: list[list[float]]  # normalised [[x, y], ...]
    kind: str = "include"
    min_ratio: float = 0.01


@dataclass
class MotionHit:
    zone: str
    ratio: float
    bbox: tuple[int, int, int, int]  # x, y, w, h in *original* frame pixels


def polygon_mask(shape: tuple[int, int], points: list[list[float]]) -> np.ndarray:
    h, w = shape
    mask = np.zeros((h, w), dtype=np.uint8)
    if len(points) >= 3:
        pts = np.array([[int(round(x * (w - 1))), int(round(y * (h - 1)))] for x, y in points],
                       dtype=np.int32)
        cv2.fillPoly(mask, [pts], 255)
    return mask


def build_masks(shape: tuple[int, int], zones: list[ZoneSpec]) -> list[tuple[ZoneSpec, np.ndarray]]:
    """Include-zone masks with all exclude zones cut out. No include zones = whole frame."""
    h, w = shape
    exclude = np.zeros((h, w), dtype=np.uint8)
    for z in zones:
        if z.kind == "exclude":
            exclude |= polygon_mask(shape, z.points)
    includes = [z for z in zones if z.kind != "exclude" and len(z.points) >= 3]
    if not includes:
        includes = [ZoneSpec("full", [[0, 0], [1, 0], [1, 1], [0, 1]])]
    out = []
    for z in includes:
        m = polygon_mask(shape, z.points)
        m[exclude > 0] = 0
        out.append((z, m))
    return out


class MotionDetector:
    def __init__(self, zones: list[ZoneSpec], consecutive: int = 2, history: int = 300,
                 var_threshold: float = 25):
        self.zones = zones
        self.consecutive = max(1, consecutive)
        self.bg = cv2.createBackgroundSubtractorMOG2(history=history, varThreshold=var_threshold,
                                                     detectShadows=True)
        self._masks: list[tuple[ZoneSpec, np.ndarray]] | None = None
        self._shape: tuple[int, int] | None = None
        self._streak: dict[str, int] = {}
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.frames = 0

    def _prepare(self, frame: np.ndarray) -> tuple[np.ndarray, float]:
        h, w = frame.shape[:2]
        scale = DETECT_WIDTH / w if w > DETECT_WIDTH else 1.0
        small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale != 1.0 else frame
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
        return cv2.GaussianBlur(gray, (5, 5), 0), scale

    def process(self, frame: np.ndarray) -> list[MotionHit]:
        gray, scale = self._prepare(frame)
        if self._shape != gray.shape:
            self._shape = gray.shape
            self._masks = build_masks(gray.shape, self.zones)
        fg = self.bg.apply(gray)
        self.frames += 1
        if self.frames < 10:  # let the background model settle
            return []
        _, fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)  # drop shadows (127)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self._kernel)
        fg = cv2.dilate(fg, self._kernel, iterations=2)
        hits = []
        assert self._masks is not None
        for zone, mask in self._masks:
            area = int(np.count_nonzero(mask))
            if area == 0:
                continue
            moving = cv2.bitwise_and(fg, mask)
            ratio = np.count_nonzero(moving) / area
            if ratio >= zone.min_ratio:
                self._streak[zone.name] = self._streak.get(zone.name, 0) + 1
            else:
                self._streak[zone.name] = 0
                continue
            if self._streak[zone.name] < self.consecutive:
                continue
            x, y, w, h = cv2.boundingRect(moving)
            inv = 1.0 / scale
            hits.append(MotionHit(zone.name, ratio,
                                  (int(x * inv), int(y * inv), int(w * inv), int(h * inv))))
        return hits


def zone_bbox(shape: tuple[int, int], zone: ZoneSpec, pad: float = 0.05) -> tuple[int, int, int, int]:
    """Bounding box (x, y, w, h) of a zone in pixel space with padding."""
    h, w = shape
    if zone.name == "full" or len(zone.points) < 3:
        return 0, 0, w, h
    xs = [p[0] for p in zone.points]
    ys = [p[1] for p in zone.points]
    x0, x1 = max(0.0, min(xs) - pad), min(1.0, max(xs) + pad)
    y0, y1 = max(0.0, min(ys) - pad), min(1.0, max(ys) + pad)
    return int(x0 * w), int(y0 * h), int((x1 - x0) * w), int((y1 - y0) * h)
