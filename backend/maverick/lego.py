"""LEGO kit parser: set number -> official instruction PDF -> camera-checkable guide.

    uv run python -m maverick.lego 60486            # by set number
    uv run python -m maverick.lego 6617227          # by instruction PDF id
    uv run python -m maverick.lego https://.../6617227.pdf

Every page is rendered to a JPEG and read by a vision model together with the page
before it, so both booklet styles work: classic (numbered steps with a parts box) and
the 4+/City style (a page of hands holding the parts, then a page showing where they go).
Results are cached in guides/kits/<kit>/.
"""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Awaitable, Callable, Literal

import fitz  # pymupdf
import httpx
from pydantic import BaseModel

from . import ai
from .guide import Guide, Step

ROOT = Path(__file__).resolve().parents[2]
KITS = ROOT / "guides" / "kits"
PDF_URL = "https://www.lego.com/cdn/product-assets/product.bi.core.pdf/{id}.pdf"
SET_PAGE = "https://www.lego.com/en-us/service/building-instructions/{set}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
# Booklets we have checked by hand. Anything else is looked up on LEGO.com.
KNOWN = {"60486": ["6617227"]}
MAX_PAGES = 320
CONCURRENCY = 12

Progress = Callable[[str, int, int], Awaitable[None]]


class PageStep(BaseModel):
    number: int | None
    say: str
    detail: str
    parts: list[str]
    check: str
    common_mistakes: list[str]


class PageInfo(BaseModel):
    kind: Literal["cover", "parts_only", "build", "inventory", "other"]
    set_name: str | None
    steps: list[PageStep]


PROMPT = """You convert LEGO building-instruction booklets into steps for a smart-glasses \
assistant. The wearer sees one short line of text; a head-worn camera then checks the step.

You get two consecutive booklet pages: PREVIOUS (context only) and CURRENT.
Extract ONLY the build steps whose finished result is drawn on CURRENT.

Booklet styles:
- Classic: numbered steps. Each has a parts box (pictures with counts like "2x") and a \
drawing of the model after those parts are added. A page can hold several steps.
- 4+ / City easy style: one page shows two hands holding the parts, the NEXT page shows \
where they go (new parts are outlined or highlighted). The parts for CURRENT are then on \
PREVIOUS. Small sub-assemblies are built on their own, then attached to the main model.

kind:
- "parts_only": CURRENT only shows parts (in hands or loose), no assembly drawing.
- "build": CURRENT shows at least one assembly result.
- "cover", "inventory" (the parts list at the back), "other" (ads, safety, app, open-the-bag).
set_name: only for a cover, the set name and number if printed. Otherwise null.

For each step on CURRENT:
- number: the printed step number, or null if none is printed.
- parts: every part ADDED in this step, as "<count>x <colour> <size and shape>", for \
example "2x tan pin", "1x lime 2x4 brick", "1x black 1x2 plate". Count carefully.
- say: 8 words max. Starts with a verb. Names the colour, the part and where it goes, \
for example "Snap lime 2x4 brick behind the seat".
- detail: one sentence with the exact position and orientation, relative to things the \
builder can see (front, back, left, right, on top of which colour, which end).
- check: one sentence saying what the camera SEES when this step is finished: the model \
so far plus the newly added parts, their colours and positions. Checkable from one photo.
- common_mistakes: 1 to 3 mistakes a camera could see (wrong colour, wrong position, \
mirrored, not pressed down, missing one of several parts).
Sub-assembly built on its own: start say with "Build". Joining it to the main model: \
start say with "Attach". Assembling a minifigure is a step too.
Keep the order of the booklet. If CURRENT has no assembly drawing, steps is empty."""


# ---------- find and fetch the booklet ----------

