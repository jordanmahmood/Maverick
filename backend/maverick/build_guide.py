"""Turn any manual or recipe (plain text) into a guide JSON, using Claude once.

    uv run python -m maverick.build_guide manual.txt ../guides/my-guide.json
"""
from __future__ import annotations

import sys
from pathlib import Path

import anthropic

from .guide import Guide

PROMPT = """Turn this manual into a guide for smart glasses with a camera.
Rules:
- One physical action per step. Keep the original order.
- say: 8 words max, starts with a verb.
- detail: one short sentence with the extra hint.
- check: one sentence describing what a head-worn camera SEES when the step is
  finished. Name colors, shapes and positions. It must be checkable from a photo.
- common_mistakes: 1 to 3 likely visible mistakes.

Manual:
"""


def build(text: str) -> Guide:
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model="claude-opus-5-5",
        max_tokens=16000,
        output_config={"effort": "high"},
        output_format=Guide,
        messages=[{"role": "user", "content": PROMPT + text}],
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise SystemExit("Claude declined or returned no guide. Try rephrasing the manual.")
    return response.parsed_output


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    guide = build(Path(sys.argv[1]).read_text())
    Path(sys.argv[2]).write_text(guide.model_dump_json(indent=2))
    print(f"Wrote {len(guide.steps)} steps to {sys.argv[2]}")
