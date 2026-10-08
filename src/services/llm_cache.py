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
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Optional

from src.utils import get_async_s3_client, get_bucket_name

logger = logging.getLogger(__name__)

_STORE_PREFIX = "llm_cache/v1"


@dataclass
class Spend:
    """What calls of one kind cost since the app started."""

    calls: int = 0
    kept: int = 0  # answers served from the store, free
    cost_usd: float = 0.0
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


def record(kind: str, usage: Any) -> float:
    """Count one call's usage under `kind` and log it; returns its cost in USD (0 when the provider gave none)."""
    cost = float(_field(usage, "cost") or 0)
    prompt = int(_field(usage, "prompt_tokens") or 0)
    details = _field(usage, "prompt_tokens_details")
    cached = int(_field(details, "cached_tokens") or 0)
    output = int(_field(usage, "completion_tokens") or 0)
    spend = _spent.setdefault(kind, Spend())
    spend.calls += 1
    spend.cost_usd += cost
    spend.prompt_tokens += prompt
    spend.cached_tokens += cached
    spend.output_tokens += output
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
