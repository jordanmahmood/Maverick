"""Pictures for the lens: manual drawings on black, and 3D brick specs.

Black is transparent on the waveguide, so the booklet's light-blue page and the cartoon
hands are turned black, and very dark parts are lifted to grey so they stay visible.
"""
from __future__ import annotations

import base64
import io
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from .guide import Guide, Step

GUIDES = Path(__file__).resolve().parents[2] / "guides"
HUD_SIZE = (440, 300)

COLOURS = {
    "lime": "#A5CA18", "black": "#30363B", "red": "#E3000B", "white": "#F4F4F4",
    "tan": "#E4CD9E", "blue": "#1E5AA8", "medium blue": "#6FA8DC", "yellow": "#F2CD37",
    "green": "#237841", "orange": "#FE8A18", "brown": "#8A5A3C", "reddish brown": "#8A5A3C",
    "light gray": "#A0A5A9", "light grey": "#A0A5A9", "gray": "#8D9296", "grey": "#8D9296",
    "light bluish gray": "#A0A5A9", "dark gray": "#6C6E68", "dark grey": "#6C6E68",
    "dark bluish gray": "#6C6E68", "teal": "#1FA5A0", "azure": "#36AEBF",
    "medium azure": "#36AEBF", "sand green": "#A0BCAC", "transparent red": "#FF4B4B",
    "clear": "#D8F0F8", "transparent": "#D8F0F8", "smoke": "#7C8791", "pink": "#F785B1",
    "purple": "#81007B", "dark red": "#A0171B", "nougat": "#D09168",
}
_PART = re.compile(r"^\s*(\d+)\s*x\s+(.*)$", re.I)
_DIMS = re.compile(r"\b(\d+)\s*x\s*(\d+)\b")


def brick_spec(part: str) -> dict | None:
    """'2x black 1x2 plate' -> a spec the HUD can draw as a 3D brick. None if not a box."""
    match = _PART.match(part)
    count, name = (int(match.group(1)), match.group(2)) if match else (1, part.strip())
    dims = _DIMS.search(name)
    low = name.lower()
    if not dims:
        return None
    if any(w in low for w in ("minifig", "tire", "wheel", "windshield", "canopy", "hinge",
                              "clip", "bracket", "arch", "panel", "fin")):
        return None
    w, l = sorted((int(dims.group(1)), int(dims.group(2))))
    if w > 8 or l > 16:
        return None
    colour = next((COLOURS[c] for c in sorted(COLOURS, key=len, reverse=True) if c in low),
                  "#A0A5A9")
    flat = "plate" in low or "tile" in low
    shape = "slope" if ("slope" in low or "wedge" in low) else ("round" if "round" in low else "box")
    return {"w": w, "l": l, "h": 1 if flat else 3, "studs": "tile" not in low and shape != "slope",
            "shape": shape, "color": colour, "count": count,
            "label": name.replace(" x ", "×").replace("x", "×", 1) if dims else name}


def bricks_for(step: Step) -> list[dict]:
    specs = [s for s in (brick_spec(p) for p in step.parts) if s]
    return specs[:2]


# ---------- manual drawing -> picture for a black, see-through display ----------

_PAGE_BLUES = [((210, 236, 251), 34), ((180, 212, 243), 26), ((132, 165, 215), 26)]


def _on_black(page: Path, keep: float = 0.90) -> Image.Image:
    rgb = np.asarray(Image.open(page).convert("RGB")).astype(np.int16)
    height = rgb.shape[0]
    rgb = rgb[: int(height * keep)]  # drop the page number and footer line
    background = np.zeros(rgb.shape[:2], dtype=bool)
    for colour, tolerance in _PAGE_BLUES:
        background |= np.abs(rgb - np.array(colour)).max(axis=2) < tolerance
    out = (64 + rgb * 0.75).clip(0, 255)  # lift dark parts so they show on the lens
    out[background] = 0
    image = Image.fromarray(out.astype(np.uint8))
    ys, xs = np.where(~background)
    if len(xs) > 200:
        x0, x1 = np.percentile(xs, [0.3, 99.7])
        y0, y1 = np.percentile(ys, [0.3, 99.7])
        pad = 14
        image = image.crop((max(0, int(x0) - pad), max(0, int(y0) - pad),
                            min(image.width, int(x1) + pad), min(image.height, int(y1) + pad)))
    return image


