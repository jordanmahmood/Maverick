"""Replay a recorded run through the hub's real logic, at the original pace.

    MAVERICK_COLLECT=0 uv run python replay.py <run folder> [start] [end]

A run folder is what the hub writes when started with MAVERICK_RECORD=<folder> and
MAVERICK_KEEP_FRAMES=1: frames/<unix time>.jpg plus events.jsonl. start/end are unix times
(default: the first full run in the folder, from "step 1" to "All done").

Nothing is sent to any glasses. The detector and the cloud checks run for real, so the
printout shows what the wearer would have seen with today's code on yesterday's footage:
when each step finished, every change of the hint line, and how often each box was drawn.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("MAVERICK_COLLECT", "0")
os.environ.pop("MAVERICK_RECORD", None)

import maverick.main as hubmod  # noqa: E402
from maverick.detector import Detector, model_path  # noqa: E402


async def replay(folder: Path, start: float | None, end: float | None) -> None:
    events = [json.loads(line) for line in (folder / "events.jsonl").open()]
    starts = [e["t"] for e in events if e.get("type") == "screen" and e.get("step") == 1]
    ends = [e["t"] for e in events if e.get("type") == "screen" and e.get("big") == "All done"]
    if end is None:
        end = ends[0] + 2 if ends else events[-1]["t"]
    if start is None:
        start = [t for t in starts if t < end][-1]
    frames = sorted((float(p.stem), p) for p in (folder / "frames").glob("*.jpg"))
    frames = [(t, p) for t, p in frames if start <= t <= end]
    print(f"{len(frames)} frames over {end - start:.0f} s")

    hub = hubmod.hub
    hub.detector = Detector(model_path(hub.session.guide.kit))
    seen: list[tuple[float, dict]] = []
    began = time.monotonic()

    async def record(message: dict, role: str | None = None, droppable: bool = False):
        seen.append((time.monotonic() - began, message))

    hub.broadcast = record
    await hub.command("restart")
    began = time.monotonic()
    for stamp, path in frames:
        wait = (stamp - start) - (time.monotonic() - began)
        if wait > 0:
            await asyncio.sleep(wait)
        hub.latest_frame = base64.b64encode(path.read_bytes()).decode()
        hub.frame_gseq = hub.frame_seq = hub.frame_seq + 1
        hub.kick_detect()
        hub.kick_check()
        if hub.session.finished:
            break
    await asyncio.sleep(3)

    boxes: dict[str, int] = {}
    step = 1

    def flush():
        if boxes:
            print("         boxes drawn: " + ", ".join(f"{k} x{v}" for k, v in sorted(boxes.items())))
            boxes.clear()

    hints = 0
    for at, message in seen:
        kind = message.get("type")
        if kind == "ar":
            for target in message.get("targets", []):
                boxes[target["label"]] = boxes.get(target["label"], 0) + 1
        elif kind == "screen":
            flush()
            step = message.get("step") or step
            print(f"{at:6.1f}  STEP {message.get('step') or '-'}: {message.get('big')}"
                  f"{'  [voice]' if message.get('say_url') else ''}")
        elif kind == "hint" and message.get("text"):
            hints += 1
            print(f"{at:6.1f}      hint ({message.get('phase')}): {message['text']}")
    flush()
    checks = [m for _, m in seen if m.get("type") == "status" and m.get("ms")]
    if checks:
        times = sorted(m["ms"] for m in checks)
        print(f"{len(checks)} full checks, median {times[len(times) // 2]} ms; {hints} hint changes")


async def replay_steps(folder: Path) -> None:
    """Each step on its own: put the hub on that step, play that step's footage, and see
    when it finishes compared with the day it was filmed. This stays meaningful even when
    one step would now finish earlier or later than it did live."""
    events = [json.loads(line) for line in (folder / "events.jsonl").open()]
    ends = [e["t"] for e in events if e.get("type") == "screen" and e.get("big") == "All done"]
    end = ends[0]
    start = [e["t"] for e in events if e.get("type") == "screen" and e.get("step") == 1
             and e["t"] < end][-1]
    marks = [(e["step"], e["t"]) for e in events if e.get("type") == "screen" and e.get("step")
             and e.get("flash") and start <= e["t"] <= end] + [(None, end)]
    marks = [(1, start)] + marks
    frames = sorted((float(p.stem), p) for p in (folder / "frames").glob("*.jpg"))
    hub = hubmod.hub
    hub.detector = Detector(model_path(hub.session.guide.kit))
    for (step, began_at), (_, ended_at) in zip(marks, marks[1:]):
        seen: list[tuple[float, dict]] = []
        clock = time.monotonic()

        async def record(message: dict, role: str | None = None, droppable: bool = False):
            seen.append((time.monotonic() - clock, message))

        hub.broadcast = record
        await hub.show(hub.session.goto(step - 1), intro=True)
        seen.clear()
        clock = time.monotonic()
        finished = None
        for stamp, path in [(t, p) for t, p in frames if began_at <= t <= ended_at + 4]:
            wait = (stamp - began_at) - (time.monotonic() - clock)
            if wait > 0:
                await asyncio.sleep(wait)
            if hub.session.index != step - 1 or hub.session.finished:
                finished = time.monotonic() - clock
                break
            hub.latest_frame = base64.b64encode(path.read_bytes()).decode()
            hub.frame_gseq = hub.frame_seq = hub.frame_seq + 1
            hub.kick_detect()
            hub.kick_check()
        await asyncio.sleep(0.5)
        hints = [(at, m["text"], m.get("phase")) for at, m in seen
                 if m.get("type") == "hint" and m.get("text")]
        boxes: dict[str, int] = {}
        for _, m in seen:
            if m.get("type") == "ar":
                for target in m.get("targets", []):
                    boxes[target["label"]] = boxes.get(target["label"], 0) + 1
        live = ended_at - began_at
        print(f"STEP {step}: live it took {live:5.1f} s; now "
              + (f"{finished:5.1f} s ({finished - live:+.1f})" if finished else "NOT FINISHED")
              + f" | boxes {boxes}")
        for at, text, phase in hints:
            print(f"      {at:5.1f}  ({phase}) {text}")
        for task in list(hub.checks) + list(hub.confirms):
            task.cancel()
        await asyncio.sleep(0.3)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    if "--steps" in sys.argv:
        asyncio.run(replay_steps(Path(sys.argv[1])))
        raise SystemExit(0)
    asyncio.run(replay(Path(sys.argv[1]), *(float(a) for a in sys.argv[2:4]),
                       *([None] * (2 - len(sys.argv[2:4])))))
