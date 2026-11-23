"""Test bench for detection ON the glasses, without the glasses.

    uv run python glasses_sim.py                      every model in guides/kits/demo/glasses/
    uv run python glasses_sim.py n320q p416q          just these
    uv run python glasses_sim.py --run <folder>       another recorded run (frames/<unix>.jpg)

For each model it answers the three things that decide whether it can go live:

  1. Does it see the parts?   The recorded frames of a real run on the glasses go through
     exactly what the glasses app does (frame resized into the top of the model's input,
     grey padding, one thread, decode, per-class non-max suppression, the thresholds in the
     model's meta), and the boxes are compared with the hub's full-size model on the same
     frames ("the reference").
  2. How fast will it be on the glasses?   Time on this Mac, one thread, multiplied by a
     factor measured on the real device (glasses_calibration.json: models that were timed on
     the glasses themselves, 8-bit and floating point kept apart). A prediction, not a
     measurement: it is only as good as the calibration points.
  3. What would the wearer see?   The run is replayed at its real pace with a detector that
     takes that long: it starts on the newest frame whenever it is free, and its boxes show
     up late by its own latency. "live" is the share of moments at which the box on screen
     still overlaps where the part really is, with no tracking to help (the on-device
     tracker only makes this better).

Budget: a model passes with predicted latency <= 200 ms and live >= 60 % on the build.
"""
from __future__ import annotations

import bisect
import glob
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import onnx
import onnxruntime as ort

from maverick import detector as D

KIT = D.KITS / "demo"
MODELS = KIT / "glasses"
HERE = Path(__file__).resolve().parent
CALIBRATION = HERE / "glasses_calibration.json"
BUDGET_MS, BUDGET_LIVE = 200, 0.60
BUILD = ("chassis", "hood", "rear")          # whichever of these is biggest is "the build"
PARTS = ("hood", "rear", "minifigure", "roof")


