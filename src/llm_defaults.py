"""Default LLM profile for Sage/Hermes brain routing."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_LOCAL_BRAIN_MODEL = "ollama:gemma4:12b"
DEFAULT_CLOUD_BRAIN_MODEL = "gemma4:31b"
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
    endpoint. This lets hosted deployments use Ollama Cloud `gemma4:31b` while
    local Hermes/Sage defaults to `ollama:gemma4:12b`.
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