async def resolve(kit: str, http: httpx.AsyncClient) -> tuple[str, list[str]]:
    """Return (cache key, [pdf urls]) for a set number, a PDF id or a PDF URL."""
    kit = kit.strip()
    if kit.lower().startswith("http"):
        match = re.search(r"(\d+)\.pdf", kit)
        return (f"pdf-{match.group(1)}" if match else "pdf-custom"), [kit]
    key = re.sub(r"[^0-9A-Za-z-]", "", kit)
    if not key:
        raise ValueError("Enter a LEGO set number, for example 60486")
    number = key.split("-")[0]
    if number in KNOWN:
        return number, [PDF_URL.format(id=i) for i in KNOWN[number]]
    try:
        page = await http.get(SET_PAGE.format(set=number))
        ids = list(dict.fromkeys(re.findall(r"product\.bi\.core\.pdf/(\d+)\.pdf", page.text)))
    except httpx.HTTPError:
        ids = []
    if ids:
        return number, [PDF_URL.format(id=i) for i in ids]
    # Maybe it is an instruction PDF id (printed on the back of the booklet).
    direct = PDF_URL.format(id=number)
    head = await http.head(direct)
    if head.status_code == 200:
        return f"pdf-{number}", [direct]
    raise ValueError(f"No building instructions found on LEGO.com for '{kit}'")


async def fetch_pdfs(urls: list[str], folder: Path, http: httpx.AsyncClient) -> list[Path]:
    paths, seen_page_counts = [], set()
    for n, url in enumerate(urls):
        path = folder / f"booklet-{n + 1}.pdf"
        if not path.exists():
            response = await http.get(url)
            response.raise_for_status()
            path.write_bytes(response.content)
        # LEGO lists the same booklet in several paper formats. Keep one of each length.
        with fitz.open(path) as doc:
            if doc.page_count in seen_page_counts:
                continue
            seen_page_counts.add(doc.page_count)
        paths.append(path)
    return paths


def render_pages(pdfs: list[Path], folder: Path, long_side: int = 1100) -> list[Path]:
    pages_dir = folder / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for pdf in pdfs:
        with fitz.open(pdf) as doc:
            for page in doc:
                if len(out) >= MAX_PAGES:
                    return out
                path = pages_dir / f"p-{len(out) + 1:03d}.jpg"
                if not path.exists():
                    zoom = long_side / max(page.rect.width, page.rect.height)
                    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                    pix.save(path, jpg_quality=82)
                out.append(path)
    return out


# ---------- read the pages ----------

def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


async def read_page(pages: list[Path], i: int, model: str) -> PageInfo:
    content = []
    if i > 0:
        content += [ai.text("PREVIOUS page (context only):"), ai.image(_b64(pages[i - 1]))]
    content += [ai.text("CURRENT page:"), ai.image(_b64(pages[i]))]
    for attempt in range(3):
        try:
            info = await ai.parse(model, content, PageInfo, system=PROMPT, max_tokens=2500,
                                  timeout=90)
            if info is not None:
                return info
        except Exception as err:  # one bad page must not sink the booklet
            print(f"[lego] page {i + 1} attempt {attempt + 1}: {type(err).__name__}: {err}")
            await asyncio.sleep(1.5 * (attempt + 1))
    return PageInfo(kind="other", set_name=None, steps=[])


def _words(text: str, limit: int) -> str:
    parts = text.split()
    return text if len(parts) <= limit else " ".join(parts[:limit])


