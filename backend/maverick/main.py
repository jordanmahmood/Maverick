"""Maverick hub. One shared build session; glasses and monitor windows all join it.

WebSocket /ws?role=glasses|monitor   (JSON)
Clients send:
  {"type":"frame","jpeg":"<base64>"}                       camera frame
  {"type":"command","name":"next|back|repeat|peek|more|restart"}
  {"type":"audio","rate":16000,"pcm16":"<base64>"}         one spoken utterance (glasses mic)
  {"type":"hello","sizes":[...],"size":"1280x960"}         glasses camera capabilities
  {"type":"goto","index":3}                                monitor: jump to a step
  {"type":"auto","on":true|false}                          monitor: camera checks on/off
  {"type":"ask","text":"which one is the axle?"}
  {"type":"debug","verdict":"done|mistake"}                demo safety net
Hub sends:
  {"type":"screen","mode":"step|info","top","big","sub","bottom","warn","progress",
   "bricks","icon","image","peek_ms","flash","sound","say_url"}
       step mode is a thin strip: part icon + one line. `image` (where the part goes) is
       only shown for peek_ms. `say_url` is a spoken clip. info mode is centred text.
  {"type":"hint","text","tone":"info|ok|warn","phase","peek_ms","say_url"}   live coaching
  {"type":"config","width","height","quality","fps"}       glasses camera settings
  {"type":"status",...}  and, to monitors only: frame, guide, state, parse, heard.

  {"type":"ar","seq":N,"targets":[{"id","box","label","sub","style":"pick|place"}],
   "away":[{"label","dx","dy"}]}   away: only after the wearer asks where a part is; it is
       out of view, dx/dy frame-widths from the centre of the view (right and down positive)
       AR highlight. box is in camera frame N (the glasses' own frame number); the
       glasses track it forward to now. Empty targets clears it.
  Phone app (a monitor with &lite=1, served at /app) sends {"type":"onboarding"} (the lens
       waits: "Finish setup on your phone"), {"type":"eye","value"}, {"type":"prefs","values":
       {brightness, voice, guidance, camera, mic, save}} and {"type":"start"} (handoff: the
       lens takes over at step 1). config.brightness (0.05..1) sets the lens brightness,
       config.volume (0..100) the speaker, config.sleep turns display and camera off.
  A tap on the touchpad makes the glasses listen (slides are back / next): command "ptt", then an `audio` message
       with "ptt": true, which is acted on without the wake word.
       Monitors follow the lens's listening pill through `heard` messages with no text: kind
       "listening" (tap), "thinking" (tapped utterance received), "waiting" (hands-free one received).
  Untethered: config carries "hub_urls" (USB tunnel first, then this Mac's addresses); the
       glasses fail over to the next one when a link dies ({"type":"ping"} every 1.5 s lets
       them notice). On Wi-Fi the glasses always detect for themselves and upload 2 fps.
       {"type":"wifi","enable":bool} / {"type":"wifi","join":{"ssid","password"}} from the
       phone app are passed to the glasses and never logged.
  Local boxes: with config.boxes = "glasses" the hub sends {"type":"plan","step","show",
       "pick":{id,cls,label},"place":{id,classes,label,where},"heading","pick_conf",
       "keep_conf"} and the glasses choose and draw pick/place boxes from their own
       detections; `ar` to the glasses then only carries extras (seek-*, away arrows).
  On-glasses detection (prefs.detect = "glasses"): config carries "detect":"glasses" and
       "detect_version"; the glasses download guides/kits/demo/glasses/{meta.json,model.onnx},
       run it on their own camera frames and send {"type":"dets","seq","found":[{cls,conf,
       box}],"ms"}; the hub answers with the usual `ar` targets and asks for only a few
       pictures a second (for the step checks). No detections for 2 s: the hub detects again.
  Glasses send {"type":"device","battery","charging","volume","model","firmware","app","ip"}
       and command "wake" when tapped while asleep. {"type":"sleep","on":bool} from the app.
  {"type":"calib","on":bool}  lens calibration screen. config carries "calib":[x0,y0,x1,y1],
       the part of the camera frame that the lens display covers.

HTTP: GET / display app (web/dist, built from ui/) · GET /classic plain monitor · POST /api/kit {"kit":"60486" | "demo"} · /api/demo* teach the
demo steps with photos (GET, PUT, capture, forget, describe)
"""
from __future__ import annotations

import asyncio
import base64
import collections
import re

import cv2
import numpy as np
import json
import subprocess
import os
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, detector, locator, voice
from .checker import FULL, answer_question, check_step, confirm_done, mock_mode, work_area
from .guide import Guide, load_guide
from .memory import Memory
from .lego import KITS, build_kit
from .state import Screen, Session, Verdict, clip
from .visual import prepare, visuals

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KIT = KITS / "60486" / "guide.json"
FALLBACK_GUIDE = ROOT / "guides" / "lego-car.json"
MAX_PARALLEL = int(os.environ.get("MAVERICK_PARALLEL", "4"))   # checks in flight
STAGGER = float(os.environ.get("MAVERICK_STAGGER", "0.45"))    # seconds between starts
SURE = 0.85            # one verdict this confident is enough; otherwise two in a row
STEADY = 0.7           # below this, a change of phase must be seen twice before it shows
SESSION_FILE = KITS / "_session.json"  # which kit and step, so a restart resumes
INTRO_PEEK_MS = 4500   # how long the "where it goes" drawing shows on a new step
HINT_HOLD = 3.5        # seconds before a hint may be reworded within the same phase
HINT_BACK = 2.5        # a hint must have been up this long before it may step backward
CAMERA = {"type": "config", "width": 1280, "height": 960, "quality": 60, "fps": 3}
RECORD = os.environ.get("MAVERICK_RECORD")  # folder: save every checked frame + verdict
KEEP_FRAMES = os.environ.get("MAVERICK_KEEP_FRAMES") == "1"  # also save every camera frame
LOC_PARALLEL = int(os.environ.get("MAVERICK_LOCATE_PARALLEL", "4"))  # locates in flight
LOC_STAGGER = float(os.environ.get("MAVERICK_LOCATE_STAGGER", "0.35"))
CALIB_FILE = KITS / "_calib.json"       # which part of the camera frame the lens covers
DEFAULT_CALIB = [0.33, 0.30, 0.67, 0.64]
DEMO = KITS / "demo"                    # the short demo, taught with photos
DETECT_FPS = int(os.environ.get("MAVERICK_DETECT_FPS", "8"))  # camera rate with a detector
PICK_CONF, KEEP_CONF = 0.55, 0.35       # show a box at this confidence; keep it down to this
STEADY_HITS, STEADY_FOR = 3, 0.3        # frames and seconds in one place before a box is drawn
STEADY_SECONDS = 1.2                    # a "just put it there" step is done after this long
NEAR, APART = 0.08, 0.35                # gap between part and base, in base lengths
SETTLE_SECONDS = 0.9                    # the part must sit still on the base this long
VOTE_SECONDS = 4.0                      # how far back "done" looks count
CONFIRM_PARALLEL, CONFIRM_STAGGER = 3, 0.33   # quick confirmations in flight; gap between
FOREIGN_CHECK = False   # the "remove the foreign object" test: dropped from the demo
GLASSES_UPLOAD_FPS = 3     # pictures a second sent to the hub when the glasses detect
GLASSES_DETS_FRESH = 2.0   # seconds without detections from the glasses before the hub steps in
SURE_BIG = 0.9      # how sure a full "done" must be for the big parts (hood, rear, roof)
NUDGE_AFTER = 6.0   # seconds of "looks done but not confirmed" before asking for a clear view
DONE_SPREAD = 1.5   # seconds of footage the agreeing "done" looks must cover
STEP_GRACE = 1.5                        # seconds into a step before "done" can count
CALM = float(os.environ.get("MAVERICK_CALM", "0.04"))  # picture change on the build, 0..1
SEEK_SECONDS = 12                       # how long a "where is it?" answer stays on the lens
COLLECT = KITS / "demo" / "train" / "live"  # one camera frame a second, for retraining
COLLECT_ON = os.environ.get("MAVERICK_COLLECT", "1") == "1"


def saved_session() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        return {}


def saved_calib() -> dict:
    """{"eye": "right", "left": [x0, y0, x1, y1], "right": [...]}. The lens lines up with
    the camera a little differently for each eye, so each eye keeps its own alignment.
    An older file holding a single box is used for both eyes."""
    setup = {"eye": "right", "left": list(DEFAULT_CALIB), "right": list(DEFAULT_CALIB)}
    try:
        saved = json.loads(CALIB_FILE.read_text())
    except (OSError, ValueError):
        return setup
    if isinstance(saved, list) and len(saved) == 4:
        setup["left"], setup["right"] = list(saved), list(saved)
    elif isinstance(saved, dict):
        for eye in ("left", "right"):
            if isinstance(saved.get(eye), list) and len(saved[eye]) == 4:
                setup[eye] = saved[eye]
        if saved.get("eye") in ("left", "right"):
            setup["eye"] = saved["eye"]
    return setup


PREFS_FILE = KITS / "_prefs.json"
# What the wearer set in the phone app. camera/mic/save are their consent: with camera off
# no picture is looked at, with mic off nothing is listened to, and pictures are only kept
# on disk when "save" is on.
# volume (0-100), sleep_after (minutes without activity before the glasses sleep; 0 = never),
# dnd (no sounds or speech) and name are the device controls the glasses' own app has.
DEFAULT_PREFS = {"brightness": 1.0, "voice": True, "guidance": "full", "camera": True,
                 "mic": True, "save": False, "onboarded": False, "volume": 100,
                 "sleep_after": 30, "dnd": False, "name": "Maverick glasses",
                 "detect": "hub"}   # "glasses": the part detector runs on the glasses themselves


def saved_prefs() -> dict:
    prefs = dict(DEFAULT_PREFS)
    try:
        saved = json.loads(PREFS_FILE.read_text())
        prefs.update({k: v for k, v in saved.items() if k in prefs})
    except (OSError, ValueError):
        pass
    return prefs


def lan_addresses() -> list[str]:
    """This Mac's IPv4 addresses on real networks (Wi-Fi, hotspot, cable), newest first."""
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)", out)
    return [a for a in found if not a.startswith(("127.", "169.254."))]


def glasses_model_meta() -> dict:
    """What the glasses need to know about the model served to them (see detector export)."""
    try:
        return json.loads((KITS / "demo" / "glasses" / "meta.json").read_text())
    except (OSError, ValueError):
        return {}


def demo_guide() -> Guide:
    path = DEMO / "guide.json"
    if path.exists():
        return load_guide(path)
    return Guide(title="Demo build", kit="demo", steps=[
        {"id": n, "say": ""} for n in range(1, 6)])


