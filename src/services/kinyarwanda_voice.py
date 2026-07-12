"""Kinyarwanda voice substrate for Sage.

Provides STT (speech-to-text) and TTS (text-to-speech) entry points so
inbound WhatsApp voice notes from Rwandan partners can become Sage chat
input, and Sage's replies can optionally come back as audio for partners
with low-literacy field staff.

Design notes:
- Stub-mode by default, matching how the WhatsApp/Telegram senders work.
  `MUNDI_KINYARWANDA_LIVE=0` (the default) returns a deterministic
  placeholder string and silent audio bytes. Set `MUNDI_KINYARWANDA_LIVE=1`
  and provide credentials to use a real backend.
- Provider abstraction: `WhisperProvider` (OpenAI multilingual Whisper)
  is the default live backend. Mbaza/DigitalUmuganda Rwanda-specific
  models can be plugged in later by implementing the `STTProvider`
  protocol; the rest of the system doesn't care which one is wired up.
- The audio MIME is part of the contract: WhatsApp delivers OGG/Opus,
  Telegram delivers OGG/Opus, web uploads may deliver MP3/WAV. The
  provider receives raw bytes + mime and is responsible for whatever
  transcoding it needs.
- We never log audio content — only metadata. Voice notes can contain
  PII (phone numbers, location, names) and Rwandan privacy norms +
  partner trust trump verbosity.

What is NOT in this module:
- The WhatsApp/Telegram inbound webhook itself. That lives behind the
  local deployment gate; the webhook will call into `transcribe()` once
  it's wired up.
- A streaming pipe. WhatsApp voice notes are short (typically < 60s)
  and the batch API is good enough; streaming Whisper is overkill for
  this use case.

Telemetry note: callers may wrap `transcribe()` and `synthesize()` in spans,
but this substrate intentionally does not emit OTel spans itself yet. When
that is added, span attributes must stay metadata-only and never include audio
content or transcribed text.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Protocol

logger = logging.getLogger("mundi.kinyarwanda_voice")

# Default language for Sage voice in Rwanda. Whisper's ISO 639-1 code for
# Kinyarwanda. Whisper has partial coverage; for higher accuracy a Mbaza
# fine-tune should be wired in via STTProvider once we benchmark them.
DEFAULT_LANG = "rw"
DEFAULT_OPENAI_AUDIO_TIMEOUT_SEC = 60.0
_MIME_TO_EXT = {
    "audio/ogg": "ogg",
    "audio/oga": "ogg",
    "audio/ogg; codecs=opus": "ogg",
    "audio/opus": "opus",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
}


@dataclass
class Transcription:
    text: str
    language: str
    confidence: Optional[float]
    provider: str
    live: bool
    duration_ms: int


@dataclass
class Synthesis:
    audio_bytes: bytes
    mime: str
    provider: str
    live: bool
    duration_ms: int


class STTProvider(Protocol):
    name: str

    async def transcribe(
        self, audio_bytes: bytes, mime: str, language: str
    ) -> Transcription: ...


class TTSProvider(Protocol):
    name: str

    async def synthesize(self, text: str, language: str) -> Synthesis: ...


# ---------------------------------------------------------------------------
# Stub providers — used when MUNDI_KINYARWANDA_LIVE=0 (default).
# ---------------------------------------------------------------------------

class StubSTT:
    """Logs metadata, returns a deterministic placeholder transcript.

    The placeholder is shaped like a real query so downstream code that
    processes the transcript doesn't get a weirdly-empty string. We
    intentionally do not return an empty string — empty inputs trip up
    LLM prompts and chat-history hygiene checks.
    """

    name = "stub-stt"

    async def transcribe(
        self, audio_bytes: bytes, mime: str, language: str
    ) -> Transcription:
        t0 = time.monotonic()
        logger.info(
            "kinyarwanda stub STT: bytes=%d mime=%s lang=%s",
            len(audio_bytes), mime, language,
        )
        # Tiny await so the function actually yields — keeps the contract
        # honest (callers will `await` and the stub shouldn't be 0ms).
        await asyncio.sleep(0)
        return Transcription(
            text="[stub-transcript] Erekana imihindagurikire y'ibihingwa muri Huye.",
            language=language,
            confidence=None,
            provider=self.name,
            live=False,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


class StubTTS:
    """Returns a 1-frame silent OGG/Opus payload as the audio body.

    Real partners shouldn't receive this — the gate is MUNDI_KINYARWANDA_LIVE.
    But we emit *something* parseable as audio so an inbound test fixture
    can round-trip the contract without raising in the caller.
    """

    name = "stub-tts"

    # Minimal OGG/Opus header + one silent frame. Just enough to be a
    # syntactically valid OGG container; not a perfectly playable file.
    _SILENT_OGG = (
        b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    )

    async def synthesize(self, text: str, language: str) -> Synthesis:
        t0 = time.monotonic()
        logger.info(
            "kinyarwanda stub TTS: text_len=%d lang=%s",
            len(text), language,
        )
        await asyncio.sleep(0)
        return Synthesis(
            audio_bytes=self._SILENT_OGG,
            mime="audio/ogg",
            provider=self.name,
            live=False,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


# ---------------------------------------------------------------------------
# Live providers — OpenAI Whisper for STT, OpenAI TTS for synthesis.
# These activate only when MUNDI_KINYARWANDA_LIVE=1 AND OPENAI_API_KEY is set.
# ---------------------------------------------------------------------------

def _openai_audio_timeout_sec() -> float:
    raw = os.environ.get(
        "MUNDI_OPENAI_AUDIO_TIMEOUT_SEC",
        str(DEFAULT_OPENAI_AUDIO_TIMEOUT_SEC),
    )
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError("MUNDI_OPENAI_AUDIO_TIMEOUT_SEC must be a number") from exc
    if timeout <= 0:
        raise ValueError("MUNDI_OPENAI_AUDIO_TIMEOUT_SEC must be positive")
    return timeout


class WhisperSTT:
    """OpenAI Whisper transcription. Kinyarwanda coverage is partial.

    For production we'll likely swap to a Mbaza/DigitalUmuganda fine-tune
    via this same protocol once we have a per-partner accuracy benchmark.
    """

    name = "openai-whisper-1"

    async def transcribe(
        self, audio_bytes: bytes, mime: str, language: str
    ) -> Transcription:
        t0 = time.monotonic()
        from openai import AsyncOpenAI

        ext = _MIME_TO_EXT.get(mime.lower(), "ogg")
        filename = f"voice.{ext}"
        client = AsyncOpenAI()
        # OpenAI's SDK accepts a (filename, fileobj) tuple for `file=`.
        bio = io.BytesIO(audio_bytes)
        bio.name = filename
        timeout = _openai_audio_timeout_sec()
        try:
            resp = await client.audio.transcriptions.create(
                model=os.environ.get("MUNDI_WHISPER_MODEL", "whisper-1"),
                file=bio,
                language=language,
                response_format="json",
                timeout=timeout,
            )
            text = getattr(resp, "text", "") or ""
        finally:
            try:
                bio.close()
            except Exception:
                pass
        return Transcription(
            text=text,
            language=language,
            confidence=None,
            provider=self.name,
            live=True,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


class OpenAITTS:
    """OpenAI TTS. Note: Kinyarwanda voice quality is currently weak;
    for partner-facing voice replies we should validate this on real
    samples before flipping LIVE for TTS."""

    name = "openai-tts-1"

    async def synthesize(self, text: str, language: str) -> Synthesis:
        t0 = time.monotonic()
        from openai import AsyncOpenAI

        client = AsyncOpenAI()
        timeout = _openai_audio_timeout_sec()
        resp = await client.audio.speech.create(
            model=os.environ.get("MUNDI_TTS_MODEL", "tts-1"),
            voice=os.environ.get("MUNDI_TTS_VOICE", "alloy"),
            input=text,
            response_format="opus",
            timeout=timeout,
        )
        # AsyncOpenAI returns a streaming response object; .read() collects all bytes
        audio_bytes = await resp.aread() if hasattr(resp, "aread") else resp.content
        return Synthesis(
            audio_bytes=audio_bytes,
            mime="audio/opus",
            provider=self.name,
            live=True,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

def _is_live() -> bool:
    return os.environ.get("MUNDI_KINYARWANDA_LIVE", "0") == "1"


def _has_openai_key() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def get_stt_provider() -> STTProvider:
    if _is_live() and _has_openai_key():
        return WhisperSTT()
    return StubSTT()


def get_tts_provider() -> TTSProvider:
    if _is_live() and _has_openai_key():
        return OpenAITTS()
    return StubTTS()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def transcribe(
    audio_bytes: bytes, mime: str = "audio/ogg", language: str = DEFAULT_LANG
) -> Transcription:
    if not audio_bytes:
        raise ValueError("transcribe(): audio_bytes is empty")
    provider = get_stt_provider()
    return await provider.transcribe(audio_bytes, mime, language)


async def synthesize(text: str, language: str = DEFAULT_LANG) -> Synthesis:
    if not text or not text.strip():
        raise ValueError("synthesize(): text is empty")
    provider = get_tts_provider()
    return await provider.synthesize(text.strip(), language)