# ---------- exactly what the glasses app does to a frame ----------
def prepare(image: np.ndarray, meta: dict) -> np.ndarray:
    w, h, ch = meta["width"], meta["height"], meta["content_height"]
    small = cv2.resize(image, (w, ch), interpolation=cv2.INTER_NEAREST)   # the app subsamples
    canvas = np.full((h, w, 3), meta.get("pad_value", 114), np.uint8)
    canvas[:ch] = small
    return canvas[..., ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0


def decode(out: np.ndarray, meta: dict) -> list[dict]:
    out = out[0]                                   # (4 + classes, anchors)
    scores = out[4:]
    best = scores.argmax(0)
    conf = scores.max(0)
    keep = conf >= meta.get("conf", 0.35)
    cx, cy, bw, bh = (out[i][keep] for i in range(4))
    w, ch = meta["width"], meta["content_height"]
    boxes = np.stack([(cx - bw / 2) / w, (cy - bh / 2) / ch, (cx + bw / 2) / w, (cy + bh / 2) / ch], 1)
    boxes = boxes.clip(0, 1)
    found = []
    for cls in np.unique(best[keep]):
        index = np.nonzero(best[keep] == cls)[0]
        order = index[np.argsort(-conf[keep][index])]
        while len(order):
            first, rest = order[0], order[1:]
            found.append({"cls": meta["classes"][int(cls)], "conf": float(conf[keep][first]),
                          "box": [float(v) for v in boxes[first]]})
            if not len(rest):
                break
            order = rest[[iou(boxes[first], boxes[k]) < meta.get("iou", 0.5) for k in rest]]
    return found


def iou(a, b) -> float:
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


# ---------- the recorded run and the reference ----------
def load_run(folder: Path) -> list[tuple[float, Path]]:
    frames = sorted((float(Path(p).stem), Path(p)) for p in glob.glob(str(folder / "frames" / "*.jpg")))
    window = folder.parent / (folder.name + "_t0.txt")
    if window.exists():                             # the stretch that is one clean run
        t0, t1 = map(float, window.read_text().split())
        frames = [(t, p) for t, p in frames if t0 <= t <= t1 + 2]
    return frames


def reference(frames: list[tuple[float, Path]], folder: Path) -> list[list[dict]]:
    cache = folder / "reference.json"
    if cache.exists():
        saved = json.loads(cache.read_text())
        if len(saved) == len(frames):
            return saved
    model = D.Detector(KIT / "model.pt")
    ref = [model.detect_array(cv2.imread(str(p))) for _, p in frames]
    cache.write_text(json.dumps(ref))
    return ref


def build_box(found: list[dict]) -> list[float] | None:
    boxes = [d["box"] for d in found if d["cls"] in BUILD and d["conf"] >= 0.5]
    return max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if boxes else None


def part_box(found: list[dict], cls: str) -> list[float] | None:
    boxes = [d for d in found if d["cls"] == cls and d["conf"] >= 0.5]
    return max(boxes, key=lambda d: d["conf"])["box"] if boxes else None


# ---------- speed ----------
def kind_of(path: Path) -> str:
    ops = {n.op_type for n in onnx.load(str(path), load_external_data=False).graph.node}
    return "int8" if ops & {"QLinearConv", "QuantizeLinear", "ConvInteger"} else "float"


def mac_ms(session: ort.InferenceSession, meta: dict) -> float:
    x = np.random.rand(1, 3, meta["height"], meta["width"]).astype(np.float32)
    for _ in range(5):
        session.run(None, {meta["input"]: x})
    times = []
    for _ in range(25):
        t = time.perf_counter()
        session.run(None, {meta["input"]: x})
        times.append(time.perf_counter() - t)
    return float(np.median(times) * 1000)


def calibration() -> dict:
    """{"placement": "...", "points": [{"model": "<name in glasses/>", "device_ms": 906}]}:
    models that were timed on the real glasses. Their time on this Mac is measured here, in
    the same sitting as the candidates, so a busy Mac skews both sides alike."""
    try:
        return json.loads(CALIBRATION.read_text())
    except (OSError, ValueError):
        return {"placement": "uncalibrated", "points": []}


def factors(cal: dict, known: dict[str, dict]) -> dict[str, float]:
    """Device time / Mac time, per kind of model ("float", "int8")."""
    ratios: dict[str, list[float]] = {}
    for point in cal.get("points", []):
        meta = known.get(point["model"])
        if not meta:
            continue
        path = MODELS / meta["file"]
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        ratios.setdefault(kind_of(path), []).append(point["device_ms"] / mac_ms(session, meta))
    return {kind: float(np.median(values)) for kind, values in ratios.items()}


# ---------- what the wearer would see ----------
def live(times: list[float], mine: list[list[dict]], ref: list[list[dict]], latency: float):
    """Replay at real pace: the detector takes `latency` seconds per look, always on the
    newest frame. Returns (looks per second, share of moments the build's box on screen
    still overlaps the reference build box, same for the step's loose part)."""
    looks: list[tuple[float, int]] = []            # (when its result appears, frame it saw)
    clock = times[0]
    while clock < times[-1]:
        seen = bisect.bisect_right(times, clock) - 1
        clock += max(latency, 1e-3)
        looks.append((clock, seen))
    ready = [t for t, _ in looks]
    hits = {"build": [0, 0], "part": [0, 0]}
    for i, t in enumerate(times):
        k = bisect.bisect_right(ready, t) - 1
        shown = mine[looks[k][1]] if k >= 0 else []
        truth = build_box(ref[i])
        if truth:
            got = build_box(shown)
            hits["build"][1] += 1
            hits["build"][0] += bool(got and iou(got, truth) >= 0.4)
        for cls in PARTS:
            truth = part_box(ref[i], cls)
            if truth and (not build_box(ref[i]) or iou(truth, build_box(ref[i])) < 0.2):
                got = part_box(shown, cls)          # a loose part, away from the build
                hits["part"][1] += 1
                hits["part"][0] += bool(got and iou(got, truth) >= 0.3)
    rate = len(looks) / max(1e-6, times[-1] - times[0])
    return rate, hits["build"][0] / max(1, hits["build"][1]), hits["part"][0] / max(1, hits["part"][1])


def metas() -> dict[str, dict]:
    found = {}
    bench = MODELS / "bench.json"
    if bench.exists():
        for meta in json.loads(bench.read_text()):
            found[meta["name"]] = meta
    main = MODELS / "meta.json"
    if main.exists():
        meta = json.loads(main.read_text())
        found.setdefault(Path(meta["file"]).stem, meta)
    return {name: meta for name, meta in found.items() if (MODELS / meta["file"]).exists()}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    folder = Path(sys.argv[sys.argv.index("--run") + 1]) if "--run" in sys.argv else Path(
        os.environ.get("MAVERICK_SIM_RUN", ""))
    if "--run" in sys.argv:
        args = [a for a in args if a != str(folder)]
    frames = load_run(folder)
    if not frames:
        raise SystemExit("no recorded frames: pass --run <folder with frames/*.jpg>")
    times = [t for t, _ in frames]
    ref = reference(frames, folder)
    cal = calibration()
    scales = factors(cal, metas())
    images = [cv2.imread(str(p)) for _, p in frames]
    names = [n for n in metas() if not args or n in args]
    print(f"{len(frames)} frames over {times[-1] - times[0]:.0f} s from {folder.name}; "
          f"reference: hub model at 640.\nDevice placement assumed: {cal.get('placement')}; "
          f"device/Mac factors: " + (", ".join(f"{k} x{v:.0f}" for k, v in scales.items()) or "none"))
    print(f"{'model':8} {'input':9} {'kind':5} {'mac ms':>6} {'glasses ms':>10} {'looks/s':>7} "
          f"{'build':>6} {'part':>5} {'recall by part':40} {'extra':>5}  verdict")
    rows = []
    for name in names:
        meta = metas()[name]
        path = MODELS / meta["file"]
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        kind = kind_of(path)
        ms = mac_ms(session, meta)
        note = "" if kind in scales else (" (float factor)" if "float" in scales else " (no calibration)")
        scale = scales.get(kind, scales.get("float", 0.0))
        predicted = ms * scale
        mine = [decode(session.run(None, {meta["input"]: prepare(im, meta)})[0], meta) for im in images]
        recall, extra = {}, 0
        for a, b in zip(ref, mine):
            used = set()
            for d in (d for d in a if d["conf"] >= 0.5 and d["cls"] != "foreign"):
                k = next((j for j, e in enumerate(b) if j not in used and e["cls"] == d["cls"]
                          and iou(d["box"], e["box"]) >= 0.4), None)
                slot = recall.setdefault(d["cls"], [0, 0])
                slot[1] += 1
                if k is not None:
                    used.add(k)
                    slot[0] += 1
            extra += sum(1 for j, e in enumerate(b) if j not in used and e["cls"] != "foreign")
        rate, on_build, on_part = live(times, mine, ref, predicted / 1000 if predicted else 0.05)
        ok = predicted and predicted <= BUDGET_MS and on_build >= BUDGET_LIVE
        by_part = " ".join(f"{c[:4]} {v[0] / max(1, v[1]):.0%}" for c, v in sorted(recall.items()))
        print(f"{name:8} {meta['width']}x{meta['height']:<5} {kind:5} {ms:6.1f} "
              f"{predicted:8.0f}{note[:2]:2} {rate:7.1f} {on_build:6.0%} {on_part:5.0%} {by_part:40} "
              f"{extra:5}  {'PASS' if ok else 'no'}{note}")
        rows.append({"model": name, "kind": kind, "mac_ms": round(ms, 1),
                     "glasses_ms": round(predicted), "looks_per_s": round(rate, 1),
                     "live_build": round(on_build, 3), "live_part": round(on_part, 3),
                     "recall": {c: round(v[0] / max(1, v[1]), 3) for c, v in recall.items()},
                     "extra_boxes": extra, "pass": bool(ok)})
    (MODELS / "sim-results.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