def save_demo(guide: Guide) -> None:
    DEMO.mkdir(parents=True, exist_ok=True)
    (DEMO / "guide.json").write_text(guide.model_dump_json(indent=2))


def first_guide() -> Guide:
    chosen = os.environ.get("MAVERICK_GUIDE")
    if chosen:
        return load_guide(chosen)
    demo = DEMO / "guide.json"  # the demo is the app now; kits are only loaded on request
    if demo.exists() and any(step.say.strip() for step in load_guide(demo).steps):
        return load_guide(demo)
    last = KITS / str(saved_session().get("kit", "")) / "guide.json"
    if saved_session().get("kit") and last.exists():
        return load_guide(last)
    return load_guide(DEFAULT_KIT if DEFAULT_KIT.exists() else FALLBACK_GUIDE)


SEND_TIMEOUT = 3.0   # seconds one client may take to accept a message


class Client:
    def __init__(self, ws: WebSocket, role: str, lite: bool = False):
        self.ws, self.role, self.lock, self.lite = ws, role, asyncio.Lock(), lite
        self.host = ws.client.host if ws.client else ""
        self.dead = False

    async def send(self, message: dict, droppable: bool = False) -> None:
        if self.dead:
            return
        if self.lite and message.get("type") in ("frame", "dets", "ar"):
            return  # the phone app follows the build, it does not need the camera
        if droppable and self.lock.locked():
            return  # a slow viewer skips frames instead of slowing everyone down
        try:
            async with self.lock:
                if self.dead:
                    return
                await asyncio.wait_for(self.ws.send_json(message), SEND_TIMEOUT)
        except asyncio.TimeoutError:
            # A viewer that stopped reading (a closed tab, a dead link) must not hold up the
            # glasses and everyone else: every broadcast waits for all its sends.
            self.dead = True
            hub.clients.discard(self)
            print(f"[hub] dropped a {self.role} that stopped reading ({self.host})")
            try:
                await asyncio.wait_for(self.ws.close(), 1)
            except Exception:
                pass
        except Exception:
            pass


