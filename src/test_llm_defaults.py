from src.llm_defaults import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_OLLAMA_BASE_URL,
    resolve_chat_endpoint,
    supports_strict_tool_schema,
)
from src.utils import _uses_only_local_ollama_models


def test_default_chat_model_is_local_gemma4_brain():
    endpoint = resolve_chat_endpoint(None)

    assert DEFAULT_CHAT_MODEL == "ollama:gemma4:12b-it-qat"
    assert endpoint.model == "gemma4:12b-it-qat"
    assert endpoint.base_url == DEFAULT_OLLAMA_BASE_URL
    assert endpoint.api_key == "ollama"
    assert endpoint.is_local_ollama is True


def test_resolve_chat_endpoint_preserves_hosted_gemma4_config():
    endpoint = resolve_chat_endpoint(
        "gemma4:31b",
        api_key="sk-test",
        base_url="https://ollama.com/v1",
    )

    assert endpoint.model == "gemma4:31b"
    assert endpoint.base_url == "https://ollama.com/v1"
    assert endpoint.api_key == "sk-test"
    assert endpoint.is_local_ollama is False


def test_local_ollama_default_does_not_require_cloud_credentials(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_FALLBACK_MODELS", raising=False)

    assert _uses_only_local_ollama_models() is True


def test_gemma_and_ollama_preserve_strict_tool_schemas():
    assert supports_strict_tool_schema("ollama:gemma4:12b-it-qat") is True
    assert supports_strict_tool_schema("gemma4:31b") is True
    assert supports_strict_tool_schema("google/gemma-4-31b-it") is True
    assert supports_strict_tool_schema("gpt-4.1") is True
    assert supports_strict_tool_schema("deepseek-chat") is False
