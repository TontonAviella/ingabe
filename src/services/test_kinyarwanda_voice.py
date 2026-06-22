"""Tests for Kinyarwanda voice substrate.

Covers the stub-mode contract (default), provider selection, and the
validation gates on the public transcribe()/synthesize() entry points.

Live providers (WhisperSTT / OpenAITTS) are not exercised — they make
real API calls and live behind MUNDI_KINYARWANDA_LIVE=1.
"""

import os
import sys
from types import SimpleNamespace

import pytest

from src.services import kinyarwanda_voice as kv


# ---------------------------------------------------------------------------
# Stub STT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stub_stt_returns_deterministic_placeholder():
    stt = kv.StubSTT()
    r = await stt.transcribe(b"\x00" * 128, "audio/ogg", "rw")
    assert r.text  # never empty — downstream LLM prompts get a real string
    assert r.text.startswith("[stub-transcript]")
    assert r.language == "rw"
    assert r.live is False
    assert r.provider == "stub-stt"
    assert r.duration_ms >= 0


@pytest.mark.asyncio
async def test_stub_tts_returns_silent_ogg():
    tts = kv.StubTTS()
    r = await tts.synthesize("muraho neza", "rw")
    assert r.audio_bytes.startswith(b"OggS")
    assert r.mime == "audio/ogg"
    assert r.live is False
    assert r.provider == "stub-tts"


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

def test_get_stt_provider_defaults_to_stub(monkeypatch):
    monkeypatch.delenv("MUNDI_KINYARWANDA_LIVE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert isinstance(kv.get_stt_provider(), kv.StubSTT)


def test_get_tts_provider_defaults_to_stub(monkeypatch):
    monkeypatch.delenv("MUNDI_KINYARWANDA_LIVE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert isinstance(kv.get_tts_provider(), kv.StubTTS)


def test_get_stt_provider_stub_when_live_but_no_key(monkeypatch):
    monkeypatch.setenv("MUNDI_KINYARWANDA_LIVE", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # LIVE without credentials must NOT silently route to OpenAI — stay stubbed
    assert isinstance(kv.get_stt_provider(), kv.StubSTT)


def test_get_stt_provider_whisper_when_live_and_key(monkeypatch):
    monkeypatch.setenv("MUNDI_KINYARWANDA_LIVE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert isinstance(kv.get_stt_provider(), kv.WhisperSTT)


def test_get_tts_provider_openai_when_live_and_key(monkeypatch):
    monkeypatch.setenv("MUNDI_KINYARWANDA_LIVE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert isinstance(kv.get_tts_provider(), kv.OpenAITTS)


# ---------------------------------------------------------------------------
# Public entry points — validation gates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transcribe_rejects_empty_audio(monkeypatch):
    monkeypatch.delenv("MUNDI_KINYARWANDA_LIVE", raising=False)
    with pytest.raises(ValueError, match="empty"):
        await kv.transcribe(b"")


@pytest.mark.asyncio
async def test_synthesize_rejects_empty_text(monkeypatch):
    monkeypatch.delenv("MUNDI_KINYARWANDA_LIVE", raising=False)
    with pytest.raises(ValueError, match="empty"):
        await kv.synthesize("")
    with pytest.raises(ValueError, match="empty"):
        await kv.synthesize("   ")


@pytest.mark.asyncio
async def test_transcribe_routes_to_stub_by_default(monkeypatch):
    monkeypatch.delenv("MUNDI_KINYARWANDA_LIVE", raising=False)
    r = await kv.transcribe(b"\x00" * 32, mime="audio/ogg", language="rw")
    assert r.live is False
    assert r.provider == "stub-stt"


@pytest.mark.asyncio
async def test_synthesize_routes_to_stub_by_default(monkeypatch):
    monkeypatch.delenv("MUNDI_KINYARWANDA_LIVE", raising=False)
    r = await kv.synthesize("erekana imihindagurikire")
    assert r.live is False
    assert r.provider == "stub-tts"
    assert r.audio_bytes.startswith(b"OggS")


# ---------------------------------------------------------------------------
# MIME → extension table
# ---------------------------------------------------------------------------

def test_mime_table_covers_whatsapp_telegram_uploads():
    # WhatsApp / Telegram both deliver OGG/Opus
    assert kv._MIME_TO_EXT["audio/ogg"] == "ogg"
    assert kv._MIME_TO_EXT["audio/ogg; codecs=opus"] == "ogg"
    # Web uploads
    assert kv._MIME_TO_EXT["audio/mpeg"] == "mp3"
    assert kv._MIME_TO_EXT["audio/wav"] == "wav"


@pytest.mark.asyncio
async def test_whisper_stt_passes_openai_timeout(monkeypatch):
    calls = []

    class FakeTranscriptions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text="muraho")

    class FakeOpenAI:
        def __init__(self):
            self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())

    monkeypatch.setenv("MUNDI_OPENAI_AUDIO_TIMEOUT_SEC", "7.5")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeOpenAI))

    result = await kv.WhisperSTT().transcribe(b"audio", "audio/ogg", "rw")

    assert result.text == "muraho"
    assert calls[0]["timeout"] == 7.5


@pytest.mark.asyncio
async def test_openai_tts_returns_opus_mime_and_passes_timeout(monkeypatch):
    calls = []

    class FakeSpeechResponse:
        async def aread(self):
            return b"opus-bytes"

    class FakeSpeech:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return FakeSpeechResponse()

    class FakeOpenAI:
        def __init__(self):
            self.audio = SimpleNamespace(speech=FakeSpeech())

    monkeypatch.setenv("MUNDI_OPENAI_AUDIO_TIMEOUT_SEC", "8")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeOpenAI))

    result = await kv.OpenAITTS().synthesize("muraho", "rw")

    assert result.audio_bytes == b"opus-bytes"
    assert result.mime == "audio/opus"
    assert calls[0]["timeout"] == 8.0
