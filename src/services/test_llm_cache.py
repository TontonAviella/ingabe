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


def test_a_claude_call_without_a_provider_cost_is_priced_from_its_tokens(store):
    # The real usage object the openai SDK builds from Anthropic's OpenAI-compatible endpoint: tokens, no cost.
    from openai.types import CompletionUsage

    usage = CompletionUsage(prompt_tokens=2327, completion_tokens=808, total_tokens=3135)
    assert llm_cache.record("vision_plot_look", usage, "claude-haiku-5-5") == pytest.approx(0.0006367)
    assert llm_cache.spent()["vision_plot_look"]["cost_usd"] == pytest.approx(0.00064, abs=0.00001)


def test_a_prompt_over_100k_tokens_is_priced_at_the_long_prompt_rate():
    assert llm_cache.price("claude-haiku-5-5", 120_000, 1_000) == pytest.approx(0.0625)  # 0.06 + 0.0025
    assert llm_cache.price("claude-haiku-5-5", 100_000, 1_000) == pytest.approx(0.0105)  # at the limit: short rate


def test_a_dated_or_routed_model_id_is_priced_as_its_base_model():
    assert llm_cache.price("claude-haiku-5-5-20261001", 50_000, 0) == pytest.approx(0.005)
    assert llm_cache.price("anthropic/claude-haiku-5-5", 0, 1_000_000) == pytest.approx(0.50)
    assert llm_cache.price("claude-haiku-5", 1_000, 1_000) is None  # a different model, not a prefix match


def test_a_call_nobody_can_price_is_counted_as_unpriced_not_as_free(store):
    assert llm_cache.record("sage", {"prompt_tokens": 500, "completion_tokens": 20}, "some-new-model") == 0.0
    assert llm_cache.record("sage", {"prompt_tokens": 500, "completion_tokens": 20}) == 0.0
    sage = llm_cache.spent()["sage"]
    assert sage["unpriced"] == 2 and sage["cost_usd"] == 0.0


def test_a_provider_cost_of_zero_is_a_real_zero_and_wins_over_the_price_table(store):
    usage = {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0}
    assert llm_cache.record("sage", usage, "claude-haiku-5-5") == 0.0
    assert llm_cache.spent()["sage"]["unpriced"] == 0
