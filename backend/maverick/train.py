"""Teach a fast detector the demo's parts from a handful of photos.

    uv run python -m maverick.train ingest <photos or folders...>   HEIC, JPG, PNG
    uv run python -m maverick.train label [--redo]   box every part with the vision model
    uv run python -m maverick.train sheet            contact sheets of the labels, to check
    uv run python -m maverick.train cutouts          cut each part out of its photo (SAM)
    uv run python -m maverick.train backgrounds      collect surfaces to paste onto
    uv run python -m maverick.train synth [N]        paste cut-outs onto many surfaces
    uv run python -m maverick.train fit [epochs] [model]   train; writes kits/demo/model.pt
    uv run python -m maverick.train all <photos...>  everything above, in order
    uv run python -m maverick.train upgrade [extra frame folders...]
        retrain with real camera frames as "not a part" examples, and switch to the new
        model only if it gives fewer false boxes without losing the parts

Why it works from few photos: each part is cut out and pasted, at every angle, size and
lighting, onto hundreds of different surfaces, with other objects and finger-like shapes
in the way. The detector learns the part, not the table it was photographed on.
Everything lives in guides/kits/demo/train/.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import random
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel

from . import ai
from .guide import load_guide

KITS = Path(__file__).resolve().parents[2] / "guides" / "kits"
KIT = KITS / "demo"
TRAIN = KIT / "train"
RAW, CUTS, BACK, DATA = TRAIN / "raw", TRAIN / "cutouts", TRAIN / "backgrounds", TRAIN / "dataset"
LABELS = TRAIN / "labels.json"
LIVE, NEG = TRAIN / "live", TRAIN / "negatives"  # hub's saved frames; same, LEGO painted out
LABEL_MODEL = "gpt-5.6-luna"
SEE_THROUGH = {"roof": 0.72}  # glass: let some of the new surface show through


def classes() -> dict[str, str]:
    """Detector class -> what it looks like, taken from the demo guide's steps."""
    out: dict[str, str] = {}
    for step in load_guide(KIT / "guide.json").steps:
        if step.cls:
            out[step.cls] = step.find or step.piece
    extras = KIT / "extras.json"  # things to recognise that are not parts of the build
    if extras.exists():
        out.update(json.loads(extras.read_text()))
    return out


EXTRA = {  # how each class is boxed when parts are already joined
    "chassis": "Box the whole car body on its wheels, including any sections already "
               "attached to it.",
}


# ---------- 1. photos in ----------

def ingest(sources: list[str]) -> int:
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    RAW.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for source in sources:
        path = Path(source).expanduser()
        files += sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
    count = 0
    for path in files:
        if path.suffix.lower() not in (".heic", ".heif", ".jpg", ".jpeg", ".png", ".webp"):
            continue
        target = RAW / (path.stem.replace(" ", "_") + ".jpg")
        if target.exists():
            continue
        try:
            image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        except Exception as err:
            print(f"skip {path.name}: {err}")
            continue
        image.thumbnail((1920, 1920))
        image.save(target, "JPEG", quality=92)
        count += 1
    print(f"{count} new photos, {len(list(RAW.glob('*.jpg')))} in total")
    return count


# ---------- 2. label ----------

class Found(BaseModel):
    found: bool
    confidence: float
    box: list[float]


LOCATE = """Locate one object in the photo. You are told the photo's size in pixels.
box: [x0, y0, x1, y1] tight around the whole object, in pixels from the top-left corner.
found: false and box [] if the object is not in the photo. Never guess.
confidence: 0 to 1 that the box is on exactly that object."""


