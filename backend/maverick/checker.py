"""Ask an Azure OpenAI vision model what the builder is doing on the current step.

The model sees the manual's drawings from before and after the step next to the live
camera frame. When we already know where the build is, the frame is cropped to that area
first, so small parts fill the picture. Set MAVERICK_MOCK=1, or leave maverick/.env
without Azure settings, to run without AI: the mock always answers 'not_yet'.
"""
from __future__ import annotations

import base64
import io
import os
from typing import Literal

from PIL import Image
from pydantic import BaseModel

from . import ai
from .guide import Step
from .state import Verdict
from .visual import reference_b64

Box = tuple[float, float, float, float]
FULL: Box = (0.0, 0.0, 1.0, 1.0)


class CheckOut(BaseModel):
    seen: str
    build_box: list[float]
    phase: Literal["no_build", "finding_part", "holding_part", "placing", "done", "mistake"]
    confidence: float
    hint: str
    message: str
    target: str
    box: list[float]


class CheckSlim(BaseModel):
    """The same judgement without boxes, for when a local detector supplies them: fewer
    words to write, so the answer arrives sooner."""
    seen: str
    phase: Literal["no_build", "finding_part", "holding_part", "placing", "done", "mistake"]
    confidence: float
    hint: str
    message: str


class Answer(BaseModel):
    text: str


def mock_mode() -> bool:
    return os.environ.get("MAVERICK_MOCK") == "1" or not ai.configured()


SYSTEM = """You watch a person build a LEGO set through a camera on their glasses and \
coach them, one step at a time. You get:
- BEFORE: a drawing or photo from before this step (it may show a different sub-assembly).
- AFTER: a drawing or photo with this step finished. In drawings, new parts are outlined.
- The step description.
- LIVE PHOTO: what the camera sees right now. It may be a zoomed crop.
Judge only the LIVE PHOTO. Be strict and literal. The model may be rotated, upside down \
or held in a hand: compare structure and colours, not the viewing angle.

phase:
- "no_build": no LEGO at all in the photo, or it is too blurred to tell.
- "finding_part": the build or loose LEGO parts are visible, but the part for this step \
is neither in the builder's hand nor on the model yet.
- "holding_part": the builder holds a correct part for this step; it is not attached yet.
- "placing": a correct part is at or near its place but not fully attached or pressed \
down, or only some of several parts are on.
- "done": the photo clearly shows the AFTER state: every new part attached in the right \
place and colour. A hand may be holding or steadying the build; judge whether the part is \
seated, not whether a hand is near. If the part is still loose, tilted, hovering or being \
lowered into place, that is "placing". If the BEFORE state would look the same from this \
angle, it is not done. A part of the same colour that was already on the build before \
this step does not count.
- "mistake": a wrong part or colour is attached, or a correct part is attached in a \
clearly wrong place or orientation.

seen: 8 words max, what the photo shows of the build right now.
build_box: [x0, y0, x1, y1] around the builder's work area in the LIVE PHOTO: the LEGO \
model plus any hand holding parts; if nothing is built yet, the pile of loose parts. Not \
the whole table. In pixels from the LIVE PHOTO's top-left corner; you are told its size. \
[] only if no LEGO is visible.
confidence: 0 to 1 for the phase.
hint: 8 words max. The next physical move, specific to what you see, like a coach at \
their shoulder. finding_part: say where the needed part is if you can see it ("Lime \
brick: left of the chassis"). holding_part: where it goes ("Now onto the studs behind \
the seat"). placing: the correction ("Press the rear end down"). done and no_build: "".
message: only for "mistake": 6 words max, plain words, what to fix. Otherwise "".
target: 3 words max: the needed part if you can see it loose or in the hand, otherwise \
the spot on the model where it goes. "" if neither is visible.
box: [x0, y0, x1, y1] of that target in the LIVE PHOTO, in pixels. [] if target is ""."""


