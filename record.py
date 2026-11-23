"""Record a demo run: the Maverick page plus the room's sound, stopping by itself.

    uv run --project backend python record.py [--max 600] [--crop W:H:X:Y] [--mic NAME]

Films the part of the screen where the Maverick page is (not the terminal next to it) with
the MacBook's microphone, which hears both the builder and the assistant's spoken replies.
It watches the hub and stops a few seconds after the last step is finished, or after --max
seconds. The file lands in recordings/run-<date>-<time>.mp4.

The first time, macOS asks to let the terminal record the screen and use the microphone.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent
# Left half of a 3420x2224 screen, below the browser's tab and address bars.
DEFAULT_CROP = "1916:1979:0:245"


async def wait_for_the_end(limit: float) -> str:
    """Return why we stopped: 'finished', 'time', or 'hub gone'."""
    started = time.monotonic()
    armed = False        # a run is under way (some step is not finished yet)
    finished_at = 0.0
    try:
        async with websockets.connect("ws://127.0.0.1:8000/ws?role=monitor", max_size=None) as ws:
            while True:
                now = time.monotonic()
                if now - started > limit:
                    return "time"
                if finished_at and now - finished_at > 4.0:  # keep the final tick on film
                    return "finished"
                try:
                    message = json.loads(await asyncio.wait_for(ws.recv(), 0.5))
                except asyncio.TimeoutError:
                    continue
                if message.get("type") != "state":
                    continue
                if not message["finished"]:
                    armed, finished_at = True, 0.0
                elif armed and not finished_at:
                    finished_at = time.monotonic()
                    print("Last step finished; stopping in a few seconds.", flush=True)
    except Exception as err:
        print(f"Lost the hub ({type(err).__name__}); stopping.", flush=True)
        return "hub gone"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=float, default=600, help="stop after this many seconds")
    parser.add_argument("--crop", default=DEFAULT_CROP, help="W:H:X:Y in screen pixels")
    parser.add_argument("--mic", default="MacBook Air Microphone")
    args = parser.parse_args()

    out_dir = ROOT / "recordings"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / time.strftime("run-%Y%m%d-%H%M%S.mp4")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "avfoundation", "-capture_cursor", "0", "-framerate", "30",
        "-i", f"Capture screen 0:{args.mic}",
        "-vf", f"crop={args.crop},scale=1280:-2", "-pix_fmt", "yuv420p",
        "-c:v", "h264_videotoolbox", "-b:v", "6M", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(target),
    ]
    recorder = subprocess.Popen(command, stdin=subprocess.PIPE)
    print(f"Recording to {target}", flush=True)
    started = time.monotonic()
    try:
        reason = asyncio.run(wait_for_the_end(args.max))
    finally:
        try:
            recorder.communicate(input=b"q", timeout=20)  # lets ffmpeg close the file properly
        except subprocess.TimeoutExpired:
            recorder.terminate()
            recorder.wait(timeout=10)
    seconds = time.monotonic() - started
    size = target.stat().st_size / 1e6 if target.exists() else 0
    print(f"Stopped ({reason}) after {seconds:.0f} s. {target} ({size:.1f} MB)", flush=True)


if __name__ == "__main__":
    main()