async def _label_one(path: Path, names: dict[str, str], gate: asyncio.Semaphore) -> list[dict]:
    picture, width, height = ai.for_boxes(base64.b64encode(path.read_bytes()).decode())

    async def one(cls: str, look: str) -> dict | None:
        async with gate:
            for attempt in range(3):
                try:
                    out = await ai.parse(LABEL_MODEL, [
                        ai.text(f"Object: {look}. {EXTRA.get(cls, '')} These are parts of one "
                                "LEGO car; other parts of it may be in the photo too."),
                        ai.text(f"Photo ({width} pixels wide, {height} pixels high):"),
                        ai.image(picture, "high")], Found, system=LOCATE, max_tokens=70,
                        effort="none", timeout=40)
                    break
                except Exception as err:
                    print(f"  retry {path.name} {cls}: {type(err).__name__}")
                    await asyncio.sleep(1 + attempt)
            else:
                return None
        box = ai.unit_box(out.box, width, height) if out and out.found else []
        if not box or out.confidence < 0.5:
            return None
        return {"cls": cls, "box": [round(v, 4) for v in box],
                "conf": round(out.confidence, 2)}

    found = await asyncio.gather(*(one(c, look) for c, look in names.items()))
    return [f for f in found if f]


async def label(redo: bool = False) -> dict:
    names = classes()
    labels: dict = json.loads(LABELS.read_text()) if LABELS.exists() and not redo else {}
    todo = [p for p in sorted(RAW.glob("*.jpg")) if p.name not in labels]
    gate = asyncio.Semaphore(10)
    results = await asyncio.gather(*(_label_one(p, names, gate) for p in todo))
    for path, found in zip(todo, results):
        labels[path.name] = found
    LABELS.write_text(json.dumps(labels, indent=1))
    counts = {c: sum(1 for boxes in labels.values() for b in boxes if b["cls"] == c)
              for c in names}
    print(f"labelled {len(todo)} photos; boxes per part: {counts}")
    return labels


COLOURS = {"chassis": (80, 200, 255), "hood": (255, 255, 255), "rear": (255, 80, 80),
           "minifigure": (255, 220, 60), "roof": (120, 255, 160)}


