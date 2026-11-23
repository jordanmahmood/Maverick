"""The trained real-time detector for the demo's parts (YOLO, runs on this Mac).

`python -m maverick.train` makes guides/kits/<kit>/model.pt from photos of the parts.
When that file exists the hub boxes every camera frame with it (tens of milliseconds)
instead of asking the cloud model (over a second), so the AR box follows in real time.
"""
from __future__ import annotations

import base64
import os
import threading
import time
from pathlib import Path

import cv2
import numpy as np

KITS = Path(__file__).resolve().parents[2] / "guides" / "kits"
IMGSZ = int(os.environ.get("MAVERICK_DETECT_SIZE", "960"))
MIN_CONF = float(os.environ.get("MAVERICK_DETECT_CONF", "0.30"))


def model_path(kit: str | None) -> Path | None:
    path = KITS / str(kit or "") / "model.pt"
    return path if kit and path.exists() else None


class Detector:
    def __init__(self, path: Path):
        from ultralytics import YOLO  # slow import: only when a model exists
        import torch
        self.path, self.stamp = path, path.stat().st_mtime
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.model = YOLO(str(path))
        self.names: dict[int, str] = dict(self.model.names)
        self.lock = threading.Lock()
        self.ms = 0.0
        self.detect_array(np.zeros((720, 960, 3), np.uint8))  # warm up

    def stale(self) -> bool:
        return not self.path.exists() or self.path.stat().st_mtime != self.stamp

    def detect_array(self, image: np.ndarray) -> list[dict]:
        with self.lock:
            started = time.perf_counter()
            result = self.model.predict(image, imgsz=IMGSZ, conf=MIN_CONF, device=self.device,
                                        verbose=False)[0]
            self.ms = (time.perf_counter() - started) * 1000
        found = []
        for box, conf, cls in zip(result.boxes.xyxyn.tolist(), result.boxes.conf.tolist(),
                                  result.boxes.cls.tolist()):
            found.append({"cls": self.names[int(cls)], "conf": round(float(conf), 3),
                          "box": [round(float(v), 4) for v in box]})
        return sorted(found, key=lambda d: -d["conf"])

    def detect(self, jpeg_b64: str) -> list[dict]:
        return self.detect_frame(jpeg_b64)[0]

    def detect_frame(self, jpeg_b64: str) -> tuple[list[dict], np.ndarray | None]:
        """Detections plus the decoded picture, for whoever else needs the pixels."""
        data = np.frombuffer(base64.b64decode(jpeg_b64), dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        return ([], None) if image is None else (self.detect_array(image), image)


def overlap(part: list[float], base: list[float]) -> float:
    """How much of the part's box lies inside the base's box, 0..1."""
    w = min(part[2], base[2]) - max(part[0], base[0])
    h = min(part[3], base[3]) - max(part[1], base[1])
    area = (part[2] - part[0]) * (part[3] - part[1])
    return max(0.0, w) * max(0.0, h) / area if area > 0 else 0.0


def gap(part: list[float], base: list[float]) -> float:
    """Empty space between two boxes, as a share of the base's longer side. 0 = touching."""
    dx = max(0.0, max(part[0], base[0]) - min(part[2], base[2]))
    dy = max(0.0, max(part[1], base[1]) - min(part[3], base[3]))
    size = max(base[2] - base[0], base[3] - base[1], 1e-6)
    return (dx * dx + dy * dy) ** 0.5 / size


def union(a: list[float], b: list[float], pad: float = 0.0) -> tuple[float, float, float, float]:
    """One box around both, grown by `pad` of its size on every side, kept inside the frame."""
    x0, y0, x1, y1 = min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])
    px, py = (x1 - x0) * pad, (y1 - y0) * pad
    return (max(0.0, x0 - px), max(0.0, y0 - py), min(1.0, x1 + px), min(1.0, y1 + py))


def best(found: list[dict], cls: str, min_conf: float = 0.45) -> dict | None:
    """The most confident detection of one class, or None."""
    for det in found:  # already sorted by confidence
        if det["cls"] == cls and det["conf"] >= min_conf:
            return det
    return None
