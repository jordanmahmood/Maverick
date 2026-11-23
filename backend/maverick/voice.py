"""Voice out (spoken steps) and voice in (commands and questions), through Azure OpenAI."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import re
import wave
from pathlib import Path

from openai import AsyncAzureOpenAI

from . import ai
from .guide import Guide

GUIDES = Path(__file__).resolve().parents[2] / "guides"
SPEECH = GUIDES / "kits" / "_speech"
TTS_MODEL = os.environ.get("MAVERICK_TTS_MODEL", "gpt-4o-mini-tts")
STT_MODEL = os.environ.get("MAVERICK_STT_MODEL", "gpt-4o-mini-transcribe")
VOICE = os.environ.get("MAVERICK_VOICE", "coral")
STYLE = "A calm, friendly coach sitting next to the builder. Brisk, clear, never excited."

FINISHED = "All done. Nice build."
FOREIGN = "Remove the foreign object from the chassis."

_client: AsyncAzureOpenAI | None = None


def enabled() -> bool:
    return ai.configured() and os.environ.get("MAVERICK_VOICE_OFF") != "1"


def client() -> AsyncAzureOpenAI:
    global _client
    if _client is None:
        _client = AsyncAzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version="2025-03-01-preview", timeout=20.0, max_retries=1)
    return _client


def spoken(text: str) -> str:
    """'2x4' reads badly aloud; 'two by four' does not."""
    text = re.sub(r"(\d+)\s*[x×]\s*(\d+)", r"\1 by \2", text)
    return text.replace("✓", "").replace("!", "").strip()


def _path(text: str) -> Path:
    return SPEECH / (hashlib.sha1(f"{VOICE}|{text}".encode()).hexdigest()[:16] + ".mp3")


def cached_url(text: str) -> str | None:
    """URL of the clip if it has already been made. Never waits."""
    path = _path(spoken(text))
    return "/guides/" + str(path.relative_to(GUIDES)) if path.exists() else None


async def say(text: str) -> str | None:
    """Make (or reuse) a spoken clip. Returns its URL, or None if speech is unavailable."""
    if not enabled() or not text.strip():
        return None
    line = spoken(text)
    path = _path(line)
    if not path.exists():
        try:
            response = await client().audio.speech.create(
                model=TTS_MODEL, voice=VOICE, input=line, response_format="mp3",
                instructions=STYLE)
            SPEECH.mkdir(parents=True, exist_ok=True)
            path.write_bytes(response.content)
        except Exception as err:
            print(f"[voice] speech failed: {type(err).__name__}: {err}")
            return None
    return "/guides/" + str(path.relative_to(GUIDES))


async def prepare(guide: Guide) -> None:
    """Speak every step once, ahead of time, so step changes are instant."""
    if not enabled():
        return
    gate = asyncio.Semaphore(6)

    async def one(text: str):
        async with gate:
            await say(text)

    await asyncio.gather(one(FINISHED), one(FOREIGN), *(one(step.say) for step in guide.steps))


# Spelling help for the transcriber: the wake word and the part names. Kept short and not
# phrased as commands, so that an echo of it can never be mistaken for one.
STT_HINT = "Maverick. The red rear, the black hood, the chassis, the minifigure, the glass roof."


async def transcribe(pcm16_b64: str, rate: int = 16000) -> str:
    """Raw 16-bit mono PCM from the glasses' microphone -> text."""
    if not enabled():
        return ""
    pcm = base64.b64decode(pcm16_b64)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    try:
        result = await client().audio.transcriptions.create(
            model=STT_MODEL, file=("speech.wav", buffer.getvalue()), language="en",
            prompt=STT_HINT)
        text = result.text.strip()
        # On noise with no speech the model sometimes just returns the hint. That is not
        # something the wearer said.
        said = re.sub(r"[^a-z ]", "", text.lower())
        hint = re.sub(r"[^a-z ]", "", STT_HINT.lower())
        parts = sum(name in said for name in ("red rear", "black hood", "chassis", "minifigure",
                                              "glass roof"))
        if parts >= 3 or (len(said) > 25 and (said in hint or hint[:40] in said
                                              or text.lower().count("maverick") >= 3)):
            return ""   # nobody lists the parts like that: it is the hint coming back
        return text
    except Exception as err:
        print(f"[voice] transcription failed: {type(err).__name__}: {err}")
        return ""


COMMANDS = [
    (r"\b(next|next step|skip|done|i'?m done|finished)\b", "next"),
    (r"\b(go back|back|previous)\b", "back"),
    (r"\b(repeat|again|say that again)\b", "repeat"),
    (r"\b(show me|show it|where does it go|where does (this|that) go|help)\b", "peek"),
    (r"\b(start over|restart)\b", "restart"),
]


# The wake word, with the ways a transcriber tends to spell it in a noisy room.
WAKE = re.compile(r"\b(?:hey |ok |okay |yo )?(?:maverick|mavericks|maverik|mavrick|mavrik|maveric|"
                  r"mav rick|mav'?rick|mavric)\b[\s,.!?:-]*", re.I)


def understand(text: str, direct: bool = False) -> tuple[str, str]:
    """('command', name) | ('ask', question) | ('ignore', '').

    Maverick only answers when spoken to by name: "Maverick, next", "Maverick, where is
    the red rear?". Anything said without the name is somebody else's conversation, however
    much it sounds like a command, and gets no reply.
    """
    match = WAKE.search(text)
    if not match and not direct:
        return "ignore", ""
    # direct: the wearer tapped the touchpad first, so everything said is for Maverick.
    request = text[match.end():].strip() if match else text.strip()
    if direct and match and not request:
        request = text[:match.start()].strip()   # "where is the red rear, Maverick"
    low = re.sub(r"[^a-z' ]", " ", request.lower()).strip()
    if not low:
        return "ignore", ""  # just the name: nothing was asked
    if len(low.split()) <= 6:
        for pattern, name in COMMANDS:
            if re.search(pattern, low):
                return "command", name
    return "ask", request
