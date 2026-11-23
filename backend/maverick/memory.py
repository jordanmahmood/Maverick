"""Where every part was last seen, even after the wearer looks away.

The camera has no compass, so head movement is measured from the picture itself: each
frame is compared with the one before it and the shift between them is added up into a
running "pan" (how far the view has moved, in frame widths). A part seen at some spot in
the frame is filed under spot + pan. Later, pan tells us where that spot is relative to
the current view: off to the right, down and to the left, and so on.

It is dead reckoning: good for "look to your right" over the next minute or so, not for
millimetres. Leaning in or tilting the head adds error; every fresh sighting corrects it.
"""
from __future__ import annotations

import time

import cv2
import numpy as np

SEEN_CONF = 0.5         # a sighting must be at least this confident to be remembered
IN_VIEW_SECONDS = 0.6   # seen this recently = "it is in view right now"
STALE_SECONDS = 180     # older than this: say so, the part has probably been moved


class Memory:
    def __init__(self):
        self.pan = np.zeros(2)                  # world position of the frame's top-left corner
        self.previous: np.ndarray | None = None
        self.window: np.ndarray | None = None
        self.seen: dict[str, dict] = {}         # class -> {"x","y","w","h","t"} in world units

    def reset(self):
        self.seen.clear()

    def update(self, image: np.ndarray, found: list[dict]) -> None:
        """Feed one camera frame (BGR) and what the detector found in it."""
        small = cv2.resize(image, (192, 144), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if self.previous is not None and self.previous.shape == gray.shape:
            if self.window is None or self.window.shape != gray.shape:
                self.window = cv2.createHanningWindow((gray.shape[1], gray.shape[0]), cv2.CV_32F)
            (dx, dy), response = cv2.phaseCorrelate(self.previous, gray, self.window)
            h, w = gray.shape
            # A clear peak and a believable shift; otherwise assume the head did not move.
            if response > 0.08 and abs(dx) < w * 0.45 and abs(dy) < h * 0.45:
                # The picture sliding right means the camera turned left.
                self.pan -= np.array([dx / w, dy / h])
        self.previous = gray
        now = time.monotonic()
        best: dict[str, dict] = {}
        for det in found:
            if det["conf"] >= SEEN_CONF and det["cls"] not in best:
                best[det["cls"]] = det
        for cls, det in best.items():
            x0, y0, x1, y1 = det["box"]
            self.seen[cls] = {"x": (x0 + x1) / 2 + self.pan[0], "y": (y0 + y1) / 2 + self.pan[1],
                              "w": x1 - x0, "h": y1 - y0, "t": now}

    def offset(self, cls: str) -> tuple[float, float, float] | None:
        """(dx, dy, age): how far the part is from the centre of the current view, in
        frame widths and heights, and how many seconds since it was seen. None if never."""
        entry = self.seen.get(cls)
        if entry is None:
            return None
        return (entry["x"] - self.pan[0] - 0.5, entry["y"] - self.pan[1] - 0.5,
                time.monotonic() - entry["t"])

    def away(self, cls: str) -> dict | None:
        """A pointer toward a part that is out of view. None if it is in view or unknown."""
        where = self.offset(cls)
        if where is None:
            return None
        dx, dy, age = where
        if age < IN_VIEW_SECONDS or age > STALE_SECONDS or (abs(dx) <= 0.5 and abs(dy) <= 0.5):
            return None
        return {"dx": round(float(dx), 3), "dy": round(float(dy), 3), "age": round(age, 1)}

    def remembered_box(self, cls: str) -> list[float] | None:
        """Where the part was last seen, as a box in the current view. None if that spot
        is outside the view, unknown, in view right now, or too old to trust."""
        entry, where = self.seen.get(cls), self.offset(cls)
        if entry is None or where is None:
            return None
        dx, dy, age = where
        if age < IN_VIEW_SECONDS or age > STALE_SECONDS or abs(dx) > 0.5 or abs(dy) > 0.5:
            return None
        cx, cy = dx + 0.5, dy + 0.5
        return [max(0.0, cx - entry["w"] / 2), max(0.0, cy - entry["h"] / 2),
                min(1.0, cx + entry["w"] / 2), min(1.0, cy + entry["h"] / 2)]

    def describe(self, cls: str, name: str) -> str:
        """One spoken sentence saying where the part is, in plain directions."""
        where = self.offset(cls)
        if where is None:
            return f"I haven't seen the {name} yet. Look around the table."
        dx, dy, age = where
        direction = direction_words(dx, dy)
        if age < IN_VIEW_SECONDS:
            return f"The {name} is in view, {direction}."
        when = _ago(age)
        caution = " It may have moved since." if age > 90 else ""
        return f"The {name} is {direction}. I saw it there {when}.{caution}"

    def facts(self, parts: dict[str, str]) -> str:
        """Every part's whereabouts, as lines for the model that answers questions."""
        return "\n".join(f"- {self.describe(cls, name)}" for cls, name in parts.items())


def direction_words(dx: float, dy: float) -> str:
    """Offset from the centre of the view (in frame widths / heights; right and down are
    positive) -> words. Half a frame is the edge of the view, so beyond 0.5 the wearer has
    to turn their head. Only a part really near the centre is "straight ahead"."""
    def one(value: float, low: str, high: str) -> str:
        size = abs(value)
        if size < 0.12:
            return ""
        word = high if value > 0 else low
        if size < 0.5:
            return f"slightly {word}"
        return f"far {word}" if size > 1.3 else word

    across = one(dx, "to your left", "to your right")
    updown = one(dy, "up", "down")
    if not across and not updown:
        return "straight ahead, in the middle of your view"
    if across and updown:
        return f"{updown} and {across}"
    return across or f"{updown} from where you are looking"


def _ago(seconds: float) -> str:
    if seconds < 5:
        return "a moment ago"
    if seconds < 60:
        return f"{int(round(seconds / 5) * 5)} seconds ago"
    return f"about {int(round(seconds / 60))} minute{'s' if seconds >= 90 else ''} ago"
