"""A guide is a list of short steps, each with a sentence the camera can check."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field


class Step(BaseModel):
    id: int
    say: str = Field(description="Instruction shown on the glasses. 8 words max.")
    detail: str = Field(default="", description="Longer hint, shown on 'tell me more'.")
    check: str = Field(default="", description="What the camera sees when this step is done.")
    common_mistakes: list[str] = Field(default_factory=list)
    # Filled by the LEGO kit parser. Paths are relative to the guides/ directory.
    parts: list[str] = Field(default_factory=list)
    image: str | None = None        # manual page showing the finished step
    parts_image: str | None = None  # manual page showing the parts to pick up
    page: int | None = None
    # For the AR highlight, and for steps taught with photos instead of a booklet.
    piece: str = ""                 # short name drawn on the box: "red hood"
    find: str = ""                  # what the part looks like, for finding it
    place: str = ""                 # where on the build it goes
    refs: list[str] = Field(default_factory=list)       # photos of the part
    done_refs: list[str] = Field(default_factory=list)  # photos of the finished step
    # For the trained real-time detector (guides/kits/<kit>/model.pt).
    cls: str = ""                   # detector class of the part to pick up: "hood"
    onto: str = ""                  # detector class of what it attaches to: "chassis"
    where: str = ""  # which part of the build it goes on: front, rear or middle
    small: bool = False  # a tiny part (the minifigure): the close-up look leads


class Guide(BaseModel):
    title: str
    steps: list[Step]
    kit: str | None = None
    source: str | None = None


def load_guide(path: str | Path) -> Guide:
    return Guide.model_validate(json.loads(Path(path).read_text()))