def _without_boxes(prompt: str) -> str:
    keep, skipping = [], False
    for line in prompt.split("\n"):
        if line.startswith(("build_box:", "target:", "box:")):
            skipping = True
        elif skipping and line[:1].isalpha() and ":" in line.split(" ")[0]:
            skipping = False
        if not skipping:
            keep.append(line)
    return "\n".join(keep)


SYSTEM_SLIM = _without_boxes(SYSTEM)


def crop_frame(jpeg_b64: str, roi: Box) -> str:
    """Cut the camera frame down to the work area, so the build fills the picture."""
    if roi == FULL:
        return jpeg_b64
    image = Image.open(io.BytesIO(base64.b64decode(jpeg_b64)))
    w, h = image.size
    part = image.crop((int(roi[0] * w), int(roi[1] * h), int(roi[2] * w), int(roi[3] * h)))
    if part.width < 640:  # the model reads small crops better when they are enlarged
        scale = 640 / part.width
        part = part.resize((640, int(part.height * scale)), Image.LANCZOS)
    out = io.BytesIO()
    part.convert("RGB").save(out, "JPEG", quality=88)
    return base64.b64encode(out.getvalue()).decode()


def _to_frame(box: list[float], roi: Box) -> list[float]:
    """A box in crop coordinates -> the same box in full-frame coordinates."""
    if len(box) != 4:
        return []
    x0, y0, x1, y1 = (min(1.0, max(0.0, v)) for v in box)
    if x1 <= x0 or y1 <= y0:
        return []
    rw, rh = roi[2] - roi[0], roi[3] - roi[1]
    return [roi[0] + x0 * rw, roi[1] + y0 * rh, roi[0] + x1 * rw, roi[1] + y1 * rh]


def work_area(build_box: list[float]) -> Box:
    """Grow the build's box into a 4:3-ish crop with room for hands. FULL if no box."""
    if len(build_box) != 4:
        return FULL
    x0, y0, x1, y1 = build_box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half_w = max((x1 - x0) * 0.85, 0.22)
    half_h = max((y1 - y0) * 0.85, 0.22)
    half_w = half_h = max(half_w, half_h)
    if half_w >= 0.46:
        return FULL
    cx = min(max(cx, half_w), 1 - half_w)
    cy = min(max(cy, half_h), 1 - half_h)
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


async def check_step(step: Step, jpeg_b64: str, previous: Step | None = None,
                     roi: Box = FULL, model: str | None = None, facts: str = "",
                     boxes: bool = True) -> Verdict:
    if mock_mode():
        return Verdict(status="not_yet")
    content: list = []
    before = reference_b64(previous) if previous else None
    after = reference_b64(step)
    if before:
        content += [ai.text("BEFORE:"), ai.image(before, "high")]
    if after:
        content += [ai.text("AFTER:"), ai.image(after, "high")]
    brief = (f"Step {step.id}: {step.say}\n"
             f"Parts added in this step: {', '.join(step.parts) or step.find or step.piece or 'see the step text'}\n"
             f"Where: {step.place or step.detail}\n"
             f"Done looks like: {step.check or step.place or step.say}\n"
             f"Common mistakes: {'; '.join(step.common_mistakes) or 'none listed'}")
    if previous is not None:
        brief += f"\nThe step before this one (already done): {previous.say}"
    if facts:
        brief += f"\nFrom a separate part detector watching the same camera: {facts}"
    try:
        live = crop_frame(jpeg_b64, roi)
    except Exception as err:
        print(f"[checker] crop failed: {err}")
        live, roi = jpeg_b64, FULL
    zoom = "" if roi == FULL else ", zoomed onto the work area"
    live, width, height = ai.for_boxes(live)
    content += [ai.text(brief),
                ai.text(f"LIVE PHOTO ({width} pixels wide, {height} pixels high{zoom}):"),
                ai.image(live, "high")]
    try:
        if not boxes:
            slim = await ai.parse(model or ai.CHECK_MODEL, content, CheckSlim, system=SYSTEM_SLIM,
                                  max_tokens=160, effort="none", timeout=12)
            if slim is None:
                return Verdict(status="not_yet")
            return Verdict(status={"done": "done", "mistake": "mistake"}.get(slim.phase, "not_yet"),
                           message=slim.message, seen=slim.seen, phase=slim.phase,
                           confidence=min(1.0, max(0.0, slim.confidence)), hint=slim.hint)
        out = await ai.parse(model or ai.CHECK_MODEL, content, CheckOut, system=SYSTEM,
                             max_tokens=320, effort="none", timeout=12)
    except Exception as err:
        print(f"[checker] {type(err).__name__}: {err}")
        return Verdict(status="not_yet", seen="(check failed)")
    if out is None:
        return Verdict(status="not_yet")
    status = {"done": "done", "mistake": "mistake"}.get(out.phase, "not_yet")
    return Verdict(status=status, message=out.message, seen=out.seen, phase=out.phase,
                   confidence=min(1.0, max(0.0, out.confidence)), hint=out.hint,
                   target=out.target, box=_to_frame(ai.unit_box(out.box, width, height), roi),
                   build_box=_to_frame(ai.unit_box(out.build_box, width, height), roi))


