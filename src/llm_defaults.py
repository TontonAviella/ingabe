"""Default LLM profile for Sage/Hermes brain routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


DEFAULT_LOCAL_BRAIN_MODEL = "ollama:gemma4:12b-it-qat"
# Roger's choice, 2026-10-06: GPT-6 Luna through OpenRouter (paid credits; vision, tools, structured output).
DEFAULT_CLOUD_BRAIN_MODEL = "openai/gpt-6-luna"
DEFAULT_CHAT_MODEL = DEFAULT_LOCAL_BRAIN_MODEL
DEFAULT_SMALL_TALK_MODEL = DEFAULT_LOCAL_BRAIN_MODEL
DEFAULT_BRAIN_QUERY_EXPANSION_MODEL = DEFAULT_LOCAL_BRAIN_MODEL
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"


@dataclass(frozen=True)
class ChatEndpoint:
    """OpenAI-compatible endpoint resolved from a model tag."""

    model: str
    base_url: str
    api_key: str
    is_local_ollama: bool


def resolve_chat_endpoint(
    model: str | None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    ollama_base_url: str | None = None,
) -> ChatEndpoint:
    """Resolve `ollama:<tag>` models to the local Ollama endpoint.

    Non-prefixed model names keep the caller's configured OpenAI-compatible
    endpoint. This lets hosted deployments use Nemotron Super 3 through an
    OpenAI-compatible gateway while local Hermes/Sage defaults to
    `ollama:gemma4:12b-it-qat`.
    """

    resolved_model = (model or DEFAULT_CHAT_MODEL).strip() or DEFAULT_CHAT_MODEL
    if resolved_model.startswith("ollama:"):
        return ChatEndpoint(
            model=resolved_model.split(":", 1)[1],
            base_url=(ollama_base_url or DEFAULT_OLLAMA_BASE_URL).strip()
            or DEFAULT_OLLAMA_BASE_URL,
            api_key="ollama",
            is_local_ollama=True,
        )

    return ChatEndpoint(
        model=resolved_model,
        base_url=(base_url or DEFAULT_OPENAI_BASE_URL).strip()
        or DEFAULT_OPENAI_BASE_URL,
        api_key=(api_key or "").strip(),
        is_local_ollama=False,
    )


def model_base(model: str | None, bases: Iterable[str]) -> str | None:
    """Which of `bases` a model id is a version of: a routed id ("anthropic/claude-haiku-5-5") or a dated one
    ("claude-haiku-5-5-20261001") is its base model; the longest base wins; None when it is none of them."""
    if not model:
        return None
    name = model.split("/")[-1]
    return max((base for base in bases if name == base or name.startswith(base + "-")), key=len, default=None)


# How a model is named to readers (the drone cards name the vision model). A model not listed here is named by its
# own id, never by a guessed name.
MODEL_NAMES = {
    "gpt-6-luna": "GPT-6 Luna",
    "claude-haiku-5-5": "Claude Haiku 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-opus-5-5": "Claude Opus 5.5",
    "gemini-3.8-flash": "Gemini 3.8 Flash",
    "gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite",
    "nemotron-3-super-120b-a12b:free": "Nemotron 3 Super",
    "gemma4:12b-it-qat": "Gemma 4 12B",
}


def model_name(model: str) -> str:
    """How a model id, as configured or as a provider reports it, is named to readers ("claude-haiku-5-5" is
    "Claude Haiku 5.5"); an id not in MODEL_NAMES is named by itself."""
    base = model_base(model, MODEL_NAMES)
    return MODEL_NAMES[base] if base is not None else model.split("/")[-1]


def supports_strict_tool_schema(model: str | None) -> bool:
    """Return whether we should preserve OpenAI strict tool schemas.

    Gemma/Ollama and Nemotron accept the `strict` function key and behave better
    with an explicit all-fields-required contract. Keep stripping it for unknown
    provider families that may reject OpenAI-specific schema extensions.
    """

    model_name = (model or DEFAULT_CHAT_MODEL).strip().lower().removeprefix("openai/")
    if model_name.startswith(("gpt-", "o1", "o3", "o4")):
        return True
    if model_name.startswith("ollama:"):
        return True
    return "gemma" in model_name or "nemotron" in model_name


def usage_extra_body(base_url: Any) -> dict[str, Any]:
    """Request fields that make the provider report each call's tokens and cost (llm_cache.record logs them).

    OpenRouter adds the cost when asked with its `usage` field. Other providers report tokens without it, and
    Google's OpenAI-compatible endpoint rejects it ("Unknown name \"usage\"", HTTP 400, 2026-10-10)."""
    return {"usage": {"include": True}} if "openrouter.ai" in str(base_url or "") else {}


# Anthropic's OpenAI-compatible endpoint ignores `reasoning_effort` and refuses adaptive thinking ("Adaptive
# thinking is not available via the OpenAI compatibility endpoint", HTTP 400, 2026-10-10); it takes a fixed
# thinking budget, which must fit under max_tokens with room left for the answer.
ANTHROPIC_THINKING_BUDGET = {"low": 1024, "medium": 4000, "high": 8000}
ANTHROPIC_ANSWER_TOKENS = 4096


def thinking_kwargs(base_url: Any, effort: str) -> dict[str, Any]:
    """Request fields that ask the provider to think at `effort` ("low", "medium", "high") before answering,
    with the fields that report the call's cost (usage_extra_body) already in `extra_body`."""
    extra_body = usage_extra_body(base_url)
    if "api.anthropic.com" in str(base_url or ""):
        budget = ANTHROPIC_THINKING_BUDGET[effort]
        return {"max_tokens": budget + ANTHROPIC_ANSWER_TOKENS,
                "extra_body": {**extra_body, "thinking": {"type": "enabled", "budget_tokens": budget}}}
    return {"reasoning_effort": effort, "extra_body": extra_body}