def sheet(out_dir: Path, per_sheet: int = 12) -> list[Path]:
    """Contact sheets with the boxes drawn, to check the labels by eye."""
    labels = json.loads(LABELS.read_text())
    out_dir.mkdir(parents=True, exist_ok=True)
    names = sorted(labels)
    sheets = []
    for start in range(0, len(names), per_sheet):
        canvas = Image.new("RGB", (4 * 480, 3 * 380), (20, 20, 20))
        for slot, name in enumerate(names[start:start + per_sheet]):
            image = cv2.imread(str(RAW / name))
            h, w = image.shape[:2]
            for b in labels[name]:
                x0, y0, x1, y1 = b["box"]
                colour = COLOURS.get(b["cls"], (255, 0, 255))[::-1]
                cv2.rectangle(image, (int(x0 * w), int(y0 * h)), (int(x1 * w), int(y1 * h)),
                              colour, max(3, w // 300))
                cv2.putText(image, f"{b['cls']} {b['conf']}", (int(x0 * w), max(30, int(y0 * h) - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, w / 900, colour, max(2, w // 500))
            cv2.putText(image, name[:28], (10, h - 14), cv2.FONT_HERSHEY_SIMPLEX, w / 1100,
                        (255, 255, 255), max(2, w // 600))
            tile = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
            tile.thumbnail((480, 380))
            canvas.paste(tile, ((slot % 4) * 480, (slot // 4) * 380))
        target = out_dir / f"labels-{start // per_sheet + 1:02d}.jpg"
        canvas.save(target, quality=82)
        sheets.append(target)
    print("\n".join(str(s) for s in sheets))
    return sheets


# ---------- 3. cut the parts out ----------

def cutouts() -> dict[str, int]:
    """One RGBA cut-out per labelled box, using SAM with the box as the prompt."""
    from ultralytics import SAM
    labels = json.loads(LABELS.read_text())
    sam = SAM("sam2.1_b.pt")
    shutil.rmtree(CUTS, ignore_errors=True)
    counts: dict[str, int] = {}
    for name, boxes in sorted(labels.items()):
        if not boxes:
            continue
        image = cv2.imread(str(RAW / name))
        h, w = image.shape[:2]
        pixel = [[b["box"][0] * w, b["box"][1] * h, b["box"][2] * w, b["box"][3] * h]
                 for b in boxes]
        try:
            result = sam(image, bboxes=pixel, verbose=False)[0]
        except Exception as err:
            print(f"skip {name}: {err}")
            continue
        if result.masks is None:
            continue
        for b, mask in zip(boxes, result.masks.data.cpu().numpy()):
            mask = (mask > 0.5).astype(np.uint8)
            if mask.shape != (h, w):
                mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
            ys, xs = np.where(mask)
            box_area = (b["box"][2] - b["box"][0]) * (b["box"][3] - b["box"][1]) * w * h
            if len(xs) < 400 or len(xs) < 0.2 * box_area:
                continue  # SAM grabbed a sliver: not a usable cut-out
            x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
            alpha = cv2.GaussianBlur(mask[y0:y1, x0:x1].astype(np.float32) * 255, (5, 5), 0)
            rgba = np.dstack([cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2RGB),
                              alpha.astype(np.uint8)])
            folder = CUTS / b["cls"]
            folder.mkdir(parents=True, exist_ok=True)
            counts[b["cls"]] = counts.get(b["cls"], 0) + 1
            Image.fromarray(rgba, "RGBA").save(folder / f"{Path(name).stem}-{counts[b['cls']]}.png")
    print(f"cut-outs per part: {counts}")
    return counts


# ---------- 3b. real scenes with the LEGO painted out ----------

class Boxes(BaseModel):
    boxes: list[list[float]]


FIND_ALL = """List every physical LEGO item in the photo: loose LEGO parts, LEGO \
sub-assemblies, LEGO models and LEGO minifigures, whether on the table or in a hand. Do not \
list pictures of LEGO on a box, a booklet or a screen, and do not list other objects.
boxes: one [x0, y0, x1, y1] per item, in pixels from the top-left corner. You are told the \
photo's size. [] if there is no LEGO in the photo."""


async def negatives(extra: list[str] | None = None, limit: int = 360) -> int:
    """Turn real camera frames into "nothing to find here" pictures.

    The detector was confusing things on the desk (a drinks can) with parts, because it
    had only ever seen the parts pasted onto invented scenes. Here every real LEGO item
    in a frame is found by the vision model and painted out, leaving the true scene: the
    desk, hands, cans, cables, the laptop showing the app. Those pictures are then used
    both as empty examples and as the surfaces the cut-outs are pasted onto.
    """
    from .detector import Detector, model_path
    NEG.mkdir(parents=True, exist_ok=True)
    frames = sorted(LIVE.glob("*.jpg"))
    for folder in extra or []:
        frames += sorted(Path(folder).expanduser().glob("*.jpg"))
    frames = [f for f in frames if not (NEG / f"{f.parent.name}-{f.stem}.jpg").exists()]
    frames = frames[:: max(1, len(frames) // limit)][:limit]
    current = model_path("demo")
    yolo = Detector(current) if current else None
    gate = asyncio.Semaphore(10)

    async def one(path: Path) -> bool:
        image = cv2.imread(str(path))
        if image is None or image.mean() < 28:   # lens covered or lights off: nothing to learn
            return False
        ok, encoded = cv2.imencode(".jpg", image)
        picture, width, height = ai.for_boxes(base64.b64encode(encoded).decode())
        async with gate:
            try:
                out = await ai.parse(LABEL_MODEL, [
                    ai.text(f"Photo ({width} pixels wide, {height} pixels high):"),
                    ai.image(picture, "high")], Boxes, system=FIND_ALL, max_tokens=300,
                    effort="none", timeout=40)
            except Exception as err:
                print(f"  skip {path.name}: {type(err).__name__}")
                return False
        boxes = [b for b in (ai.unit_box(box, width, height) for box in (out.boxes if out else []))
                 if b]
        if yolo:  # anything the current detector is very sure about is a real part too
            boxes += [d["box"] for d in yolo.detect_array(image) if d["conf"] >= 0.8]
        h, w = image.shape[:2]
        smooth = cv2.GaussianBlur(image, (0, 0), sigmaX=w / 14)
        for x0, y0, x1, y1 in boxes:
            px, py = (x1 - x0) * 0.18 + 0.01, (y1 - y0) * 0.18 + 0.01
            a, b = int(max(0, x0 - px) * w), int(max(0, y0 - py) * h)
            c, d = int(min(1, x1 + px) * w), int(min(1, y1 + py) * h)
            image[b:d, a:c] = smooth[b:d, a:c]
        cv2.imwrite(str(NEG / f"{path.parent.name}-{path.stem}.jpg"),
                    cv2.resize(image, (960, 720)), [cv2.IMWRITE_JPEG_QUALITY, 88])
        return True

    made = sum(await asyncio.gather(*(one(f) for f in frames)))
    print(f"{made} new scene pictures, {len(list(NEG.glob('*.jpg')))} in total")
    return made


async def add_class(name: str, photos: list[str]) -> int:
    """Teach one more thing from a few photos that each show only that thing: box it,
    cut it out, and leave every other cut-out exactly as it is."""
    from ultralytics import SAM
    before = {p.name for p in RAW.glob("*.jpg")}
    ingest(photos)
    fresh = sorted(p for p in RAW.glob("*.jpg") if p.name not in before)
    look = classes()[name]
    labels = json.loads(LABELS.read_text()) if LABELS.exists() else {}
    gate = asyncio.Semaphore(8)
    found = await asyncio.gather(*(_label_one(p, {name: look}, gate) for p in fresh))
    sam = SAM("sam2.1_b.pt")
    made = 0
    for path, boxes in zip(fresh, found):
        labels[path.name] = boxes[:1]
        if not boxes:
            continue
        image = cv2.imread(str(path))
        h, w = image.shape[:2]
        b = boxes[0]["box"]
        result = sam(image, bboxes=[[b[0] * w, b[1] * h, b[2] * w, b[3] * h]], verbose=False)[0]
        if result.masks is None:
            continue
        mask = (result.masks.data[0].cpu().numpy() > 0.5).astype(np.uint8)
        if mask.shape != (h, w):
            mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
        ys, xs = np.where(mask)
        if len(xs) < 400:
            continue
        x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
        alpha = cv2.GaussianBlur(mask[y0:y1, x0:x1].astype(np.float32) * 255, (5, 5), 0)
        rgba = np.dstack([cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2RGB), alpha.astype(np.uint8)])
        (CUTS / name).mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgba, "RGBA").save(CUTS / name / f"{path.stem}.png")
        made += 1
    LABELS.write_text(json.dumps(labels, indent=1))
    print(f"{name}: {len(fresh)} photos, {made} cut-outs")
    return made


def _held_out(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """(for training, kept aside for testing): every eighth scene is never trained on."""
    return ([p for i, p in enumerate(paths) if i % 8], [p for i, p in enumerate(paths) if not i % 8])


# ---------- 4. surfaces ----------

def _texture(rng: random.Random, w: int = 960, h: int = 720) -> np.ndarray:
    """A made-up surface: plain, graded, wood-like, cloth-like, speckled or tiled."""
    base = np.array([rng.randint(20, 245) for _ in range(3)], np.float32)
    kind = rng.choice(["plain", "grade", "wood", "noise", "tiles", "cloth"])
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    image = np.ones((h, w, 3), np.float32) * base
    if kind == "grade":
        angle = rng.uniform(0, math.tau)
        ramp = (xx * math.cos(angle) + yy * math.sin(angle)) / max(w, h)
        image += (ramp[..., None] - 0.5) * rng.uniform(40, 140)
    elif kind == "wood":
        angle = rng.uniform(-0.3, 0.3)
        grain = np.sin((yy * math.cos(angle) + xx * math.sin(angle)) / rng.uniform(3, 14)
                       + np.sin(xx / rng.uniform(60, 200)) * rng.uniform(1, 4))
        image += grain[..., None] * rng.uniform(6, 26) * np.array([1.0, 0.8, 0.5])
    elif kind == "noise":
        small = np.random.default_rng(rng.randint(0, 1 << 30)).normal(0, 1, (h // 8, w // 8, 3))
        image += cv2.resize(small.astype(np.float32), (w, h)) * rng.uniform(10, 45)
    elif kind == "tiles":
        size = rng.randint(30, 160)
        checks = ((xx // size + yy // size) % 2)[..., None]
        image += (checks - 0.5) * rng.uniform(15, 90)
    elif kind == "cloth":
        weave = np.sin(xx / rng.uniform(1.5, 5)) * np.sin(yy / rng.uniform(1.5, 5))
        image += weave[..., None] * rng.uniform(5, 22)
    image += np.random.default_rng(rng.randint(0, 1 << 30)).normal(0, rng.uniform(1, 7), (h, w, 3))
    return np.clip(image, 0, 255).astype(np.uint8)


def backgrounds(photos: int = 160) -> int:
    """Real photos of anything (random stock photos), as extra surfaces and clutter."""
    import httpx
    BACK.mkdir(parents=True, exist_ok=True)
    have = len(list(BACK.glob("*.jpg")))
    with httpx.Client(follow_redirects=True, timeout=20) as http:
        for n in range(have, photos):
            try:
                response = http.get(f"https://picsum.photos/seed/surface{n}/960/720")
                response.raise_for_status()
                (BACK / f"photo-{n:03d}.jpg").write_bytes(response.content)
            except Exception as err:
                print(f"background {n}: {type(err).__name__}")
    total = len(list(BACK.glob("*.jpg")))
    print(f"{total} background photos")
    return total


# ---------- 5. paste the parts onto surfaces ----------

def _warp(cut: np.ndarray, rng: random.Random, long_side: int) -> np.ndarray:
    """Resize, rotate (any angle), tilt and re-light one RGBA cut-out."""
    h, w = cut.shape[:2]
    scale = long_side / max(h, w)
    cut = cv2.resize(cut, (max(2, int(w * scale)), max(2, int(h * scale))),
                     interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    if rng.random() < 0.5:
        cut = cut[:, ::-1]
    h, w = cut.shape[:2]
    side = int(math.hypot(h, w)) + 8
    padded = np.zeros((side, side, 4), np.uint8)
    oy, ox = (side - h) // 2, (side - w) // 2
    padded[oy:oy + h, ox:ox + w] = cut
    matrix = cv2.getRotationMatrix2D((side / 2, side / 2), rng.uniform(0, 360), 1.0)
    matrix[0, 1] += rng.uniform(-0.15, 0.15)   # a little shear = a different viewing angle
    matrix[1, 1] *= rng.uniform(0.75, 1.0)     # squash = seen from lower down
    padded = cv2.warpAffine(padded, matrix, (side, side), flags=cv2.INTER_LINEAR)
    rgb = padded[..., :3].astype(np.float32)
    rgb = (rgb - 128) * rng.uniform(0.75, 1.3) + 128 + rng.uniform(-35, 35)
    rgb *= np.array([rng.uniform(0.9, 1.1), rng.uniform(0.95, 1.05), rng.uniform(0.9, 1.1)])
    padded[..., :3] = np.clip(rgb, 0, 255)
    ys, xs = np.where(padded[..., 3] > 20)
    if len(xs) == 0:
        return padded
    return padded[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def _paste(canvas: np.ndarray, cut: np.ndarray, x: int, y: int, opacity: float = 1.0) -> np.ndarray:
    """Paste RGBA `cut` at (x, y). Returns the pasted alpha mask at canvas size."""
    H, W = canvas.shape[:2]
    h, w = cut.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    mask = np.zeros((H, W), np.float32)
    if x1 <= x0 or y1 <= y0:
        return mask
    part = cut[y0 - y:y1 - y, x0 - x:x1 - x]
    alpha = part[..., 3:4].astype(np.float32) / 255 * opacity
    canvas[y0:y1, x0:x1] = (canvas[y0:y1, x0:x1] * (1 - alpha) + part[..., :3] * alpha)
    mask[y0:y1, x0:x1] = part[..., 3] / 255
    return mask


def _shadow(canvas: np.ndarray, cut: np.ndarray, x: int, y: int, rng: random.Random) -> None:
    alpha = cut[..., 3].astype(np.float32) / 255
    blur = rng.choice([9, 15, 25, 41])
    soft = cv2.GaussianBlur(alpha, (blur, blur), 0) * rng.uniform(0.25, 0.65)
    shade = np.dstack([np.zeros((*soft.shape, 3), np.uint8), (soft * 255).astype(np.uint8)])
    _paste(canvas, shade, x + rng.randint(-14, 14), y + rng.randint(2, 18))


def _distractor(rng: random.Random) -> np.ndarray:
    """Something that is not one of our parts: a coloured block, or a finger-like blob."""
    w, h = rng.randint(30, 260), rng.randint(20, 160)
    cut = np.zeros((h, w, 4), np.uint8)
    if rng.random() < 0.45:   # finger / hand
        colour = rng.choice([(224, 172, 140), (198, 134, 98), (141, 85, 54), (255, 205, 170)])
        cv2.ellipse(cut, (w // 2, h // 2), (w // 2 - 1, h // 2 - 1), 0, 0, 360, (*colour, 255), -1)
    else:                     # brick-like block with studs
        colour = tuple(rng.randint(0, 255) for _ in range(3))
        cv2.rectangle(cut, (0, 0), (w - 1, h - 1), (*colour, 255), -1)
        lighter = tuple(min(255, c + 30) for c in colour)
        for sx in range(12, w - 8, 24):
            for sy in range(12, h - 8, 24):
                cv2.circle(cut, (sx, sy), 7, (*lighter, 255), -1)
    return cut


def synth(count: int = 1500, seed: int = 7) -> int:
    rng = random.Random(seed)
    names = list(classes())
    cuts = {c: [np.array(Image.open(p).convert("RGBA")) for p in sorted((CUTS / c).glob("*.png"))]
            for c in names}
    cuts = {c: v for c, v in cuts.items() if v}
    if not cuts:
        raise SystemExit("No cut-outs. Run: label, then cutouts.")
    photos = sorted(BACK.glob("*.jpg"))
    scenes, _ = _held_out(sorted(NEG.glob("*.jpg")))
    out_images, out_labels = TRAIN / "synth" / "images", TRAIN / "synth" / "labels"
    shutil.rmtree(TRAIN / "synth", ignore_errors=True)
    out_images.mkdir(parents=True)
    out_labels.mkdir(parents=True)
    W, H = 960, 720
    for n in range(count):
        kind = rng.random()
        if scenes and kind < 0.55:     # a real frame from the camera, LEGO painted out
            canvas = cv2.cvtColor(cv2.imread(str(rng.choice(scenes))), cv2.COLOR_BGR2RGB)
            canvas = cv2.resize(canvas, (W, H))
            if rng.random() < 0.5:
                canvas = canvas[:, ::-1].copy()
        elif photos and kind < 0.8:
            canvas = cv2.cvtColor(cv2.imread(str(rng.choice(photos))), cv2.COLOR_BGR2RGB)
            canvas = cv2.resize(canvas, (W, H))
            if rng.random() < 0.5:  # blur it: a surface, out of focus, behind the parts
                k = rng.choice([5, 9, 15])
                canvas = cv2.GaussianBlur(canvas, (k, k), 0)
        else:
            canvas = _texture(rng, W, H)
        canvas = canvas.astype(np.float32)
        placed: list[tuple[str, np.ndarray]] = []
        chosen = [c for c in cuts if rng.random() < 0.6] or [rng.choice(list(cuts))]
        if scenes and rng.random() < 0.15:
            chosen = []  # nothing to find: the scene on its own
        things = [("part", c) for c in chosen] + [("other", "") for _ in range(rng.randint(0, 4))]
        rng.shuffle(things)
        zoom = rng.uniform(0.5, 1.6)  # camera near or far, shared by everything in the shot
        for kind, cls in things:
            if kind == "other":
                cut = _warp(_distractor(rng), rng, rng.randint(40, 260))
                mask = _paste(canvas, cut, rng.randint(-40, W - 40), rng.randint(-40, H - 40))
                placed = [(c, np.clip(m - mask, 0, 1)) for c, m in placed]
                continue
            natural = {"chassis": 0.30, "hood": 0.17, "rear": 0.19, "roof": 0.15,
                       "minifigure": 0.075, "foreign": 0.085}.get(cls, 0.15)
            long_side = int(W * natural * zoom * rng.uniform(0.8, 1.25))
            cut = _warp(rng.choice(cuts[cls]), rng, max(24, min(long_side, 620)))
            h, w = cut.shape[:2]
            x, y = rng.randint(-w // 5, W - 4 * w // 5), rng.randint(-h // 5, H - 4 * h // 5)
            if rng.random() < 0.7:
                _shadow(canvas, cut, x, y, rng)
            mask = _paste(canvas, cut, x, y, SEE_THROUGH.get(cls, 1.0))
            placed = [(c, np.clip(m - mask, 0, 1)) for c, m in placed]
            placed.append((cls, mask))
        image = np.clip(canvas, 0, 255).astype(np.uint8)
        # The camera: exposure, colour cast, focus blur, motion blur, sensor noise, JPEG.
        image = np.clip(image.astype(np.float32) * rng.uniform(0.55, 1.35)
                        * np.array([rng.uniform(0.9, 1.12), 1.0, rng.uniform(0.85, 1.1)]), 0, 255)
        if rng.random() < 0.45:
            k = rng.choice([3, 5, 7])
            image = cv2.GaussianBlur(image, (k, k), 0)
        if rng.random() < 0.2:
            k = rng.choice([5, 9, 13])
            kernel = np.zeros((k, k), np.float32)
            kernel[k // 2, :] = 1 / k
            image = cv2.filter2D(image, -1, kernel)
        image += np.random.default_rng(n).normal(0, rng.uniform(0, 9), image.shape)
        image = np.clip(image, 0, 255).astype(np.uint8)
        lines = []
        for cls, mask in placed:
            ys, xs = np.where(mask > 0.35)
            full = np.count_nonzero(mask > 0.05) or 1
            if len(xs) < 150 or len(xs) < 0.45 * full:
                continue  # mostly hidden: do not ask the detector to find it
            x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
            lines.append(f"{names.index(cls)} {(x0 + x1) / 2 / W:.5f} {(y0 + y1) / 2 / H:.5f} "
                         f"{(x1 - x0) / W:.5f} {(y1 - y0) / H:.5f}")
        Image.fromarray(image).save(out_images / f"s{n:05d}.jpg", quality=rng.randint(45, 92))
        (out_labels / f"s{n:05d}.txt").write_text("\n".join(lines))
    print(f"{count} made-up training pictures in {TRAIN / 'synth'}")
    return count


# ---------- 6. train ----------

def fit(epochs: int = 40, base: str = "yolo11s.pt", imgsz: int = 800,
        out: Path | None = None) -> Path:
    from ultralytics import YOLO
    names = list(classes())
    labels = json.loads(LABELS.read_text()) if LABELS.exists() else {}
    shutil.rmtree(DATA, ignore_errors=True)
    for split in ("train", "val"):
        (DATA / "images" / split).mkdir(parents=True)
        (DATA / "labels" / split).mkdir(parents=True)
    rng = random.Random(3)
    real = sorted(labels)
    rng.shuffle(real)
    held_out = set(real[: max(2, len(real) // 6)])
    for name in real:  # the real photos, labelled by the vision model
        lines = [f"{names.index(b['cls'])} {(b['box'][0] + b['box'][2]) / 2:.5f} "
                 f"{(b['box'][1] + b['box'][3]) / 2:.5f} {b['box'][2] - b['box'][0]:.5f} "
                 f"{b['box'][3] - b['box'][1]:.5f}" for b in labels[name] if b["cls"] in names]
        copies = [("val", "")] if name in held_out else [("train", f"-{k}") for k in range(4)]
        for split, tag in copies:
            stem = Path(name).stem + tag
            shutil.copy(RAW / name, DATA / "images" / split / f"{stem}.jpg")
            (DATA / "labels" / split / f"{stem}.txt").write_text("\n".join(lines))
    made = sorted((TRAIN / "synth" / "images").glob("*.jpg"))
    for n, path in enumerate(made):
        split = "val" if n % 15 == 0 else "train"
        shutil.copy(path, DATA / "images" / split / path.name)
        shutil.copy(TRAIN / "synth" / "labels" / f"{path.stem}.txt",
                    DATA / "labels" / split / f"{path.stem}.txt")
    (DATA / "data.yaml").write_text(
        f"path: {DATA}\ntrain: images/train\nval: images/val\nnames:\n"
        + "".join(f"  {i}: {c}\n" for i, c in enumerate(names)))
    import torch
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = YOLO(base)
    model.train(data=str(DATA / "data.yaml"), imgsz=imgsz, epochs=epochs, batch=16, device=device,
                project=str(TRAIN / "runs"), name="parts", exist_ok=True, plots=False,
                degrees=25, flipud=0.3, fliplr=0.5, scale=0.6, hsv_h=0.02, hsv_s=0.6, hsv_v=0.45,
                mosaic=1.0, close_mosaic=6, workers=4, patience=15, verbose=False)
    best = TRAIN / "runs" / "parts" / "weights" / "best.pt"
    out = out or KIT / "model.pt"
    shutil.copy(best, out)
    print(f"model written to {out}")
    return out


def score(model: Path) -> dict:
    """How a model does on what matters: false boxes on real scenes with no LEGO in them
    (kept aside, never trained on), and how many of the real photographed parts it finds."""
    from .detector import Detector
    names = list(classes())
    detector = Detector(model)
    _, aside = _held_out(sorted(NEG.glob("*.jpg")))
    false_boxes = sum(sum(1 for d in detector.detect_array(cv2.imread(str(p))) if d["conf"] >= 0.5)
                      for p in aside)
    labels = json.loads(LABELS.read_text()) if LABELS.exists() else {}
    found = total = 0
    for name, boxes in labels.items():
        image = cv2.imread(str(RAW / name))
        seen = detector.detect_array(image)
        for b in boxes:
            if b["cls"] not in names:
                continue
            total += 1
            cx, cy = (b["box"][0] + b["box"][2]) / 2, (b["box"][1] + b["box"][3]) / 2
            found += any(d["cls"] == b["cls"] and d["conf"] >= 0.5
                         and d["box"][0] <= cx <= d["box"][2] and d["box"][1] <= cy <= d["box"][3]
                         for d in seen)
    return {"model": model.name, "scenes": len(aside), "false_boxes": false_boxes,
            "false_per_scene": round(false_boxes / max(1, len(aside)), 3),
            "parts_found": found, "parts": total}


def upgrade(extra: list[str], epochs: int = 24, base: str = "yolo11n.pt", imgsz: int = 640,
            scenes: bool = True) -> None:
    if scenes:
        asyncio.run(negatives(extra))
    synth(3000)
    candidate = fit(epochs, base, imgsz, out=KIT / "model-candidate.pt")
    live = KIT / "model.pt"
    before, after = score(live) if live.exists() else None, score(candidate)
    print("before:", before)
    print("after: ", after)
    # A model that knows more things is judged on the parts both know; it wins if it
    # finds at least as many of those and draws no more false boxes.
    better = before is None or (after["false_boxes"] <= before["false_boxes"] + 1
                                and after["parts_found"] >= before["parts_found"])
    if better:
        if live.exists():
            shutil.copy(live, KIT / "model-previous.pt")
        shutil.copy(candidate, live)
        print("SWITCHED: the new model is live (the hub reloads it by itself).")
    else:
        print("KEPT the current model: the new one was not better. It is at", candidate)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    command, rest = args[0], args[1:]
    if command == "ingest":
        ingest(rest)
    elif command == "label":
        asyncio.run(label(redo="--redo" in rest))
    elif command == "sheet":
        sheet(Path(rest[0]) if rest else TRAIN / "sheets")
    elif command == "cutouts":
        cutouts()
    elif command == "backgrounds":
        backgrounds()
    elif command == "synth":
        synth(int(rest[0]) if rest else 1500)
    elif command == "fit":
        fit(int(rest[0]) if rest else 40, rest[1] if len(rest) > 1 else "yolo11s.pt")
    elif command == "negatives":
        asyncio.run(negatives(rest))
    elif command == "score":
        print(score(Path(rest[0]) if rest else KIT / "model.pt"))
    elif command == "upgrade":
        upgrade(rest)
    elif command == "add":  # add <class name> <photos...>, then retrain without new scenes
        asyncio.run(add_class(rest[0], rest[1:]))
        upgrade([], scenes=False)
    elif command == "all":
        ingest(rest)
        asyncio.run(label())
        cutouts()
        backgrounds()
        synth()
        fit()
    else:
        raise SystemExit(__doc__)