def assemble(kit: str, source: str, pages: list[Path], infos: list[PageInfo]) -> Guide:
    title = next((p.set_name for p in infos if p.kind == "cover" and p.set_name), None)
    steps: list[Step] = []
    for info in infos:
        # A page of finished sub-assemblies lying in the hands adds no parts: it is the
        # "pick up" picture for the next page, not a step of its own.
        if info.kind == "build" and not any(s.parts for s in info.steps):
            info.kind = "parts_only"
    for i, info in enumerate(infos):
        if info.kind != "build":
            continue
        parts_page = pages[i - 1] if i > 0 and infos[i - 1].kind == "parts_only" else None
        for found in info.steps:
            steps.append(Step(
                id=len(steps) + 1,
                say=_words(found.say.strip().rstrip("."), 9),
                detail=found.detail.strip(),
                check=found.check.strip(),
                common_mistakes=found.common_mistakes[:3],
                parts=found.parts,
                image=str(pages[i].relative_to(ROOT / "guides")),
                parts_image=str(parts_page.relative_to(ROOT / "guides")) if parts_page else None,
                page=i + 1))
    # The last pages often show the finished model again. Drop a repeat of the step before.
    kept: list[Step] = []
    for step in steps:
        last = kept[-1] if kept else None
        if last and not step.parts_image and last.page != step.page and \
                SequenceMatcher(None, last.say.lower(), step.say.lower()).ratio() > 0.8:
            continue
        step.id = len(kept) + 1
        kept.append(step)
    steps = kept
    if not steps:
        raise ValueError("No build steps were found in this booklet")
    return Guide(title=title or f"LEGO {kit}", steps=steps, kit=kit, source=source)


async def build_kit(kit: str, progress: Progress | None = None, force: bool = False,
                    model: str | None = None) -> Guide:
    """Set number (or PDF id / URL) in, guide out. Cached after the first run."""
    model = model or ai.PARSE_MODEL

    async def tell(message: str, done: int = 0, total: int = 0):
        if progress:
            await progress(message, done, total)

    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=90) as http:
        await tell("Finding the booklet")
        key, urls = await resolve(kit, http)
        folder = KITS / key
        cached = folder / "guide.json"
        if cached.exists() and not force:
            return Guide.model_validate_json(cached.read_text())
        if not ai.configured():
            raise ValueError("Azure OpenAI is not configured (maverick/.env)")
        folder.mkdir(parents=True, exist_ok=True)
        await tell("Downloading the booklet")
        pdfs = await fetch_pdfs(urls, folder, http)

    pages = await asyncio.to_thread(render_pages, pdfs, folder)
    total = len(pages)
    await tell("Reading pages", 0, total)
    gate = asyncio.Semaphore(CONCURRENCY)
    done = 0

    async def one(i: int) -> PageInfo:
        nonlocal done
        async with gate:
            info = await read_page(pages, i, model)
        done += 1
        await tell("Reading pages", done, total)
        return info

    infos = await asyncio.gather(*(one(i) for i in range(total)))
    guide = assemble(key, urls[0], pages, list(infos))
    (folder / "guide.json").write_text(guide.model_dump_json(indent=2))
    (folder / "pages.json").write_text(
        "[" + ",\n".join(i.model_dump_json() for i in infos) + "]")
    await tell("Ready", total, total)
    return guide


def reassemble(key: str) -> Guide:
    """Rebuild guide.json from the saved page readings, without calling the model."""
    folder = KITS / key
    infos = [PageInfo.model_validate(i) for i in json.loads((folder / "pages.json").read_text())]
    pages = sorted((folder / "pages").glob("p-*.jpg"))
    old = Guide.model_validate_json((folder / "guide.json").read_text())
    guide = assemble(key, old.source or "", pages, infos)
    (folder / "guide.json").write_text(guide.model_dump_json(indent=2))
    shutil.rmtree(folder / "hud", ignore_errors=True)
    return guide


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    if "--reassemble" in sys.argv:
        result = reassemble(sys.argv[1])
        print(f"{result.title}: {len(result.steps)} steps")
        raise SystemExit(0)

    async def _progress(message: str, done: int, total: int):
        print(f"\r{message} {done}/{total}   ", end="", flush=True)

    result = asyncio.run(build_kit(sys.argv[1], _progress, force="--force" in sys.argv,
                                   model=next((a.split("=")[1] for a in sys.argv
                                               if a.startswith("--model=")), None)))
    print(f"\n{result.title}: {len(result.steps)} steps")
    for s in result.steps:
        print(f"{s.id:3d} p{s.page:<3d} {s.say}  [{', '.join(s.parts)}]")