class Confirm(BaseModel):
    attached: bool
    confidence: float


CONFIRM = """You confirm one step of a LEGO build from a close-up taken by a camera on the \
builder's glasses. You are told what the step adds and where it goes.
attached: true only if the new part is clearly seated in its place on the build in the \
LIVE PHOTO: pressed down, flush, no gap. A part held just above or beside its place, \
tilted, or still gripped by fingers that are lowering or lining it up is NOT attached. \
If the photo is blurred or the build is partly out of view, false. Hands may be holding the build, even around the new part: builders keep hold \
of a model. False if the part is loose, lying beside the build, hovering above it, tilted, \
or not visible.
confidence: 0 to 1."""


async def confirm_done(step: Step, jpeg_b64: str, area: Box = FULL) -> tuple[bool, float, str]:
    """A quick second opinion on a close-up of the build. Small question, small picture,
    small answer, so it comes back fast.

    It does not ask whether hands are on the part: on a real run the builder held the car
    by the freshly attached end the whole time, and the step never finished."""
    if mock_mode():
        return False, 0.0, ""
    try:
        live = crop_frame(jpeg_b64, area)
    except Exception:
        live = jpeg_b64
    content = [ai.text(f"This step adds: {step.find or step.piece}.\nWhere it goes: "
                       f"{step.place or step.detail}\nFinished looks like: {step.check or step.say}"),
               ai.text("LIVE PHOTO:"), ai.image(live, "high")]
    try:
        out = await ai.parse(ai.CHECK_MODEL, content, Confirm, system=CONFIRM, max_tokens=45,
                             effort="none", timeout=8)
    except Exception as err:
        print(f"[checker] confirm: {type(err).__name__}: {err}")
        return False, 0.0, ""
    if out is None:
        return False, 0.0, ""
    return bool(out.attached), float(out.confidence), ""


async def answer_question(step: Step, question: str, jpeg_b64: str | None,
                          context: str = "") -> str:
    """A spoken question about the task, answered in one short line."""
    if mock_mode():
        return "Mock mode: no answer"
    content: list = []
    after = reference_b64(step)
    if after:
        content += [ai.text("Manual drawing of this step finished:"), ai.image(after, "high")]
    if jpeg_b64:
        content += [ai.text("What the wearer sees now:"), ai.image(jpeg_b64, "high")]
    content.append(ai.text(
        f"The wearer is building a LEGO set and is on this step: {step.say}. {step.detail} "
        f"Parts: {', '.join(step.parts)}\n"
        + (f"\nWhat is known about where the parts are:\n{context}" if context else "")
        + f"\nThey asked: {question}\n"
        "Answer in 14 words or fewer, like a helpful person next to them. It is spoken "
        "aloud and shown on a tiny display. No lists."))
    try:
        answer = await ai.parse(ai.CHECK_MODEL, content, Answer, max_tokens=120, timeout=20)
    except Exception as err:
        print(f"[checker] question failed: {err}")
        return "Could not answer, try again"
    return answer.text if answer else "Could not answer, try again"