class Hub:
    def __init__(self):
        self.session = Session(first_guide())
        saved = saved_session()
        if saved.get("kit") and saved.get("kit") == self.session.guide.kit:
            self.session.goto(int(saved.get("index", 0)))
        self.clients: set[Client] = set()
        self.screen: Screen = self.session.step_screen()
        self.shown_index = -1
        self.latest_frame: str | None = None
        self.frame_seq = self.checked_seq = self.applied_seq = 0
        self.checks: set[asyncio.Task] = set()
        self.last_check_start = 0.0
        self.parse_task: asyncio.Task | None = None
        self.auto = True
        self.roi = FULL
        self.camera: dict = {}
        self.last_status: dict = {"type": "status", "checking": False, "mock": mock_mode()}
        self.parse_state: dict = {"type": "parse", "busy": False}
        # AR highlight: where the part (or its place) is, for the box on the lens.
        self.ar_on = True
        self.frame_gseq: int | None = None   # the glasses' own number for latest_frame
        self.locates: set[asyncio.Task] = set()
        self.last_locate_start = 0.0
        self.located_seq = self.loc_applied = self.loc_misses = 0
        self.ar_shown = False
        self.eyes: dict = saved_calib()
        self.eye: str = self.eyes["eye"]              # the wearer's dominant eye
        self.calib: list[float] = self.eyes[self.eye]
        self.calibrating = False
        # "setup": the lens is asking which eye is dominant. "run": the build is on.
        self.stage = "run"
        self.prefs: dict = saved_prefs()
        self.heading: tuple[float, float, float] | None = None  # (when, dx, dy) toward the rear
        self.last_image = None
        self.run_began = time.time()
        self.run_ended: float | None = None
        self.run_stats = {"caught": 0, "asked": 0}
        self.device: dict = {}                # battery, volume... as the glasses report them
        # On-glasses detection: when the glasses last sent their own detections, and how
        # long one took there. Without fresh ones the hub detects for itself.
        self.addresses: list[str] = lan_addresses()
        self.glasses_dets_at = 0.0
        self.detect_mode_sent = False         # last "glasses are detecting" state told to clients
        self.glasses_detect_ms = 0.0
        self.glasses_detect_fps = 0.0
        self.last_found: list[dict] = []
        self.hub_found: tuple[float, list[dict]] = (0.0, [])   # the hub's own last look
        self.extras_shown = False             # an extra box or arrow is on the lens
        self.asleep = False                   # display and camera off until a tap
        self.touched_at = time.monotonic()    # last sign of the wearer doing something
        # Trained detector (guides/kits/<kit>/model.pt): boxes every frame on this Mac.
        self.detector: detector.Detector | None = None
        self.detector_loading = False
        self.detecting = False
        self.detected_seq = 0
        self.detect_fps = 0.0
        self.shown: dict[str, float] = {}     # target id -> last time it was drawn
        self.relevant_at = 0.0                # last time this step's parts were in view
        self.candidates: dict[str, dict] = {}  # target id -> where it has been seen lately
        self.memory = Memory()                # where each part was last seen
        self.seek: dict | None = None         # the part the wearer asked "where is it?" about
        # (time, gap between part and base or None, part's place on the base or None)
        self.track: collections.deque = collections.deque(maxlen=60)
        self.votes: collections.deque = collections.deque(maxlen=8)  # (frame time, status)
        self.confirms: set[asyncio.Task] = set()
        self.confirm_at = 0.0
        self.noticed = False                  # "looks right, checking" is on the lens
        self.first_done_at = 0.0              # first sure "done" of this step
        self.nudged = False                   # asked once for a clear view of the part
        # Something that does not belong (a jar, a cap) put on the build.
        self.foreign: collections.deque = collections.deque(maxlen=6)  # (frame time, name)
        self.foreign_on = False               # the "remove it" screen is up
        self.full_mistake_at = 0.0            # last time the full check called a mistake
        self.foreign_box: tuple[float, list[float]] | None = None      # detector's sighting
        self.step_began = time.monotonic()
        self.motion: collections.deque = collections.deque(maxlen=40)  # (time, change 0..1)
        self.base_patch: np.ndarray | None = None
        self.focus = FULL                     # the build right now, for a close look
        self.focus_at = 0.0
        self.collected_at = 0.0
        self.steady_since = 0.0
        self.reset_coach()

    def reset_coach(self):
        self.done_streak = self.mistake_streak = self.warn_misses = 0
        self.phase, self.hint_text, self.hint_at, self.peek_at = "", "", 0.0, 0.0
        self.pending_phase = ""
        self.pending_count = 0
        if hasattr(self, "track"):
            self.track.clear()
            self.votes.clear()
            self.motion.clear()
            self.noticed = False
            self.first_done_at, self.nudged = 0.0, False
            if not getattr(self, "foreign_on", False):
                self.foreign.clear()
            if not getattr(self, "screen", None) or not self.screen.warn:
                self.step_began = time.monotonic()

    # ----- sending -----
    async def broadcast(self, message: dict, role: str | None = None, droppable: bool = False):
        if RECORD and message.get("type") != "frame":  # everything the hub says, for replays
            try:
                with (Path(RECORD) / "events.jsonl").open("a") as log:
                    log.write(json.dumps({"t": round(time.time(), 3), "to": role or "all",
                                          **message}) + "\n")
            except OSError:
                pass
        quiet = self.prefs["dnd"]
        if (quiet or not self.prefs["voice"]) and "say_url" in message:
            message = {k: v for k, v in message.items() if k != "say_url"}
        if quiet and "sound" in message:      # do not disturb: no beeps either
            message = {k: v for k, v in message.items() if k != "sound"}
        if (self.prefs["guidance"] == "minimal" and message.get("type") == "hint"
                and message.get("tone") != "warn" and message.get("text")):
            message = {**message, "text": ""}  # only warnings interrupt in minimal guidance
        targets = [c for c in self.clients if role is None or c.role == role]
        if targets:
            await asyncio.gather(*(c.send(message, droppable) for c in targets))

    def guide_message(self) -> dict:
        g = self.session.guide
        return {"type": "guide", "title": g.title, "kit": g.kit, "source": g.source,
                "steps": [s.model_dump() for s in g.steps]}

    def state_message(self) -> dict:
        return {"type": "state", "index": self.session.index, "finished": self.session.finished,
                "total": self.session.total, "auto": self.auto, "model": ai.CHECK_MODEL,
                "camera": self.camera.get("size", ""), "voice": voice.enabled(),
                "kit": self.session.guide.kit, "ar": self.ar_on, "calib": self.calib,
                "calibrating": self.calibrating, "locate_model": locator.MODEL,
                "stage": self.stage, "eye": self.eye, "prefs": self.prefs,
                "device": self.device, "asleep": self.asleep,
                "detect_by": "glasses" if self.on_glasses() else "hub",
                "link": self.link(), "addresses": self.addresses,
                "glasses_detect_ms": round(self.glasses_detect_ms),
                "glasses_detect_fps": round(self.glasses_detect_fps, 1),
                "run": self.run_summary(),
                "detector": bool(self.detector), "detect_ms": round(self.detector.ms)
                if self.detector else 0,
                "glasses": sum(1 for c in self.clients if c.role == "glasses")}

    def run_summary(self) -> dict:
        if self.session.finished and self.run_ended is None:
            self.run_ended = time.time()
        if not self.session.finished:
            self.run_ended = None
        return {"seconds": round((self.run_ended or time.time()) - self.run_began),
                **self.run_stats}

    def message(self, screen: Screen, intro: bool = False) -> dict:
        """Screen -> what the lens draws. Step screens are a thin strip plus a short peek."""
        message = screen.to_message()
        is_step = not self.session.finished and screen.top.startswith("STEP")
        message["mode"] = "step" if is_step else "info"
        if is_step:
            step = self.session.step
            message["top"] = f"{self.session.index + 1}/{self.session.total}"
            message["step"], message["total"] = self.session.index + 1, self.session.total
            message["big"] = message["big"].removeprefix("✓ ")
            message["bottom"] = ""
            try:
                message.update(visuals(step))
            except Exception as err:
                print(f"[visual] {err}")
            message["peek_ms"] = INTRO_PEEK_MS if intro else 0
            if intro and not screen.warn:
                clip_url = voice.cached_url(step.say)
                if clip_url:
                    message["say_url"] = clip_url
        elif self.session.finished and intro:
            clip_url = voice.cached_url(voice.FINISHED)
            if clip_url:
                message["say_url"] = clip_url
        return message

    async def show(self, screen: Screen | None, *, flash: str | None = None,
                   sound: str | None = None, intro: bool | None = None):
        if screen is None:
            return
        if screen.warn and not self.screen.warn:
            self.run_stats["caught"] += 1
        asyncio.get_event_loop().call_soon(lambda: asyncio.ensure_future(self.send_plan()))
        self.touched_at = time.monotonic()
        self.screen = screen
        marker = self.session.total if self.session.finished else self.session.index
        if intro is None:  # introduce a step (peek + voice) only when it is a new one
            intro = marker != self.shown_index and not screen.warn
        self.shown_index = marker
        try:
            SESSION_FILE.write_text(json.dumps({"kit": self.session.guide.kit,
                                                "index": self.session.index}))
        except OSError:
            pass
        if not screen.warn:
            self.reset_coach()
        if intro:
            await self.clear_ar()  # a new step: the old box no longer means anything
        message = self.message(screen, intro)
        if flash:
            message["flash"] = flash
        if sound:
            message["sound"] = sound
        await self.broadcast(message)
        await self.broadcast(self.state_message(), role="monitor")

    async def status(self, **fields):
        self.last_status = {"type": "status", "mock": mock_mode(), **fields}
        await self.broadcast(self.last_status, role="monitor")

    async def hint(self, text: str, tone: str = "info", *, phase: str = "", peek_ms: int = 0,
                   speak: bool = False):
        message = {"type": "hint", "text": text, "tone": tone, "phase": phase,
                   "peek_ms": peek_ms}
        if speak and text:
            url = await voice.say(text)
            if url:
                message["say_url"] = url
        self.hint_text, self.hint_at = text, time.monotonic()
        await self.broadcast(message)

    # ----- AR highlight -----
    def camera_config(self) -> dict:
        config = {**CAMERA, "calib": self.calib, "brightness": self.prefs["brightness"],
                  "volume": self.prefs["volume"], "sleep": self.asleep}
        if self.detector:  # a local detector can use many more frames per second
            config.update(fps=DETECT_FPS, quality=55)
        config["detect"] = "glasses" if self.wants_glasses() else "hub"
        # With their own detector running, the glasses also choose and draw the boxes
        # themselves (from the `plan`), so a box never waits on the network.
        config["boxes"] = "glasses" if self.wants_glasses() else "hub"
        # Every way to reach this hub: the USB tunnel first, then this Mac's addresses, so
        # the glasses carry on over Wi-Fi when the cable is pulled.
        config["hub_urls"] = ["ws://127.0.0.1:8000/ws"] + [f"ws://{a}:8000/ws" for a in self.addresses]
        meta = glasses_model_meta()
        config["detect_version"] = meta.get("version", 0)
        if self.on_glasses():
            # The glasses find the parts themselves; the hub only needs a few pictures a
            # second for the "is the step done" checks. A fraction of the bandwidth.
            config.update(fps=GLASSES_UPLOAD_FPS, quality=55)
            if self.link() == "wifi":
                config.update(fps=2, quality=45)   # lighter still over the air
        return config

    def link(self) -> str:
        """How the glasses reach the hub: "usb" (adb tunnel), "wifi", or "" (not connected)."""
        hosts = [c.host for c in self.clients if c.role == "glasses"]
        if not hosts:
            return ""
        return "usb" if all(h.startswith("127.") or h in ("::1", "localhost") for h in hosts) else "wifi"

    def wants_glasses(self) -> bool:
        """On Wi-Fi the glasses always find the parts themselves: eight pictures a second
        do not fit through a busy network, a few hundred bytes of boxes do."""
        return self.prefs["detect"] == "glasses" or self.link() == "wifi"

    def on_glasses(self) -> bool:
        """True while the glasses are detecting for themselves and it is working."""
        return (self.wants_glasses()
                and time.monotonic() - self.glasses_dets_at < GLASSES_DETS_FRESH)

    # ----- trained detector -----
    def load_detector(self):
        """(Re)load the kit's trained model in the background, if there is one."""
        path = detector.model_path(self.session.guide.kit)
        current = self.detector
        if self.detector_loading or (current and current.path == path and not current.stale()):
            return
        if path is None:
            if current:
                self.detector = None
                asyncio.create_task(self.broadcast(self.camera_config(), role="glasses"))
            return
        self.detector_loading = True

        async def load():
            try:
                self.detector = await asyncio.to_thread(detector.Detector, path)
                print(f"[detector] loaded {path} on {self.detector.device}: "
                      f"{sorted(self.detector.names.values())}")
            except Exception as err:
                print(f"[detector] could not load {path}: {type(err).__name__}: {err}")
                self.detector = None
            finally:
                self.detector_loading = False
            await self.broadcast(self.camera_config(), role="glasses")
            await self.broadcast(self.state_message(), role="monitor")

        asyncio.create_task(load())

    # ----- is the part really on, and has the builder let go? -----
    def apart(self) -> bool:
        """The detector sees the part clearly separate from the base: a real gap between
        them. (An attached part touches or overlaps the base's box.)"""
        now = time.monotonic()
        recent = [g for t, g, _ in self.track if now - t < 0.8 and g is not None]
        return len(recent) >= 3 and min(recent) > APART

    def still_on(self, seconds: float) -> bool:
        """The part has been touching the base, without moving on it, for this long.
        Stillness comes from the tracker: a part being adjusted keeps shifting."""
        now = time.monotonic()
        window = [(t, g, r) for t, g, r in self.track if now - t < seconds]
        on = [(t, r) for t, g, r in window if g is not None and g <= NEAR]
        if len(on) < 3 or len(on) < 0.7 * len(window) or now - on[0][0] < seconds * 0.75:
            return False
        xs, ys = [r[0] for _, r in on], [r[1] for _, r in on]
        return max(xs) - min(xs) < 0.10 and max(ys) - min(ys) < 0.10

    def settled(self) -> bool:
        return self.still_on(SETTLE_SECONDS)

    def calm(self) -> bool:
        """Nothing much has changed on the build for half a second: no hands at work."""
        now = time.monotonic()
        recent = [c for t, c in self.motion if now - t < 0.5]
        return len(recent) >= 3 and max(recent) < CALM

    def kick_confirm(self, frame: str, index: int):
        """While the base is in view and the part is not lying apart from it, keep asking
        for quick second opinions: a few in flight at once, a third of a second apart, so
        the first frame after the builder lets go gets its answer as soon as possible.
        (Running one at a time meant a fresh answer only every two seconds.)"""
        now = time.monotonic()
        self.confirms = {t for t in self.confirms if not t.done()}
        base_seen = now - self.focus_at < 0.5   # the build, under whatever name, is in view
        if (self.auto and len(self.confirms) < CONFIRM_PARALLEL
                and now - self.confirm_at >= CONFIRM_STAGGER
                and (not self.screen.warn or self.foreign_on) and base_seen
                and (self.foreign_on or not self.apart())):
            self.confirm_at = now
            self.confirms.add(asyncio.create_task(self.run_confirm(frame, index, now)))

    async def run_confirm(self, frame: str, index: int, frame_time: float):
        session = self.session
        area = self.focus if time.monotonic() - self.focus_at < 0.6 else FULL
        attached, confidence, foreign = await confirm_done(session.step, frame, area)
        if session is self.session and index == self.session.index:
            self.foreign.append((frame_time, foreign))
            if await self.watch_foreign(index):
                return
            self.votes.append((frame_time, "done" if attached and confidence >= 0.75 else "no",
                               "quick"))
            await self.maybe_finish(index)

    async def watch_foreign(self, index: int) -> bool:
        if not FOREIGN_CHECK:
            return False
        return await self._watch_foreign(index)

    async def _watch_foreign(self, index: int) -> bool:
        """Raise or clear the "remove the foreign object" screen. True while it is up.

        Raised when two looks at frames a moment apart both name something on the build
        that is not a part, or when the trained detector has just seen its known foreign
        object on the build. Cleared when two looks in a row see nothing wrong and the
        detector no longer sees it there."""
        now = time.monotonic()
        looks = sorted((v for v in self.foreign if now - v[0] < 3.0), key=lambda v: v[0])
        seen_by_detector = self.foreign_box is not None and now - self.foreign_box[0] < 0.6
        full_agrees = now - self.full_mistake_at < 3.0
        last_two = looks[-2:]
        both_bad = len(last_two) == 2 and all(v[1] for v in last_two) \
            and last_two[1][0] - last_two[0][0] >= 0.25
        both_clear = len(last_two) == 2 and not any(v[1] for v in last_two) \
            and last_two[1][0] - last_two[0][0] >= 0.25
        # Two independent signals must agree before the wearer is interrupted: the quick
        # look names something twice running, AND either the full check calls a mistake
        # or the trained detector sees its known foreign object on the build. On ordinary
        # frames the quick look alone named nearby things (eyeglasses, a jar on the table)
        # 5 times in 13, so it is never trusted by itself.
        if not self.foreign_on and not self.screen.warn and both_bad and (
                full_agrees or seen_by_detector):
            name = next((v[1] for v in reversed(looks) if v[1]), "object")
            self.foreign_on = True
            progress = self.session.index / self.session.total
            await self.show(Screen(top=self.session._top(), big="Remove the foreign object",
                                   sub=f"The {name} is not part of this build", warn=True,
                                   progress=progress), flash="wrong", sound="warn", intro=False)
            clip = voice.cached_url(voice.FOREIGN) or await voice.say(voice.FOREIGN)
            await self.broadcast({"type": "hint", "text": f"The {name} is not part of this build",
                                  "tone": "warn", "phase": "foreign", "peek_ms": 0,
                                  **({"say_url": clip} if clip else {})})
        elif self.foreign_on and both_clear and not seen_by_detector:
            self.foreign_on = False
            self.foreign.clear()
            await self.show(self.session.step_screen(), intro=False)
        return self.foreign_on

    async def maybe_finish(self, index: int):
        """Finish the step when the evidence agrees. The newest look must say done, and:
        - the tracker sees the part sitting still on the base: that one look is enough; or
        - the newest full check and the newest quick confirmation both say done (two
          different questions about recent frames, agreeing); or
        - with no quick confirmations running, the last three full checks all say done.
        Never while the tracker sees the part lying apart from the base.

        Measured on a real run: demanding three in a row over every look took 40 s on one
        step because the two kinds of look interleave and one "no" resets the count, while
        isolated wrong "done"s (3 of them on another step) never had the other kind agree."""
        if index != self.session.index or self.session.finished or self.screen.warn:
            return
        now = time.monotonic()
        # Looks are judged in the order their frames were taken, not the order answers arrive.
        votes = sorted((v for v in self.votes if now - v[0] < VOTE_SECONDS), key=lambda v: v[0])
        if not votes or votes[-1][1] != "done":
            return
        step = self.session.step
        guarded = bool(self.detector and step.cls and step.onto)
        if guarded and self.apart():
            return
        # The careful rule (2026-11-21, after steps were marked done while the part was
        # still being held over its place). The full check is the judge: it looks at the
        # manual drawings and at what the hands are doing. The quick look is only a second
        # opinion, and it flickers, so its "no" does not veto.
        #   - the newest full checks, in a row, say "done" and are sure (see on_verdict);
        #   - two of them at least 0.4 s of footage apart plus one quick "done" since just
        #     before the first, or three of them a second apart;
        #   - or, with the tracker seeing the part sitting still on the base, one sure
        #     full check plus one quick "done".
        # Nobody attaches a part in the first second and a half; a "done" there is the
        # previous step's finished look being misread.
        streak = []
        for vote in reversed([v for v in votes if v[2] == "full"]):
            if vote[1] != "done":
                break
            streak.append(vote)
        streak.reverse()
        streak = [v for v in streak if v[0] - self.step_began >= STEP_GRACE]
        spread = streak[-1][0] - streak[0][0] if streak else 0.0
        since = (streak[0][0] if streak else votes[-1][0]) - 1.0
        second = any(v[2] == "quick" and v[1] == "done" and v[0] >= since
                     and v[0] - self.step_began >= STEP_GRACE for v in votes)
        settled = guarded and self.settled()
        small = bool(getattr(step, "small", False))
        if small:
            fast = settled and len(streak) >= 1 and second and now - votes[-1][0] < 2.5
            agree = len(streak) >= 2 and spread >= 0.4 and second
            unanimous = len(streak) >= 3 and spread >= 1.0
        else:
            # Big parts (hood, rear, roof). A part held in place can look attached in a
            # single frame: on 2026-11-21 the red rear was marked done twice while only
            # being held against the chassis. Once on the quick look plus one full "done"
            # among ten "being held": so the quick look alone no longer leads here. Once on
            # full "done"s of 0.87, 0.94 and 0.86: so a full check must be 0.9 sure to
            # count for these parts (see on_verdict). Two such checks in a row, with a
            # quick "done" as second opinion. Three in a row was tried on the recorded run
            # and never happened even with the hood firmly on: the check flickers while
            # hands are on the car.
            fast = settled and len(streak) >= 2 and spread >= 0.3 and now - votes[-1][0] < 2.5
            agree = len(streak) >= 2 and spread >= 0.4 and second
            unanimous = len(streak) >= 3 and spread >= 1.0
        # Small parts (the minifigure): the full check wavers between "seated" and "lying
        # across" from one frame to the next while the close-up quick look is steady. So:
        # the last three quick looks all say done over at least a second of footage, a
        # sure full "done" exists from that same stretch, and no full check since has seen
        # the part still in the hand.
        quicks = [v for v in votes if v[2] == "quick"
                  and v[0] - self.step_began >= STEP_GRACE]
        run = []
        for vote in reversed(quicks):
            if vote[1] != "done":
                break
            run.append(vote)
        steady_quick = len(run) >= 3 and run[0][0] - run[-1][0] >= 1.0
        fulls = [v for v in votes if v[2] == "full" and v[0] >= (run[-1][0] - 1.0 if run else 0)]
        backed = any(v[1] == "done" for v in fulls) and not any(
            v[1] == "holding" for v in fulls)
        agree = agree or (small and steady_quick and backed)
        majority = agree
        if RECORD:
            with (Path(RECORD) / "finish.jsonl").open("a") as log:
                log.write(json.dumps({"t": round(now, 2), "step": index + 1,
                                      "votes": [f"{v[2][0]}:{v[1]}" for v in votes[-5:]],
                                      "streak": len(streak), "spread": round(spread, 2),
                                      "settled": self.settled(), "fast": fast,
                                      "majority": agree, "unanimous": unanimous}) + "\n")
        if (not (fast or agree or unanimous) and self.first_done_at and not self.nudged
                and now - self.first_done_at > NUDGE_AFTER):
            # It has looked finished for a while but the checks keep disagreeing, which
            # happens while hands cover the part. Say what would settle it.
            self.nudged = True
            await self.hint("Press it on, then let go so I can see it", "info",
                            phase="checking", speak=True)
        elif not (fast or agree or unanimous) and not self.noticed:
            # First sign it is finished: say so at once, so the wait is not a blank.
            self.noticed = True
            await self.hint("Looks right. Checking…", "ok", phase="checking")
        if fast or majority or unanimous:
            self.votes.clear()
            await self.show(self.session.apply_verdict(
                Verdict(status="done", phase="done", confidence=1.0), for_index=index),
                flash="done", sound="done")

    def detector_facts(self) -> str:
        step = self.session.step
        if not (self.detector and step.cls and step.onto):
            return ""
        name = locator.piece_name(step).lower()
        if self.apart():
            return f"the {name} is lying apart from the {step.onto}, not on it."
        if self.settled():
            return f"the {name} is touching the {step.onto} and holding still."
        return ""

    def steady(self, key: str, det: dict | None, now: float) -> dict | None:
        """Only pass a detection on once the same thing has been seen in about the same
        place for a few frames in a row. A lookalike that the detector picks for a frame
        or two (a drinks can for the hood) never gets drawn, and the box cannot hop
        between two objects: a new place has to earn its turn."""
        state = self.candidates.get(key)
        hold = 1.0 if self.on_glasses() else 0.5   # the glasses detect a few times a second
        if det is None:
            if state and now - state["seen"] > hold:
                self.candidates.pop(key, None)
            return state["det"] if state and state["shown"] and now - state["seen"] < hold else None
        cx, cy = (det["box"][0] + det["box"][2]) / 2, (det["box"][1] + det["box"][3]) / 2
        size = max(det["box"][2] - det["box"][0], det["box"][3] - det["box"][1])
        if state and abs(cx - state["cx"]) < max(0.08, size) and abs(cy - state["cy"]) < max(0.08, size):
            state.update(cx=cx, cy=cy, det=det, seen=now, hits=state["hits"] + 1)
            if state["hits"] >= STEADY_HITS and now - state["since"] >= STEADY_FOR:
                state["shown"] = True
        else:
            # Somewhere new. Keep showing the old box briefly while the new place proves itself.
            previous = state if state and state["shown"] and now - state["seen"] < hold - 0.1 else None
            self.candidates[key] = state = {"cx": cx, "cy": cy, "det": det, "seen": now,
                                            "since": now, "hits": 1, "shown": False}
            if previous:
                return previous["det"]
        return state["det"] if state["shown"] else None

    def kick_detect(self):
        if (self.detector and not self.detecting and self.ar_on and self.latest_frame
                and not self.calibrating and self.frame_seq > self.detected_seq):
            self.detecting, self.detected_seq = True, self.frame_seq
            asyncio.create_task(self.run_detect(self.frame_gseq, self.latest_frame))

    def plan(self) -> dict:
        """What to box for the current step, for glasses that detect and draw locally."""
        step, index = self.session.step, self.session.index
        show = (self.stage == "run" and not self.session.finished and not self.screen.warn
                and not self.asleep and self.prefs["camera"] and self.ar_on)
        pick = place = None
        if step.cls:
            pick = {"id": f"s{index + 1}-pick", "cls": step.cls, "label": locator.piece_name(step)}
        if step.onto:
            built = {step.onto} | {s.cls for s in self.session.guide.steps[:index] if s.cls}
            built.discard(step.cls)
            place = {"id": f"s{index + 1}-place", "classes": sorted(built), "label": "Attach here",
                     "where": step.where}
        now = time.monotonic()
        heading = None
        if self.heading and now - self.heading[0] < 20.0:
            heading = [round(self.heading[1], 3), round(self.heading[2], 3)]
        return {"type": "plan", "step": index + 1, "show": show, "pick": pick, "place": place,
                "heading": heading, "pick_conf": PICK_CONF, "keep_conf": KEEP_CONF}

    async def send_plan(self, force: bool = False):
        """Send the plan to the glasses when it has changed (step, warning, heading)."""
        plan = self.plan()
        key = json.dumps({**plan, "heading": None}, sort_keys=True)
        was = getattr(self, "_plan_sent", ("", None))
        old, new = was[1], plan["heading"]
        turned = (old is None) != (new is None) or (
            old is not None and new is not None
            and (abs(old[0] - new[0]) > 0.08 or abs(old[1] - new[1]) > 0.08))
        if force or key != was[0] or turned:
            self._plan_sent = (key, new)
            await self.broadcast(plan, role="glasses")

    async def sync_detect(self):
        """Tell the glasses (upload rate) and the pages (who is detecting) when detection
        moves between the glasses and the hub, in either direction."""
        mode = self.on_glasses()
        if mode != self.detect_mode_sent:
            self.detect_mode_sent = mode
            await self.broadcast(self.camera_config(), role="glasses")
            await self.broadcast(self.state_message(), role="monitor")

    async def glasses_dets(self, msg: dict):
        """Detections made on the glasses (ONNX model on the device): same use as the
        hub's own, without the picture having to travel first."""
        now = time.monotonic()
        gap = now - self.glasses_dets_at
        self.glasses_dets_at = now
        if 0 < gap < 2:
            self.glasses_detect_fps = 0.8 * self.glasses_detect_fps + 0.2 / gap
        self.glasses_detect_ms = 0.8 * self.glasses_detect_ms + 0.2 * float(msg.get("ms") or 0)
        if not self.wants_glasses():
            return
        await self.sync_detect()   # just took over: ask for fewer pictures
        found = [{"cls": str(d.get("cls")), "conf": float(d.get("conf", 0)),
                  "box": [float(v) for v in d.get("box", [0, 0, 0, 0])][:4], "by": "glasses"}
                 for d in (msg.get("found") or []) if len(d.get("box") or []) == 4]
        # Parts the glasses' small view missed but the hub's last full-size look found,
        # if that look is recent (they are usually lying still on the table).
        at, extra = self.hub_found
        if now - at < 0.8:
            have = {d["cls"] for d in found}
            found += [{**d, "by": "hub"} for d in extra if d["cls"] not in have]
        if not self.detecting and self.ar_on and not self.calibrating:
            self.detecting = True
            asyncio.create_task(self.run_detect(msg.get("seq"), None, found=found,
                                                took=float(msg.get("ms") or 0) / 1000))

    async def run_detect(self, gseq: int | None, frame: str | None,
                         found: list[dict] | None = None, took: float = 0.0):
        """Box one frame and send the AR targets for the step. `found` given: the glasses
        already detected on that frame; otherwise the hub's own model looks at `frame`."""
        try:
            started = time.perf_counter()
            watch = self.session.step.onto or self.session.step.cls
            remote = found is not None

            def work() -> list[dict]:
                if remote:
                    return found
                if self.on_glasses():
                    # The glasses are detecting. The few pictures that still arrive feed
                    # the memory of where parts were, the car's heading and the step
                    # checks, and the hub's full-size model looks at them for the parts
                    # too small for the glasses' low-resolution view (the minifigure).
                    seen, image = self.detector.detect_frame(frame)
                    self.last_image = image
                    self.hub_found = (time.monotonic(), seen)
                    if image is not None:
                        self.memory.update(image, seen)
                    return None
                seen, image = self.detector.detect_frame(frame)
                self.last_image = image
                if image is not None:
                    self.memory.update(image, seen)  # silent: only used when asked
                    # How much is the picture changing on the build itself? Hands fitting
                    # a part make it change a lot; letting go makes it calm.
                    base = detector.best(seen, watch, KEEP_CONF) if watch else None
                    if base:
                        h, w = image.shape[:2]
                        x0, y0, x1, y1 = base["box"]
                        crop = image[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
                        if crop.size:
                            patch = cv2.cvtColor(cv2.resize(crop, (64, 48)), cv2.COLOR_BGR2GRAY)
                            patch = patch.astype(np.float32) / 255
                            if self.base_patch is not None:
                                change = float(np.abs(patch - self.base_patch).mean())
                                self.motion.append((time.monotonic(), change))
                            self.base_patch = patch
                    else:
                        self.base_patch = None
                return seen

            found = await asyncio.to_thread(work)
            if found is None:
                return   # picture stored; the detections come from the glasses
            self.last_found = found
            if not remote:
                took = time.perf_counter() - started
            self.detect_fps = 0.8 * self.detect_fps + 0.2 / max(took, 1e-3)
            await self.broadcast({"type": "dets", "seq": gseq, "found": found,
                                  "ms": round(took * 1000), "by": "glasses" if remote else "hub",
                                  "motion": round(self.motion[-1][1], 4) if self.motion else None,
                                  "calm": self.calm(), "settled": self.settled(),
                                  "apart": self.apart()}, role="monitor", droppable=True)
            if remote and self.on_glasses():
                await self.send_plan()   # show:false when there is nothing to box
            if self.stage != "run":
                return
            if self.session.finished or self.screen.warn:
                if self.ar_shown:
                    self.ar_shown = True
                    await self.clear_ar()
                return
            step, index, now = self.session.step, self.session.index, time.monotonic()
            in_play = {step.cls, step.onto} | {s.cls for s in self.session.guide.steps[:index]}
            if any(d["cls"] in in_play for d in found):
                self.relevant_at = now
            targets = []
            pick_id, place_id = f"s{index + 1}-pick", f"s{index + 1}-place"
            floor = KEEP_CONF if now - self.shown.get(pick_id, 0) < 1.0 else PICK_CONF
            pick = detector.best(found, step.cls, floor) if step.cls else None
            pick = self.steady(pick_id, pick, now)
            name = locator.piece_name(step)
            if pick:
                self.shown[pick_id] = now
                targets.append({"id": pick_id, "box": pick["box"], "label": name, "sub": "",
                                "style": "pick", "by": pick.get("by", "hub")})
            floor = KEEP_CONF if now - self.shown.get(place_id, 0) < 1.0 else PICK_CONF
            # Where it goes is the build as it stands. Once parts are on, the detector tends
            # to call the whole car by the name of a part already attached ("rear", "hood"),
            # so take the biggest box among the base and every part from earlier steps.
            place = None
            if step.onto:
                built = {step.onto} | {s.cls for s in self.session.guide.steps[:index] if s.cls}
                built.discard(step.cls)
                on_build = [d for d in found if d["cls"] in built and d["conf"] >= floor]
                if on_build:
                    place = max(on_build, key=lambda d: (d["box"][2] - d["box"][0])
                                * (d["box"][3] - d["box"][1]))
            place = self.steady(place_id, place, now)
            if place:
                self.shown[place_id] = now
                self.find_heading(place["box"], now)
                targets.append({"id": place_id, "box": self.spot(step.where, place["box"], now),
                                "label": "Attach here", "sub": "", "style": "place",
                                "by": place.get("by", "hub")})
            # Is the part sitting on the base, apart from it, or out of sight?
            if step.onto:
                if pick and place:
                    cw = max(1e-6, place["box"][2] - place["box"][0])
                    ch = max(1e-6, place["box"][3] - place["box"][1])
                    rel = (((pick["box"][0] + pick["box"][2]) - (place["box"][0] + place["box"][2])) / 2 / cw,
                           ((pick["box"][1] + pick["box"][3]) - (place["box"][1] + place["box"][3])) / 2 / ch)
                    self.track.append((now, detector.gap(pick["box"], place["box"]), rel))
                else:
                    self.track.append((now, None, None))
            # Where to look closely: around the build as it is in this very frame (the base
            # and whatever is on or at it). Stale by the next frame, so it is redone each time.
            near = [d["box"] for d in found if d["cls"] in in_play and d["conf"] >= KEEP_CONF]
            base_boxes = [b for b in near if place and detector.gap(b, place["box"]) < 0.25]
            if place:
                area = place["box"]
                for box in base_boxes:
                    area = detector.union(area, box)
                self.focus, self.focus_at = detector.union(area, area, pad=0.4), now
            elif pick and step.onto:
                self.focus, self.focus_at = detector.union(pick["box"], pick["box"], pad=0.6), now
            if step.onto:
                self.kick_confirm(frame, index)
            # A known foreign object (taught like a part) sitting on the build: box it in
            # red while the "remove it" screen is up, and tell the watcher at once.
            intruder = detector.best(found, "foreign", PICK_CONF) if FOREIGN_CHECK else None
            if intruder and place and detector.gap(intruder["box"], place["box"]) < 0.04:
                self.foreign_box = (now, intruder["box"])
                if self.foreign_on:
                    targets = [t for t in targets if t["style"] != "pick"]
                    targets.append({"id": "foreign", "box": intruder["box"], "label": "Remove this",
                                    "sub": "", "style": "remove"})
            # "Where is the red rear?": box it if it is in view, otherwise point toward it.
            away = []
            if self.seek and now < self.seek["until"]:
                hit = detector.best(found, self.seek["cls"], KEEP_CONF)
                if hit:
                    self.seek.setdefault("found_at", now)
                    if not any(t["box"] == hit["box"] for t in targets):
                        targets.append({"id": f"seek-{self.seek['cls']}", "box": hit["box"],
                                        "label": self.seek["label"], "sub": "", "style": "pick"})
                    if now - self.seek["found_at"] > 3:
                        self.seek = None
                else:
                    pointer = self.memory.away(self.seek["cls"])
                    spot = self.memory.remembered_box(self.seek["cls"])
                    if pointer:      # out of view: an arrow at the edge
                        away.append({"label": self.seek["label"], **pointer})
                    elif spot:       # inside the view but not seen now: mark where it was
                        targets.append({"id": f"seek-{self.seek['cls']}-spot", "box": spot,
                                        "label": f"{self.seek['label']} was here", "sub": "",
                                        "style": "place"})
            elif self.seek:
                self.seek = None
            local = remote and self.on_glasses()   # the glasses draw pick/place themselves
            if local:
                await self.send_plan()
                # Extras for the lens: answers to "where is...?", and any pick/place box that
                # only the hub's full-size model found (a part too small for the glasses'
                # view): the glasses draw it when they have no box of their own for that id.
                extras = [t for t in targets if t["id"].startswith("seek-") or t["id"] == "foreign"
                          or t.get("by") == "hub"]
                message = {"type": "ar", "seq": gseq, "targets": targets, "away": away,
                           "ms": round(took * 1000)}
                await self.broadcast(message, role="monitor", droppable=True)
                if extras or away or self.extras_shown:
                    self.extras_shown = bool(extras or away)
                    await self.broadcast({**message, "targets": extras, "local": True},
                                         role="glasses")
                self.ar_shown = bool(targets or away)
            elif targets or away:
                self.ar_shown = True
                await self.broadcast({"type": "ar", "seq": gseq, "targets": targets,
                                      "away": away, "ms": round(took * 1000)})
            elif self.ar_shown and now - max(self.shown.values(), default=0) > 0.7:
                await self.clear_ar()
            # A step that only asks for the part to be put down is done once it sits steady.
            if step.cls and not step.onto:
                sure = detector.best(found, step.cls, 0.6)
                self.steady_since = (self.steady_since or now) if sure else 0.0
                if sure and now - self.steady_since >= STEADY_SECONDS \
                        and index == self.session.index:
                    self.steady_since = 0.0
                    await self.apply(Verdict(status="done", phase="done", confidence=1.0),
                                     index, force=True)
        except Exception as err:
            print(f"[detector] {type(err).__name__}: {err}")
        finally:
            self.detecting = False
            self.kick_detect()  # a newer frame may be waiting

    async def clear_ar(self):
        self.loc_misses = 0
        if self.ar_shown:
            self.ar_shown = False
            await self.broadcast({"type": "ar", "seq": self.frame_gseq, "targets": []})

    def kick_locate(self):
        self.locates = {t for t in self.locates if not t.done()}
        now = time.monotonic()
        if (self.ar_on and self.auto and self.latest_frame and not self.session.finished
                and not self.detector and not self.calibrating and not self.parse_state.get("busy")
                and not mock_mode() and len(self.locates) < LOC_PARALLEL
                and self.frame_seq > self.located_seq
                and now - self.last_locate_start >= LOC_STAGGER):
            self.located_seq, self.last_locate_start = self.frame_seq, now
            self.locates.add(asyncio.create_task(
                self.run_locate(self.frame_seq, self.frame_gseq, self.latest_frame)))

    async def run_locate(self, seq: int, gseq: int | None, frame: str):
        """Find the part to pick up, or the spot where it goes, in one frame.

        The answer is a second or so old when it lands, so it carries the glasses' own
        frame number: the glasses track the box forward from that frame to now.
        """
        session, index = self.session, self.session.index
        mode = "place" if self.phase in ("holding_part", "placing") else "pick"
        step = session.step
        started = time.perf_counter()
        box = await locator.locate(step, frame, mode, self.roi)
        ms = int((time.perf_counter() - started) * 1000)
        now_mode = "place" if self.phase in ("holding_part", "placing") else "pick"
        if (session is not self.session or index != self.session.index or mode != now_mode
                or seq < self.loc_applied or self.session.finished or self.screen.warn):
            return
        self.loc_applied = seq
        if RECORD:
            with (Path(RECORD) / "locates.jsonl").open("a") as log:
                log.write(json.dumps({"seq": seq, "gseq": gseq, "ms": ms, "step": index + 1,
                                      "mode": mode, "box": box}) + "\n")
        if not box:
            self.loc_misses += 1
            if self.loc_misses >= 3:
                await self.clear_ar()
            return
        self.loc_misses, self.ar_shown = 0, True
        name = locator.piece_name(step)
        target = {"id": f"s{index + 1}-{mode}", "box": [round(v, 4) for v in box],
                  "label": name if mode == "pick" else "Attach here", "sub": "", "style": mode}
        await self.broadcast({"type": "ar", "seq": gseq, "targets": [target], "ms": ms})

    # ----- lens calibration -----
    async def calibrate(self, action: str, rect: list | None = None):
        if action == "start":
            self.calibrating = True
            await self.clear_ar()
            await self.broadcast({"type": "calib", "on": True})
        elif action == "stop":
            self.calibrating = False
            await self.broadcast({"type": "calib", "on": False})
        elif action == "capture":
            box = await locator.find_calibration(self.latest_frame) if self.latest_frame else []
            if not box:
                await self.broadcast({"type": "calib", "on": True,
                                      "error": "Could not see the pink rectangle. Try again."})
                return
            await self.set_calib(box)
            self.calibrating = False
            await self.broadcast({"type": "calib", "on": False, "ok": True, "calib": box})
        elif action == "set" and rect and len(rect) == 4:
            await self.set_calib([float(v) for v in rect])
        await self.broadcast(self.state_message(), role="monitor")

    async def set_calib(self, box: list[float]):
        """Store the lens alignment for the eye in use, and send it to the glasses."""
        self.calib = [round(min(1.0, max(0.0, v)), 4) for v in box]
        self.eyes[self.eye] = self.calib
        self.save_eyes()
        await self.broadcast(self.camera_config(), role="glasses")

    def save_eyes(self):
        self.eyes["eye"] = self.eye
        try:
            CALIB_FILE.write_text(json.dumps(self.eyes))
        except OSError:
            pass

    # ----- setup happens in the phone app; the lens waits, then takes over -----
    def setup_screen(self) -> dict:
        return {"type": "screen", "mode": "info", "top": "", "big": "Finish setup on your phone",
                "sub": "Open Maverick to continue", "bottom": "", "warn": False, "progress": 0.0}

    def paused_screen(self) -> dict:
        return {"type": "screen", "mode": "info", "top": "", "big": "Camera paused",
                "sub": "Turn it back on in the Maverick app", "bottom": "", "warn": False,
                "progress": 0.0}

    async def sleep(self, on: bool):
        """Display and camera off (auto sleep, or "Sleep now" in the phone app); a tap on
        the glasses or the phone app wakes them where they left off."""
        if on == self.asleep:
            return
        self.asleep = on
        self.touched_at = time.monotonic()
        if on:
            self.latest_frame = None
            await self.clear_ar()
        await self.broadcast(self.camera_config(), role="glasses")
        if on:   # for a glasses app that does not know "sleep": at least a black lens
            await self.broadcast({"type": "screen", "mode": "info", "top": "", "big": "",
                                  "sub": "", "bottom": "", "warn": False, "progress": 0.0},
                                 role="glasses")
        else:
            await self.broadcast(self.lens_now())
        await self.broadcast(self.state_message(), role="monitor")

    def maybe_sleep(self):
        minutes = self.prefs["sleep_after"]
        if (minutes and not self.asleep and self.stage == "run"
                and time.monotonic() - self.touched_at > minutes * 60):
            asyncio.create_task(self.sleep(True))

    def lens_now(self) -> dict:
        """What the lens should be showing right now."""
        if self.stage == "setup":
            return self.setup_screen()
        if not self.prefs["camera"]:
            return self.paused_screen()
        return self.message(self.screen, intro=True)

    async def fresh_run(self):
        """Step 1, nothing remembered."""
        if self.asleep:
            await self.sleep(False)
        self.foreign_on, self.seek, self.heading = False, None, None
        self.session.command("restart")
        self.memory.reset()
        self.shown_index = -1
        self.screen = self.session.step_screen()
        self.reset_coach()
        self.run_began, self.run_ended = time.time(), None
        self.run_stats = {"caught": 0, "asked": 0}
        await self.clear_ar()

    async def start_setup(self):
        """The phone app is onboarding: the lens waits for it."""
        self.stage = "setup"
        await self.fresh_run()
        await self.broadcast(self.setup_screen())
        await self.broadcast(self.state_message(), role="monitor")

    async def start_run(self, handoff: bool = False):
        """Begin the build at step 1. From the phone it is a handoff: the lens says so."""
        await self.fresh_run()
        if handoff:
            self.stage = "setup"
            await self.set_prefs({"onboarded": True, "camera": True})
            await self.broadcast({"type": "screen", "mode": "info", "top": "", "bottom": "",
                                  "big": f"Set up for your {self.eye} eye",
                                  "sub": "Starting your build", "warn": False, "progress": 0.0})
            await asyncio.sleep(1.6)
        self.stage = "run"
        self.shown_index = -1
        self.run_began = time.time()
        await self.show(self.session.step_screen())
        await self.broadcast(self.state_message(), role="monitor")

    async def set_prefs(self, changes: dict):
        was_camera = self.prefs["camera"]
        for key, value in changes.items():
            if key not in DEFAULT_PREFS:
                continue
            if key == "brightness":
                value = round(min(1.0, max(0.05, float(value))), 2)
            elif key == "volume":
                value = int(min(100, max(0, float(value))))
            elif key == "sleep_after":
                value = int(value) if int(value) in (0, 10, 15, 30) else 30
            elif key == "name":
                value = str(value).strip()[:40] or DEFAULT_PREFS["name"]
            elif key == "detect":
                value = "glasses" if value == "glasses" else "hub"
            elif key == "guidance":
                value = "minimal" if value == "minimal" else "full"
            else:
                value = bool(value)
            self.prefs[key] = value
        try:
            PREFS_FILE.write_text(json.dumps(self.prefs))
        except OSError:
            pass
        self.touched_at = time.monotonic()
        if "brightness" in changes or "volume" in changes or "detect" in changes:
            await self.broadcast(self.camera_config(), role="glasses")
        if self.prefs["camera"] != was_camera and self.stage == "run":
            if self.prefs["camera"]:
                self.shown_index = -1
                await self.show(self.screen, intro=False)
            else:
                self.latest_frame = None
                await self.clear_ar()
                await self.broadcast(self.paused_screen())
        await self.broadcast(self.state_message(), role="monitor")

    async def set_eye(self, eye: str, begin: bool = False):
        self.eye = eye
        self.calib = self.eyes[eye]
        self.save_eyes()
        await self.broadcast(self.camera_config(), role="glasses")
        await self.broadcast(self.state_message(), role="monitor")

    # ----- which part of the build: front, rear or middle -----
    def find_heading(self, box: list[float], now: float):
        """Which way the car points, from its lime block: on this chassis the lime bricks
        sit at the rear end, where the red rear goes. Remembered while hands cover it."""
        image = self.last_image
        if image is None:
            return
        h, w = image.shape[:2]
        crop = image[int(box[1] * h):int(box[3] * h), int(box[0] * w):int(box[2] * w)]
        if crop.size == 0:
            return
        lime = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (30, 110, 110), (55, 255, 255))
        count = int(lime.sum() // 255)
        if count < max(12, 0.004 * lime.size):
            return
        ys, xs = np.nonzero(lime)
        dx, dy = xs.mean() / lime.shape[1] - 0.5, ys.mean() / lime.shape[0] - 0.5
        if max(abs(dx), abs(dy)) < 0.08:
            return  # lime in the middle of the box says nothing about direction
        if self.heading and now - self.heading[0] < 6.0:   # smooth, but follow a real turn
            dx, dy = 0.6 * self.heading[1] + 0.4 * dx, 0.6 * self.heading[2] + 0.4 * dy
        self.heading = (now, float(dx), float(dy))

    def spot(self, where: str, box: list[float], now: float) -> list[float]:
        """The part of the build's box where this step's part goes: the front half, the
        rear half, or the middle 30% along the car. The whole box if the direction the car
        points is not known yet."""
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        heading = self.heading if self.heading and now - self.heading[0] < 20.0 else None
        if where == "middle":
            # Along the car's length: its heading if known, else the longer side of the box.
            if heading:
                along_x = abs(heading[1]) * w >= abs(heading[2]) * h
            else:
                along_x = w * 4 >= h * 3   # the frame is 4:3, so compare in pixels
            if along_x:
                return [x0 + 0.35 * w, y0, x1 - 0.35 * w, y1]
            return [x0, y0 + 0.35 * h, x1, y1 - 0.35 * h]
        if where not in ("front", "rear") or not heading:
            return box
        dx, dy = heading[1], heading[2]          # toward the rear
        if where == "front":
            dx, dy = -dx, -dy
        big = max(abs(dx), abs(dy))
        out = [x0, y0, x1, y1]
        if abs(dx) >= 0.5 * big:                 # the car lies (partly) left to right
            if dx > 0: out[0] = x0 + 0.5 * w
            else: out[2] = x1 - 0.5 * w
        if abs(dy) >= 0.5 * big:                 # ... or (partly) up and down
            if dy > 0: out[1] = y0 + 0.5 * h
            else: out[3] = y1 - 0.5 * h
        return out

    # ----- camera checks -----
    def kick_check(self):
        self.checks = {t for t in self.checks if not t.done()}
        now = time.monotonic()
        # With a trained detector, only pay for a cloud check while this step's parts are in
        # view: it costs nothing when the camera is looking elsewhere or lying on the desk.
        watching = not self.detector or time.monotonic() - self.relevant_at < 2.5
        if (self.auto and watching and self.stage == "run" and self.latest_frame
                and not self.session.finished
                and not self.parse_state.get("busy") and len(self.checks) < MAX_PARALLEL
                and self.frame_seq > self.checked_seq and now - self.last_check_start >= STAGGER):
            self.checked_seq, self.last_check_start = self.frame_seq, now
            self.checks.add(asyncio.create_task(self.run_check(self.frame_seq, self.latest_frame)))

    async def run_check(self, seq: int, frame: str):
        """Check one frame. Up to MAX_PARALLEL run at once; late answers are dropped."""
        session, index, roi = self.session, self.session.index, self.roi
        started, frame_time = time.perf_counter(), time.monotonic()
        previous = session.guide.steps[index - 1] if index > 0 else None
        local = bool(self.detector)  # the detector supplies boxes and where to look
        if local:
            roi = self.focus if time.monotonic() - self.focus_at < 0.6 else FULL
        verdict = await check_step(session.step, frame, previous, roi,
                                   facts=self.detector_facts(), boxes=not local)
        ms = int((time.perf_counter() - started) * 1000)
        if session is not self.session or seq < self.applied_seq:
            return  # a newer frame was already judged, or a new kit was loaded
        self.applied_seq = seq
        # Follow the build: zoom the next check onto it, zoom back out if it is lost.
        if not local:
            self.roi = work_area(verdict.build_box)  # FULL when the model found no LEGO
        await self.status(checking=False, ms=ms, verdict=verdict.status, phase=verdict.phase,
                          seen=verdict.seen, confidence=round(verdict.confidence, 2),
                          step=index + 1, hint=verdict.hint, target=verdict.target,
                          box=verdict.box, roi=list(roi), zoomed=roi != FULL)
        if RECORD and self.prefs["save"]:
            folder = Path(RECORD)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{seq:06d}.jpg").write_bytes(base64.b64decode(frame))
            with (folder / "checks.jsonl").open("a") as log:
                log.write(json.dumps({"seq": seq, "ms": ms, "step": index + 1, "roi": roi,
                                      **verdict.model_dump()}) + "\n")
        await self.apply(verdict, index, frame_time=frame_time)

    async def apply(self, verdict: Verdict, index: int, force: bool = False,
                    frame_time: float | None = None):
        if index != self.session.index or self.session.finished:
            return
        sure = force or verdict.confidence >= SURE
        if verdict.status == "done":
            if force:
                await self.show(self.session.apply_verdict(verdict, for_index=index),
                                flash="done", sound="done")
                return
            small = bool(getattr(self.session.step, "small", False))
            sure = verdict.confidence >= (SURE if small else SURE_BIG)
            self.votes.append((frame_time or time.monotonic(), "done" if sure else "unsure",
                               "full"))
            if sure and not self.first_done_at:
                self.first_done_at = time.monotonic()
            self.mistake_streak = 0
            if sure and self.screen.warn and not self.foreign_on:
                # A warning was up and the step now looks finished: the mistake is gone.
                # Take the warning down at once, or it blocks the step from finishing (on
                # 2026-11-21 six sure "done"s in a row sat behind "Seat the minifigure
                # upright" for almost two minutes).
                self.warn_misses = 0
                began, votes = self.step_began, list(self.votes)
                await self.show(self.session.step_screen(), intro=False)
                # Putting the instruction back must not throw away what has been seen.
                self.step_began = began
                self.votes.extend(votes)
            await self.maybe_finish(index)
            return
        if verdict.phase != "no_build":   # the car was out of view: not a "no", just no news
            status = "holding" if verdict.phase == "holding_part" else verdict.status
            self.votes.append((frame_time or time.monotonic(), status, "full"))
            self.noticed = False
        if verdict.status == "mistake":
            self.full_mistake_at = time.monotonic()
            if await self.watch_foreign(index):
                return
            if any(name for t, name in self.foreign if time.monotonic() - t < 2.5):
                return  # probably a foreign object: let the two signals settle it
            self.mistake_streak += 1
            if self.mistake_streak >= 3 and not self.screen.warn and not self.foreign_on:
                await self.show(self.session.apply_verdict(verdict, for_index=index), sound="warn")
                asyncio.create_task(self.hint(verdict.message or "Check this step", "warn",
                                              phase="mistake", speak=True))
            return
        self.mistake_streak = 0
        if self.screen.warn and not self.foreign_on:
            self.warn_misses += 1
            if self.warn_misses >= 2:  # the mistake is gone: put the instruction back
                await self.show(self.session.step_screen(), intro=False)
            return
        await self.coach(verdict)

    async def coach(self, verdict: Verdict):
        """The live line under the instruction. It should read like one calm coach, not a
        ticker, so it changes only when the builder's situation really changes:
        - forward (looking for the part -> holding it -> placing it): as soon as one
          confident check says so;
        - backward, or to "nothing in view": only after two checks in a row agree and the
          current line has been up for a couple of seconds;
        - same situation: the words stay as they are, however the model rephrases them.
        Measured on a real run: without this the line changed ten times in twenty seconds
        on one step, twice going backward for a single check."""
        order = {"no_build": 0, "finding_part": 1, "holding_part": 2, "placing": 3}
        now, phase, text = time.monotonic(), verdict.phase, verdict.hint.strip()
        if phase not in order:
            return
        if phase == self.phase:
            self.pending_phase, self.pending_count = phase, 0
            if not self.hint_text and text and phase != "no_build":
                await self.hint(text, "ok" if order[phase] >= 2 else "info", phase=phase)
            return
        self.pending_count = self.pending_count + 1 if phase == self.pending_phase else 1
        self.pending_phase = phase
        forward = order[phase] > order.get(self.phase, 0)
        held = now - self.hint_at
        if forward:
            ready = (verdict.confidence >= STEADY or self.pending_count >= 2) and held >= 0.8
        else:
            ready = self.pending_count >= 2 and held >= HINT_BACK
        if not ready:
            return
        first_time_holding = order[phase] >= 2 and order.get(self.phase, 0) < 2
        self.phase, self.pending_count = phase, 0
        if phase == "no_build" or not text:
            if self.hint_text:
                await self.hint("", phase=phase)
            return
        peek = 0
        if first_time_holding and now - self.peek_at > 6:  # right part in hand: show where it goes
            peek, self.peek_at = 3500, now
        await self.hint(text, "ok" if order[phase] >= 2 else "info", phase=phase, peek_ms=peek)

    # ----- commands, voice, questions -----
    async def command(self, name: str):
        session = self.session
        if name == "calib_capture":
            await self.calibrate("capture")
            return
        self.touched_at = time.monotonic()
        if name == "wake":
            await self.sleep(False)
            return
        if name == "ptt":   # the glasses started listening (tap on the touchpad)
            await self.broadcast({"type": "heard", "text": "", "kind": "listening", "value": ""},
                                 role="monitor")
            return
        if self.asleep:
            return
        if name.startswith("choice_"):
            return  # the eye is chosen in the phone app now
        if self.stage != "run":
            return  # the lens is waiting for the phone app to finish setup
        if name == "peek":
            step = session.step
            self.peek_at = time.monotonic()
            await self.hint(clip(step.detail or step.say, 14), "info", phase="peek",
                            peek_ms=6000)
            url = await voice.say(step.detail or step.say)
            if url and session.step is step:
                await self.broadcast({"type": "hint", "text": self.hint_text, "tone": "info",
                                      "phase": "peek", "peek_ms": 0, "say_url": url})
            return
        screen = session.command(name)
        await self.show(screen, sound="done" if name == "next" and screen else None,
                        intro=True if name in ("repeat", "more") else None)

    def part_names(self) -> dict[str, str]:
        """Detector class -> the name people say: {"rear": "red rear", ...}."""
        return {s.cls: (s.piece or s.cls) for s in self.session.guide.steps if s.cls}

    def part_asked_about(self, question: str) -> str | None:
        low = question.lower()
        names = self.part_names()
        for cls, name in sorted(names.items(), key=lambda item: -len(item[1])):
            if name.lower() in low or re.search(rf"\b{re.escape(cls)}\b", low):
                return cls
        return None

    async def answer(self, question: str):
        self.run_stats["asked"] += 1
        names = self.part_names()
        cls = self.part_asked_about(question)
        if cls and re.search(r"\b(where|find|see|lost|locate)\b", question.lower()):
            # "Where is the red rear?" is answered from memory at once, and the lens then
            # points at it (or boxes it) for a few seconds. It never points unasked.
            text = self.memory.describe(cls, names[cls])
            label = names[cls][:1].upper() + names[cls][1:]
            self.seek = {"cls": cls, "label": label, "until": time.monotonic() + SEEK_SECONDS}
            await self.broadcast({"type": "heard", "text": question, "kind": "where",
                                  "value": cls}, role="monitor")
        else:
            await self.hint("…", "info", phase="asking")
            text = await answer_question(self.session.step, question, self.latest_frame,
                                         context=self.memory.facts(names))
        await self.hint(text, "ok", phase="answer", speak=True)
        self.hint_at = time.monotonic() + 6  # keep the answer up for a while

    async def heard(self, pcm16: str, rate: int, direct: bool = False):
        """direct: the wearer tapped the touchpad to make Maverick listen, so what follows is
        meant for it and needs no "Maverick" in front. Hands-free speech still does."""
        text = await voice.transcribe(pcm16, rate)
        kind, value = voice.understand(text, direct=direct)
        if direct and kind == "ignore":
            await self.hint("Didn't catch that. Tap and try again.", "info", phase="voice")
        if kind != "ignore":
            self.touched_at = time.monotonic()
        await self.broadcast({"type": "heard", "text": text, "kind": kind, "value": value},
                             role="monitor")
        if kind == "command":
            await self.command(value)
        elif kind == "ask":
            await self.answer(value)

    # ----- kit parser -----
    async def load_kit(self, kit: str, force: bool):
        async def progress(message: str, done: int, total: int):
            self.parse_state = {"type": "parse", "busy": True, "kit": kit, "message": message,
                                "done": done, "total": total}
            await self.broadcast(self.parse_state, role="monitor")
            await self.broadcast({**Screen(
                top=f"LEGO {kit}", big=message,
                sub=f"{done} of {total} pages" if total else "",
                progress=(done / total) if total else 0).to_message(), "mode": "info"},
                role="glasses")

        try:
            if kit.strip().lower() == "demo":
                guide = demo_guide()
                guide.steps = [s for s in guide.steps if s.say.strip()]
                if not guide.steps:
                    raise ValueError("Teach the demo steps first (Demo steps panel)")
                for n, step in enumerate(guide.steps, 1):
                    step.id = n
            else:
                await progress("Finding the booklet", 0, 0)
                guide = await build_kit(kit, progress, force=force)
            await asyncio.to_thread(prepare, guide)
        except Exception as err:
            self.parse_state = {"type": "parse", "busy": False, "kit": kit,
                                "error": str(err) or type(err).__name__}
            await self.broadcast(self.parse_state, role="monitor")
            await self.show(self.session.step_screen(), intro=False)
            return
        self.session, self.shown_index, self.roi = Session(guide), -1, FULL
        self.memory.reset()
        self.load_detector()
        asyncio.create_task(voice.prepare(guide))
        self.parse_state = {"type": "parse", "busy": False, "kit": kit,
                            "message": f"{len(guide.steps)} steps ready"}
        await self.broadcast(self.parse_state, role="monitor")
        await self.broadcast(self.guide_message(), role="monitor")
        await self.show(self.session.step_screen())


hub = Hub()
prepare(hub.session.guide)
app = FastAPI(title="Maverick")


async def keep_links():
    """Tell the glasses the hub is alive (so they notice a dead cable within seconds) and
    notice when this Mac changes network (so they learn its new address while still on
    the cable)."""
    last_check = 0.0
    while True:
        await asyncio.sleep(1.5)
        glasses = [c for c in hub.clients if c.role == "glasses"]
        if glasses:
            await asyncio.gather(*(c.send({"type": "ping"}, droppable=True) for c in glasses))
        if time.monotonic() - last_check > 5:
            last_check = time.monotonic()
            now = await asyncio.to_thread(lan_addresses)
            if now and now != hub.addresses:
                hub.addresses = now
                await hub.broadcast(hub.camera_config(), role="glasses")
                await hub.broadcast(hub.state_message(), role="monitor")


@app.on_event("startup")
async def start_keep_links():
    asyncio.create_task(keep_links())


@app.on_event("startup")
async def warm_up():
    asyncio.create_task(voice.prepare(hub.session.guide))
    hub.load_detector()

    async def watch_model():  # pick up a freshly trained model without a restart
        while True:
            await asyncio.sleep(5)
            hub.load_detector()

    asyncio.create_task(watch_model())


DIST = ROOT / "web" / "dist"  # the display app, built from ui/ with `npm run build`


@app.get("/")
async def display():
    built = DIST / "index.html"
    return FileResponse(built if built.exists() else ROOT / "web" / "monitor.html")


@app.get("/app")
async def phone_app():
    """The Maverick phone app (shown inside the Android shell in phone-app/)."""
    return FileResponse(DIST / "index.html")


@app.get("/classic")
async def classic_monitor():
    return FileResponse(ROOT / "web" / "monitor.html")


@app.get("/fake")
async def fake_glasses():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/health")
async def health():
    return {"ok": True, "mock": mock_mode(), "guide": hub.session.guide.title,
            "steps": hub.session.total, "model": ai.CHECK_MODEL, "voice": voice.enabled(),
            "detector": bool(hub.detector),
            "detect_ms": round(hub.detector.ms) if hub.detector else None}


class KitRequest(BaseModel):
    kit: str
    force: bool = False


@app.post("/api/kit")
async def load_kit(request: KitRequest):
    if hub.parse_task and not hub.parse_task.done():
        return JSONResponse({"ok": False, "error": "Already reading a manual"}, status_code=409)
    hub.parse_task = asyncio.create_task(hub.load_kit(request.kit, request.force))
    return {"ok": True}


# ----- demo steps, taught with photos from the glasses camera -----
class DemoCapture(BaseModel):
    step: int                 # 0-based
    kind: str = "refs"        # "refs" = the part, "done_refs" = the finished step
    index: int | None = None  # for delete


@app.get("/api/demo")
async def get_demo():
    return demo_guide().model_dump()


@app.put("/api/demo")
async def put_demo(guide: Guide):
    guide.kit = "demo"
    for n, step in enumerate(guide.steps, 1):
        step.id = n
    save_demo(guide)
    if hub.session.guide.kit == "demo":  # live edit: keep the builder on their step
        index = hub.session.index
        fresh = Guide.model_validate(guide.model_dump())
        fresh.steps = [s for s in fresh.steps if s.say.strip()]
        if fresh.steps:
            hub.session = Session(fresh)
            hub.session.goto(index)
            asyncio.create_task(voice.prepare(fresh))
            await hub.broadcast(hub.guide_message(), role="monitor")
            await hub.show(hub.session.step_screen(), intro=False)
    return guide.model_dump()


@app.post("/api/demo/capture")
async def capture_demo(request: DemoCapture):
    """Save what the glasses see right now as a photo of the part, or of the finished step."""
    if not hub.latest_frame:
        return JSONResponse({"error": "No camera frame yet"}, status_code=409)
    guide = demo_guide()
    if not 0 <= request.step < len(guide.steps) or request.kind not in ("refs", "done_refs"):
        return JSONResponse({"error": "No such step"}, status_code=400)
    frame = hub.latest_frame
    if request.kind == "refs":  # keep just the part, so it is a clean reference
        try:
            frame = await locator.crop_to_subject(frame)
        except Exception as err:
            print(f"[demo] crop failed: {err}")
    photos = DEMO / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    name = f"{request.kind}-{request.step + 1}-{int(time.time() * 1000) % 10_000_000}.jpg"
    (photos / name).write_bytes(base64.b64decode(frame))
    getattr(guide.steps[request.step], request.kind).append(f"kits/demo/photos/{name}")
    save_demo(guide)
    return guide.model_dump()


@app.post("/api/demo/forget")
async def forget_demo_photo(request: DemoCapture):
    guide = demo_guide()
    if 0 <= request.step < len(guide.steps) and request.kind in ("refs", "done_refs"):
        photos = getattr(guide.steps[request.step], request.kind)
        if request.index is not None and 0 <= request.index < len(photos):
            photos.pop(request.index)
            save_demo(guide)
    return guide.model_dump()


@app.post("/api/demo/describe")
async def describe_demo_step(request: DemoCapture):
    """Write the step's words from its photos, so teaching is point, click, done."""
    guide = demo_guide()
    if not 0 <= request.step < len(guide.steps):
        return JSONResponse({"error": "No such step"}, status_code=400)
    step = guide.steps[request.step]
    previous = guide.steps[request.step - 1] if request.step > 0 else None
    try:
        words = await locator.describe_step(step, previous)
    except Exception as err:
        return JSONResponse({"error": f"{type(err).__name__}: {err}"}, status_code=502)
    for field, value in words.items():
        setattr(step, field, value)
    save_demo(guide)
    return guide.model_dump()


KITS.mkdir(parents=True, exist_ok=True)
app.mount("/guides", StaticFiles(directory=ROOT / "guides"), name="guides")
app.mount("/web", StaticFiles(directory=ROOT / "web"), name="web")
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.websocket("/ws")
async def join(ws: WebSocket):
    await ws.accept()
    role = ws.query_params.get("role", "glasses")
    me = Client(ws, role, lite=ws.query_params.get("lite") == "1")
    hub.clients.add(me)
    if role == "glasses":
        await hub.broadcast(hub.camera_config(), role="glasses")   # the link may have changed
        await hub.send_plan(force=True)
    await me.send(hub.lens_now())
    if role == "monitor":
        await me.send(hub.last_status)
        await me.send(hub.guide_message())
        await me.send(hub.parse_state)
    await hub.broadcast(hub.state_message(), role="monitor")
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            session = hub.session
            if kind == "frame":
                if not hub.prefs["camera"] or hub.asleep:
                    continue  # paused by the wearer, or asleep: the picture is dropped unseen
                hub.maybe_sleep()
                await hub.sync_detect()
                hub.latest_frame = msg.get("jpeg")
                hub.frame_gseq = msg.get("seq")
                hub.frame_seq += 1
                if RECORD and KEEP_FRAMES and hub.prefs["save"] and hub.latest_frame:
                    folder = Path(RECORD) / "frames"
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / f"{time.time():.3f}.jpg").write_bytes(
                        base64.b64decode(hub.latest_frame))
                asyncio.create_task(hub.broadcast(
                    {"type": "frame", "jpeg": hub.latest_frame, "from": role},
                    role="monitor", droppable=True))
                if (COLLECT_ON and hub.prefs["save"] and hub.latest_frame
                        and time.monotonic() - hub.collected_at >= 1.0):
                    hub.collected_at = time.monotonic()
                    COLLECT.mkdir(parents=True, exist_ok=True)
                    (COLLECT / f"f{int(time.time() * 10)}.jpg").write_bytes(
                        base64.b64decode(hub.latest_frame))
                hub.kick_detect()
                hub.kick_check()
                hub.kick_locate()
            elif kind == "calib":
                asyncio.create_task(hub.calibrate(msg.get("action", ""), msg.get("rect")))
            elif kind == "ar":
                hub.ar_on = bool(msg.get("on"))
                if not hub.ar_on:
                    hub.ar_shown = True
                    await hub.clear_ar()
                await hub.broadcast(hub.state_message(), role="monitor")
            elif kind == "command":
                if RECORD:
                    with (Path(RECORD) / "events.jsonl").open("a") as log:
                        log.write(json.dumps({"t": round(time.time(), 3), "from": role,
                                              "type": "command", "name": msg.get("name", "")}) + "\n")
                asyncio.create_task(hub.command(msg.get("name", "")))
            elif kind == "audio" and (not hub.prefs["mic"] or hub.asleep):
                pass  # the wearer turned the microphone off: nothing is transcribed
            elif kind == "audio":
                # The lens now shows "Thinking…" (after a tap) or its listening dot; so does the page.
                await hub.broadcast({"type": "heard", "text": "", "value": "",
                                     "kind": "thinking" if msg.get("ptt") else "waiting"},
                                    role="monitor")
                asyncio.create_task(hub.heard(msg.get("pcm16", ""), int(msg.get("rate", 16000)),
                                              direct=bool(msg.get("ptt"))))
            elif kind == "hello":
                hub.camera = {"size": msg.get("size", ""), "sizes": msg.get("sizes", [])}
                await hub.broadcast(hub.state_message(), role="monitor")
            elif kind == "setup":         # the page's "Restart demo" button: step 1, now
                await hub.start_run()
            elif kind == "onboarding":    # the phone app is setting up: the lens waits
                await hub.start_setup()
            elif kind == "start":         # the phone app hands the build to the glasses
                asyncio.create_task(hub.start_run(handoff=True))
            elif kind == "wifi" and role != "glasses":
                # "Turn Wi-Fi on" / "Join this network" from the phone app. Goes straight to
                # the glasses and nowhere else: it can carry a password, so it is neither
                # logged nor kept.
                for client in [c for c in hub.clients if c.role == "glasses"]:
                    await client.send({k: v for k, v in msg.items()
                                       if k in ("type", "enable", "join", "forget")})
            elif kind == "dets" and role == "glasses":   # detections made on the glasses
                await hub.glasses_dets(msg)
            elif kind == "dets_error":    # the glasses could not run the model: hub detects
                print(f"[glasses detector] {msg.get('text')}")
                await hub.set_prefs({"detect": "hub"})
            elif kind == "device":        # battery, charging, volume... from the glasses
                changed = {k: v for k, v in msg.items() if k != "type"}
                if changed != hub.device:
                    hub.device = changed
                    await hub.broadcast(hub.state_message(), role="monitor")
            elif kind == "sleep":         # "Sleep now" / "Wake" in the phone app
                await hub.sleep(bool(msg.get("on", True)))
            elif kind == "prefs":         # brightness, voice, guidance, camera, mic, save
                await hub.set_prefs(msg.get("values") or {})
            elif kind == "eye":
                if msg.get("value") in ("left", "right"):
                    await hub.set_eye(msg["value"])
            elif kind == "goto":
                await hub.show(session.goto(int(msg.get("index", 0))))
            elif kind == "auto":
                hub.auto = bool(msg.get("on"))
                await hub.broadcast(hub.state_message(), role="monitor")
            elif kind == "debug":
                verdict = Verdict(status=msg.get("verdict", "not_yet"),
                                  message=(session.step.common_mistakes or ["Wrong part"])[0])
                await hub.apply(verdict, session.index, force=True)
            elif kind == "ask":
                asyncio.create_task(hub.answer(msg.get("text", "")))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.clients.discard(me)
        await hub.broadcast(hub.state_message(), role="monitor")
