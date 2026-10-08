"""Kept model answers and spending counts, with storage replaced by a dict (no paid calls)."""

from __future__ import annotations

import asyncio

import pytest

from src.services import llm_cache


@pytest.fixture
def store(monkeypatch):
    kept: dict[tuple[str, str], dict] = {}

    async def load(kind, key):
        return kept.get((kind, key))

    async def save(kind, key, value):
        kept[(kind, key)] = value

    monkeypatch.setattr(llm_cache, "_load", load)
    monkeypatch.setattr(llm_cache, "_save", save)
    monkeypatch.setattr(llm_cache, "_spent", {})
    return kept


def test_the_same_input_is_paid_for_once(store):
    calls = []

    async def ask():
        calls.append(1)
        return {"text": "Maize"}

    key = llm_cache.key_of("openai/gpt-6-luna", "system", b"picture bytes")
    first = asyncio.run(llm_cache.answer("vision", key, ask))
    again = asyncio.run(llm_cache.answer("vision", key, ask))
    other = asyncio.run(llm_cache.answer("vision", llm_cache.key_of("openai/gpt-6-luna", "system", b"other"), ask))
    assert first == ({"text": "Maize"}, False) and again == ({"text": "Maize"}, True) and other[1] is False
    assert len(calls) == 2 and llm_cache.spent()["vision"]["kept"] == 1


def test_storage_trouble_never_blocks_the_call(monkeypatch):
    async def broken(*args):
        raise ConnectionError("storage down")

    monkeypatch.setattr(llm_cache, "_load", broken)
    monkeypatch.setattr(llm_cache, "_save", broken)

    async def ask():
        return {"text": "title"}

    assert asyncio.run(llm_cache.answer("chat_title", "k", ask)) == ({"text": "title"}, False)


def test_usage_is_counted_with_the_share_served_from_the_provider_cache(store):
    usage = {"prompt_tokens": 3235, "completion_tokens": 12, "cost": 0.0000356,
             "prompt_tokens_details": {"cached_tokens": 3232}}
    assert llm_cache.record("sage", usage) == pytest.approx(0.0000356)
    llm_cache.record("sage", {"prompt_tokens": 1000, "completion_tokens": 5, "cost": 0.0001})
    sage = llm_cache.spent()["sage"]
    assert sage["calls"] == 2 and sage["prompt_tokens"] == 4235 and sage["cached_share"] == pytest.approx(0.763, abs=0.001)
