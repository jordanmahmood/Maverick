"""Azure OpenAI (AI Foundry) access. Keys live only in maverick/.env, which is gitignored."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncAzureOpenAI

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

CHECK_MODEL = os.environ.get("MAVERICK_CHECK_MODEL", "gpt-4.1")
PARSE_MODEL = os.environ.get("MAVERICK_PARSE_MODEL", "gpt-4.1")
API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

_client: AsyncAzureOpenAI | None = None


def configured() -> bool:
    return bool(os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY"))


def client() -> AsyncAzureOpenAI:
    global _client
    if _client is None:
        _client = AsyncAzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=API_VERSION, timeout=60.0, max_retries=2)
    return _client


def image(jpeg_b64: str, detail: str = "high") -> dict:
    return {"type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64}", "detail": detail}}


def for_boxes(jpeg_b64: str) -> tuple[str, int, int]:
    """Resize a photo so its short side is 768 px and return (jpeg, width, height).

    Ask the model for boxes in PIXELS of this exact size. Measured on gpt-5.6-luna and
    gpt-5.5: with fractions (or a 0..1000 grid) on a non-square photo the model sometimes
    divides y by the width, putting the box 25% too high. At "high" detail it sees the
    photo at this size anyway, and pixel answers were right 12 times out of 12.
    """
    import base64
    import io

    from PIL import Image
    image = Image.open(io.BytesIO(base64.b64decode(jpeg_b64))).convert("RGB")
    scale = 768 / min(image.size)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    if max(size) > 2048:
        scale = 2048 / max(size)
        size = (round(size[0] * scale), round(size[1] * scale))
    if size != image.size:
        image = image.resize(size, Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85)
    return base64.b64encode(out.getvalue()).decode(), size[0], size[1]


def unit_box(box: list[float], width: int, height: int) -> list[float]:
    """A pixel box from the model -> fractions 0..1, clamped. [] if it is not a box."""
    if len(box) != 4:
        return []
    x0, x1 = sorted((box[0] / width, box[2] / width))
    y0, y1 = sorted((box[1] / height, box[3] / height))
    x0, y0, x1, y1 = (min(1.0, max(0.0, v)) for v in (x0, y0, x1, y1))
    return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else []


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def is_reasoning(model: str) -> bool:
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


async def parse(model: str, content: list, schema, *, system: str | None = None,
                max_tokens: int = 1200, effort: str = "low", timeout: float | None = None):
    """One structured-output call. Returns the parsed pydantic object, or None."""
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": content}]
    kwargs: dict = {"model": model, "messages": messages, "response_format": schema}
    if is_reasoning(model):
        kwargs["max_completion_tokens"] = max_tokens * 4
        kwargs["reasoning_effort"] = effort
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = 0
    if timeout:
        kwargs["timeout"] = timeout
    response = await client().beta.chat.completions.parse(**kwargs)
    return response.choices[0].message.parsed