def hud_image(step: Step, with_parts: bool) -> str | None:
    """Make (once) the lens picture for a step. Returns a path relative to guides/."""
    if not step.image:
        return None
    source = GUIDES / step.image
    if not source.exists():
        return None
    target = source.parent.parent / "hud" / f"s-{step.id:03d}{'p' if with_parts else ''}.png"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        result = _on_black(source)
        parts_page = GUIDES / step.parts_image if (with_parts and step.parts_image) else None
        if parts_page and parts_page.exists():
            parts = _on_black(parts_page)
            parts.thumbnail((HUD_SIZE[0] * 2 // 5, HUD_SIZE[1]))
            result.thumbnail((HUD_SIZE[0] * 3 // 5 - 8, HUD_SIZE[1]))
            canvas = Image.new("RGB", (parts.width + 8 + result.width,
                                       max(parts.height, result.height)), "black")
            canvas.paste(parts, (0, (canvas.height - parts.height) // 2))
            canvas.paste(result, (parts.width + 8, (canvas.height - result.height) // 2))
            result = canvas
        else:
            result.thumbnail(HUD_SIZE)
        result.save(target, optimize=True)
    return str(target.relative_to(GUIDES))


def _content_box(page: Path) -> tuple[Image.Image, tuple[int, int, int, int]]:
    image = Image.open(page).convert("RGB")
    rgb = np.asarray(image).astype(np.int16)
    rgb = rgb[: int(rgb.shape[0] * 0.90)]
    background = np.abs(rgb - np.array(_PAGE_BLUES[0][0])).max(axis=2) < _PAGE_BLUES[0][1]
    ys, xs = np.where(~background)
    if len(xs) < 200:
        return image, (0, 0, image.width, image.height)
    x0, x1 = np.percentile(xs, [0.3, 99.7])
    y0, y1 = np.percentile(ys, [0.3, 99.7])
    pad = 24
    return image, (max(0, int(x0) - pad), max(0, int(y0) - pad),
                   min(image.width, int(x1) + pad), min(image.height, int(y1) + pad))


@lru_cache(maxsize=512)
def _reference(rel_path: str) -> str | None:
    """The manual drawing in its own colours, cropped to the drawing, for the checker."""
    source = GUIDES / rel_path
    if not source.exists():
        return None
    image, box = _content_box(source)
    image = image.crop(box)
    image.thumbnail((512, 512))  # one image tile: keeps checks fast
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return base64.b64encode(out.getvalue()).decode()


@lru_cache(maxsize=128)
def _photo(rel_path: str) -> str | None:
    source = GUIDES / rel_path
    if not source.exists():
        return None
    image = Image.open(source).convert("RGB")
    image.thumbnail((512, 512))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return base64.b64encode(out.getvalue()).decode()


def reference_b64(step: Step) -> str | None:
    """What the build looks like with this step finished: manual drawing, else a photo."""
    if step.image:
        return _reference(step.image)
    return _photo(step.done_refs[0]) if step.done_refs else None


def icon_image(step: Step) -> str | None:
    """Small picture of the parts to pick up, for steps without a drawable 3D brick."""
    if not step.parts_image:
        return None
    source = GUIDES / step.parts_image
    if not source.exists():
        return None
    target = source.parent.parent / "hud" / f"i-{step.id:03d}.png"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        picture = _on_black(source, keep=0.84)  # also drops the little footer figure
        picture.thumbnail((184, 184))
        picture.save(target, optimize=True)
    return str(target.relative_to(GUIDES))


def part_image(step: Step) -> str | None:
    """A clean cut-out photo of the part (made by maverick.train), if the kit has one."""
    if not step.cls:
        return None
    for kit in (GUIDES / "kits").iterdir():
        path = kit / "parts" / f"{step.cls}.png"
        if path.exists():
            return str(path.relative_to(GUIDES))
    return None


def visuals(step: Step) -> dict:
    """Extra fields for a step's screen message."""
    bricks = bricks_for(step)
    image = hud_image(step, with_parts=False)
    extra: dict = {"bricks": bricks}
    if image:
        extra["image"] = "/guides/" + image
    part = part_image(step)
    if part:
        extra["icon"], extra["bricks"] = "/guides/" + part, []
    elif not bricks:
        icon = icon_image(step)
        if icon:
            extra["icon"] = "/guides/" + icon
    return extra


def prepare(guide: Guide) -> None:
    for step in guide.steps:
        try:
            visuals(step)
        except Exception as err:
            print(f"[visual] step {step.id}: {err}")
