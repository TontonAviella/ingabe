"""Keeping what the language model answered, so the same question is never paid for twice, and counting what
every call costs.

Two savings, both measured on GPT-6 Luna through OpenRouter (2026-10-07):
- Answers kept: a call whose answer depends only on its input (a plot's pictures, a document, a chat title,
  search variants of a query) is stored under a hash of the model and the whole input; the same input again
  returns the stored answer at no cost. Kept in object storage: llm_cache/v1/<kind>/<sha>.json.
- The provider's prompt cache: a prompt whose start repeats an earlier one is served from OpenRouter's cache
  at about a tenth of the input price (3,232 cached tokens cost $0.000036 against $0.00041). Writing the
  cache costs 25% more than plain input, once. Callers put the fixed text first and the changing parts
  (pictures, the question) last; `record` logs how much of each prompt came from the cache.

What a call cost comes from the provider when it says (OpenRouter's usage `cost`); Anthropic's OpenAI-compatible
endpoint says nothing, so its calls are priced here from their tokens and the published per-model prices.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Optional

from src.llm_defaults import model_base
from src.utils import get_async_s3_client, get_bucket_name

logger = logging.getLogger(__name__)

_STORE_PREFIX = "llm_cache/v1"

# USD per million tokens (input, output), from Anthropic's price list (2026-10). Haiku 5.5 charges more once a
# prompt passes 100k tokens. Used only when the provider sends no cost of its own.
_LONG_PROMPT_TOKENS = 100_000
_PRICES_PER_MILLION: dict[str, tuple[tuple[float, float], Optional[tuple[float, float]]]] = {
    "claude-haiku-5-5": ((0.10, 0.50), (0.50, 2.50)),
    "claude-sonnet-5-5": ((2.0, 10.0), None),
    "claude-opus-5-5": ((4.0, 20.0), None),
}


@dataclass
class Spend:
    """What calls of one kind cost since the app started."""

    calls: int = 0
    kept: int = 0  # answers served from the store, free
    cost_usd: float = 0.0
    unpriced: int = 0  # calls whose cost nobody could tell (no provider cost, no known price)
    prompt_tokens: int = 0
    cached_tokens: int = 0  # prompt tokens served from the provider's cache
    output_tokens: int = 0


_spent: dict[str, Spend] = {}


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    value = getattr(obj, name, None)
    if value is None:
        value = (getattr(obj, "model_extra", None) or {}).get(name)
    return value


def price(model: Optional[str], prompt_tokens: int, output_tokens: int) -> Optional[float]:
    """What a call cost in USD from its tokens and the model's published prices; None for a model with no known price.
    A model id with a date suffix ("claude-haiku-5-5-20261001") is priced as its base model."""
    base = model_base(model, _PRICES_PER_MILLION)
    if base is None:
        return None
    short, long = _PRICES_PER_MILLION[base]
    per_input, per_output = long if long is not None and prompt_tokens > _LONG_PROMPT_TOKENS else short
    return (prompt_tokens * per_input + output_tokens * per_output) / 1_000_000


def record(kind: str, usage: Any, model: Optional[str] = None) -> float:
    """Count one call's usage under `kind` and log it; returns its cost in USD. The provider's own cost wins; without
    it the cost is priced from the tokens (`price`). A call nobody can price counts as unpriced and returns 0."""
    prompt = int(_field(usage, "prompt_tokens") or 0)
    details = _field(usage, "prompt_tokens_details")
    cached = int(_field(details, "cached_tokens") or 0)
    output = int(_field(usage, "completion_tokens") or 0)
    reported = _field(usage, "cost")
    cost = float(reported) if reported is not None else price(model, prompt, output)
    spend = _spent.setdefault(kind, Spend())
    spend.calls += 1
    spend.prompt_tokens += prompt
    spend.cached_tokens += cached
    spend.output_tokens += output
    if cost is None:
        spend.unpriced += 1
        logger.info("llm %s: cost unknown (no price for %s), prompt %d tokens (%d from the provider's cache), output %d",
                    kind, model, prompt, cached, output)
        return 0.0
    spend.cost_usd += cost
    logger.info("llm %s: $%.5f, prompt %d tokens (%d from the provider's cache), output %d", kind, cost, prompt, cached,
                output)
    return cost


def spent() -> dict[str, dict[str, Any]]:
    """Spending by kind since the app started, with the share of prompt tokens served from the provider's cache."""
    return {kind: {**asdict(s), "cost_usd": round(s.cost_usd, 5),
                   "cached_share": round(s.cached_tokens / s.prompt_tokens, 3) if s.prompt_tokens else None}
            for kind, s in sorted(_spent.items())}


def key_of(model: str, *parts: Any) -> str:
    """A hash of the model and everything it is shown; bytes (pictures, files) count by their content."""
    digest = hashlib.sha256(model.encode())
    for part in parts:
        if isinstance(part, (bytes, bytearray)):
            digest.update(hashlib.sha256(part).digest())
        else:
            digest.update(json.dumps(part, sort_keys=True, default=str).encode())
    return digest.hexdigest()


async def _load(kind: str, key: str) -> Optional[dict[str, Any]]:
    s3 = await get_async_s3_client()
    try:
        response = await s3.get_object(Bucket=get_bucket_name(), Key=f"{_STORE_PREFIX}/{kind}/{key}.json")
    except s3.exceptions.NoSuchKey:
        return None
    async with response["Body"] as body:
        return json.loads(await body.read())


async def _save(kind: str, key: str, value: dict[str, Any]) -> None:
    s3 = await get_async_s3_client()
    await s3.put_object(Bucket=get_bucket_name(), Key=f"{_STORE_PREFIX}/{kind}/{key}.json",
                        Body=json.dumps(value).encode(), ContentType="application/json")


async def answer(kind: str, key: str, compute: Callable[[], Awaitable[dict[str, Any]]]) -> tuple[dict[str, Any], bool]:
    """(the kept answer for `key`, True), or (`compute()`'s answer, kept for next time, False). Storage trouble
    never blocks the call: the answer is then computed (and paid for) as if nothing were kept."""
    try:
        kept = await _load(kind, key)
    except Exception:
        logger.warning("llm cache read failed for %s", kind, exc_info=True)
        kept = None
    if kept is not None:
        _spent.setdefault(kind, Spend()).kept += 1
        return kept, True
    value = await compute()
    try:
        await _save(kind, key, value)
    except Exception:
        logger.warning("llm cache write failed for %s", kind, exc_info=True)
    return value, False
