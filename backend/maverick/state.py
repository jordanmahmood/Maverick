"""The step state machine. Pure logic, no network: easy to test and to trust.

It decides what the glasses show. The display only changes on a command,
a 'done' verdict or a 'mistake' verdict. 'not_yet' changes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from .guide import Guide

MAX_WORDS = 8


class Verdict(BaseModel):
    status: Literal["done", "not_yet", "mistake"]
    message: str = ""
    seen: str = ""          # what the checker saw, for the monitor window
    confidence: float = 1.0
    hint: str = ""          # live coaching line
    target: str = ""        # what the box points at
    box: list[float] = []   # [x0, y0, x1, y1] in the camera frame, 0..1
    phase: str = ""         # no_build, finding_part, holding_part, placing, done, mistake
    build_box: list[float] = []  # where the build is in the camera frame


@dataclass
class Screen:
    top: str
    big: str
    sub: str = ""
    bottom: str = ""
    warn: bool = False
    progress: float = 0.0

    def to_message(self) -> dict:
        return {"type": "screen", **self.__dict__}


def clip(text: str, words: int = MAX_WORDS) -> str:
    parts = text.split()
    return text if len(parts) <= words else " ".join(parts[:words]) + "…"


class Session:
    def __init__(self, guide: Guide):
        self.guide = guide
        self.index = 0
        self.finished = False

    @property
    def step(self):
        return self.guide.steps[self.index]

    @property
    def total(self) -> int:
        return len(self.guide.steps)

    def _top(self) -> str:
        return f"STEP {self.index + 1} / {self.total}"

    def step_screen(self, prefix: str = "") -> Screen:
        if self.finished:
            return Screen(top="", big="All done", sub=self.guide.title, progress=1.0)
        return Screen(top=self._top(), big=prefix + clip(self.step.say),
                      sub=self._parts(),
                      bottom="",
                      progress=self.index / self.total)

    def _parts(self) -> str:
        """The parts to pick up, short enough for two small lines."""
        parts = self.step.parts
        text = " · ".join(p.replace("x ", "× ", 1) for p in parts[:3])
        return text + (f" · +{len(parts) - 3} more" if len(parts) > 3 else "")

    def goto(self, index: int) -> Screen:
        self.index, self.finished = max(0, min(index, self.total - 1)), False
        return self.step_screen()

    def _advance(self) -> Screen:
        if self.index + 1 >= self.total:
            self.finished = True
            return self.step_screen()
        self.index += 1
        return self.step_screen(prefix="✓ ")

    def command(self, name: str) -> Screen | None:
        """Voice, nod or button commands. These always work, camera or not."""
        if name == "restart":
            self.index, self.finished = 0, False
            return self.step_screen()
        if self.finished:
            return self.step_screen()
        if name == "next":
            return self._advance()
        if name == "back":
            self.index = max(0, self.index - 1)
            return self.step_screen()
        if name == "repeat":
            return self.step_screen()
        if name == "more":
            return Screen(top=self._top(), big=clip(self.step.say),
                          sub=self.step.detail or "No extra detail for this step",
                          bottom='say "next" when done', progress=self.index / self.total)
        return None

    def apply_verdict(self, verdict: Verdict, for_index: int) -> Screen | None:
        """Camera verdicts. Returns None when the display should not change."""
        if self.finished or for_index != self.index:
            return None  # stale: the wearer already moved on
        if verdict.status == "done":
            return self._advance()
        if verdict.status == "mistake":
            return Screen(top=self._top(), big="! " + clip(verdict.message or "Check this step", 6),
                          sub=clip(self.step.say), bottom="fix it and keep going",
                          warn=True, progress=self.index / self.total)
        return None
