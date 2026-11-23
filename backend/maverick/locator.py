"""Find one thing in the camera frame, fast, for the AR highlight on the lens.

The hub asks this several times a second (calls overlap); the glasses track the box
between answers. Kept apart from the step checker so its answer is tiny and quick.
Also finds the calibration rectangle that lines the lens display up with the camera.
"""
from __future__ import annotations

import base64
import io
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from PIL import Image
from pydantic import BaseModel

from . import ai
from .guide import Step
from .visual import reference_b64

GUIDES = Path(__file__).resolve().parents[2] / "guides"
MODEL = os.environ.get("MAVERICK_LOCATE_MODEL", "gpt-5.6-luna")
SIDE = int(os.environ.get("MAVERICK_LOCATE_SIDE", "1280"))  # px sent to the model; boxes
# drift when the photo is shrunk (measured: exact at 1280, about 4% low at 768)
MIN_CONFIDENCE = float(os.environ.get("MAVERICK_LOCATE_CONFIDENCE", "0.6"))


class Found(BaseModel):
    found: bool
    confidence: float
    box: list[float]


SYSTEM = """You point at one thing in a photo taken by a camera on a person's glasses. \
Your box is drawn on their lens as an AR highlight, so it must be tight and on the right \
object.
box: [x0, y0, x1, y1] tight around exactly that thing in the LIVE PHOTO, in pixels from \
its top-left corner. You are told the LIVE PHOTO's size in pixels.
found: false, and box [], if the thing is not clearly visible. Never guess: a box on the \
wrong object is worse than no box.
confidence: 0 to 1 that the box is on exactly the right thing.
REFERENCE photos, when given, show the same physical object from other angles, or what \
the build looks like when the step is finished. Only the LIVE PHOTO gets a box."""


def shrink(jpeg_b64: str, side: int = SIDE) -> str:
    image = Image.open(io.BytesIO(base64.b64decode(jpeg_b64)))
    if max(image.size) <= side:
        return jpeg_b64
    image.thumbnail((side, side))
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=82)
    return base64.b64encode(out.getvalue()).decode()


@lru_cache(maxsize=128)
def photo_b64(rel_path: str, side: int = 512) -> str | None:
    path = GUIDES / rel_path
    if not path.exists():
        return None
    image = Image.open(path).convert("RGB")
    image.thumbnail((side, side))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85)
    return base64.b64encode(out.getvalue()).decode()


def piece_name(step: Step) -> str:
    """Short label for the box, like 'Red hood'. Sentence case: nothing shouts."""
    if step.piece:
        return step.piece[:1].upper() + step.piece[1:]
    if step.parts:
        words = step.parts[0].split()
        if words and words[0].rstrip("x×").isdigit():
            words = words[1:]
        return " ".join(words[:3]).capitalize()
    return "This part"


def find_text(step: Step) -> str:
    return step.find or ", ".join(step.parts) or step.say


def place_text(step: Step) -> str:
    return step.place or step.detail or step.check


async def locate(step: Step, jpeg_b64: str, mode: Literal["pick", "place"],
                 roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)) -> list[float]:
    """Box of the part to pick up, or of the spot where it goes. [] if not visible.

    The part to pick up can be anywhere on the table, so that search uses the whole frame.
    The spot where it goes is on the build, so that search zooms onto the work area.
    """
    from .checker import _to_frame, crop_frame
    if mode == "pick":
        roi = (0.0, 0.0, 1.0, 1.0)
    try:
        live = crop_frame(jpeg_b64, roi)
    except Exception:
        live, roi = jpeg_b64, (0.0, 0.0, 1.0, 1.0)
    content: list = []
    if mode == "pick":
        for ref in step.refs[:2]:
            picture = photo_b64(ref)
            if picture:
                content += [ai.text("REFERENCE, the part to find:"), ai.image(picture, "low")]
        content.append(ai.text(
            f"Find the loose part the builder must pick up next: {find_text(step)}. "
            "It is lying on the table or held in a hand, not yet attached. "
            "If several are needed, box the one nearest the builder's hands."))
    else:
        after = reference_b64(step)
        if after:
            content += [ai.text("REFERENCE, the build with this step finished:"),
                        ai.image(after, "low")]
        content.append(ai.text(
            f"The builder is holding this part: {find_text(step)}. "
            f"Find the spot on the build where it attaches: {place_text(step)}. "
            "Box only that spot on the build, sized like the part, not the whole build "
            "and not the part in the hand."))
    picture, width, height = ai.for_boxes(live)
    content += [ai.text(f"LIVE PHOTO ({width} pixels wide, {height} pixels high):"),
                ai.image(picture, "high")]
    try:
        out = await ai.parse(MODEL, content, Found, system=SYSTEM, max_tokens=70,
                             effort="none", timeout=8)
    except Exception as err:
        print(f"[locator] {type(err).__name__}: {err}")
        return []
    if out is None or not out.found or out.confidence < MIN_CONFIDENCE:
        return []
    box = _to_frame(ai.unit_box(out.box, width, height), roi)
    if not box:
        return []
    x0, y0, x1, y1 = box
    if x1 - x0 < 0.01 or y1 - y0 < 0.01 or (x1 - x0) * (y1 - y0) > 0.6:
        return []  # a sliver, or most of the frame: not a useful highlight
    return box


# ---------- teaching the demo steps ----------

async def crop_to_subject(jpeg_b64: str) -> str:
    """Cut a taught photo down to the LEGO part being shown to the camera."""
    picture, width, height = ai.for_boxes(jpeg_b64)
    out = await ai.parse(MODEL, [
        ai.text("Find the single LEGO part or LEGO sub-assembly being shown to the camera: "
                "the one held up in a hand, or lying alone nearest the centre of the photo."),
        ai.text(f"LIVE PHOTO ({width} pixels wide, {height} pixels high):"),
        ai.image(picture, "high")],
        Found, system=SYSTEM, max_tokens=70, effort="none", timeout=15)
    found = ai.unit_box(out.box, width, height) if out and out.found else []
    if not found:
        return jpeg_b64
    image = Image.open(io.BytesIO(base64.b64decode(jpeg_b64))).convert("RGB")
    w, h = image.size
    x0, y0, x1, y1 = found
    pad_x, pad_y = (x1 - x0) * 0.18, (y1 - y0) * 0.18
    part = image.crop((int(max(0, x0 - pad_x) * w), int(max(0, y0 - pad_y) * h),
                       int(min(1, x1 + pad_x) * w), int(min(1, y1 + pad_y) * h)))
    if part.width < 40 or part.height < 40:
        return jpeg_b64
    buffer = io.BytesIO()
    part.save(buffer, "JPEG", quality=90)
    return base64.b64encode(buffer.getvalue()).decode()


class StepWords(BaseModel):
    piece: str
    find: str
    say: str
    place: str
    check: str


DESCRIBE = """You are writing one step of a LEGO build for an AR assistant on smart \
glasses. You get photos of the PART to attach in this step, and photos of the build with \
this step FINISHED (and, when given, the build BEFORE this step). Write:
- piece: 2 or 3 words naming the part by colour and shape, like "red hood" or "rear wheels".
- find: one sentence describing how the part looks, so it can be picked out among other \
LEGO parts on a table: colours, shape, size, distinctive features.
- say: the instruction shown to the builder. 8 words max, starts with a verb.
- place: one sentence saying exactly where on the build the part attaches, relative to \
things the builder can see.
- check: one sentence saying what the camera sees when the step is finished.
Plain words. No part numbers."""


async def describe_step(step: Step, previous: Step | None) -> dict:
    content: list = []
    for ref in step.refs[:3]:
        picture = photo_b64(ref)
        if picture:
            content += [ai.text("PART:"), ai.image(picture, "high")]
    if previous and previous.done_refs:
        picture = photo_b64(previous.done_refs[0])
        if picture:
            content += [ai.text("BEFORE this step:"), ai.image(picture, "high")]
    for ref in step.done_refs[:2]:
        picture = photo_b64(ref)
        if picture:
            content += [ai.text("FINISHED:"), ai.image(picture, "high")]
    if not content:
        raise ValueError("Capture a photo of the part or of the finished step first")
    hints = "; ".join(f"{k}: {v}" for k, v in
                      (("piece", step.piece), ("say", step.say)) if v.strip())
    if hints:
        content.append(ai.text(f"The builder already wrote (keep these): {hints}"))
    out = await ai.parse(MODEL, content, StepWords, system=DESCRIBE, max_tokens=400,
                         effort="low", timeout=40)
    if out is None:
        raise ValueError("No description came back")
    words = out.model_dump()
    for kept in ("piece", "say"):  # what the builder typed wins
        if getattr(step, kept).strip():
            words[kept] = getattr(step, kept)
    return words


# ---------- lens calibration ----------

def find_pink_rectangle(jpeg_b64: str) -> list[float]:
    """The bright pink calibration rectangle on the laptop screen, as a camera box.

    The wearer lines the lens display's outline up with it, so this box is exactly the
    part of the camera frame that the display covers. [] if it cannot be found.
    """
    data = np.frombuffer(base64.b64decode(jpeg_b64), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        return []
    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # Pink/magenta on an LCD through a small camera: hue around 150 (OpenCV 0..180).
    mask = cv2.inRange(hsv, (135, 70, 110), (175, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    biggest = max(contours, key=cv2.contourArea)
    x, y, bw, bh = cv2.boundingRect(biggest)
    filled = cv2.contourArea(biggest) / max(1, bw * bh)
    if bw * bh < 0.01 * w * h or filled < 0.6:
        return []
    return [x / w, y / h, (x + bw) / w, (y + bh) / h]


async def find_calibration(jpeg_b64: str) -> list[float]:
    box = find_pink_rectangle(jpeg_b64)
    if box:
        return box
    try:  # colours washed out by the camera: let the model find it
        picture, width, height = ai.for_boxes(jpeg_b64)
        out = await ai.parse(MODEL, [
            ai.text("Find the large solid pink (magenta) rectangle shown on the laptop "
                    "screen. Box the pink area only."),
            ai.text(f"LIVE PHOTO ({width} pixels wide, {height} pixels high):"),
            ai.image(picture, "high")], Found, system=SYSTEM, max_tokens=70,
            effort="none", timeout=15)
    except Exception as err:
        print(f"[locator] calibration: {type(err).__name__}: {err}")
        return []
    return ai.unit_box(out.box, width, height) if out and out.found else []
