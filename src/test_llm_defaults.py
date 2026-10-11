from src.llm_defaults import (
    DEFAULT_CLOUD_BRAIN_MODEL,
    DEFAULT_CHAT_MODEL,
    DEFAULT_OLLAMA_BASE_URL,
    resolve_chat_endpoint,
    supports_strict_tool_schema,
    thinking_kwargs,
    usage_extra_body,
)
from src.utils import _uses_only_local_ollama_models


def test_default_chat_model_is_local_gemma4_brain():
    endpoint = resolve_chat_endpoint(None)

    assert DEFAULT_CHAT_MODEL == "ollama:gemma4:12b-it-qat"
    assert endpoint.model == "gemma4:12b-it-qat"
    assert endpoint.base_url == DEFAULT_OLLAMA_BASE_URL
    assert endpoint.api_key == "ollama"
    assert endpoint.is_local_ollama is True


def test_cloud_brain_model_is_gpt6_luna():
    assert DEFAULT_CLOUD_BRAIN_MODEL == "openai/gpt-6-luna"


def test_resolve_chat_endpoint_preserves_hosted_nemotron_config():
    endpoint = resolve_chat_endpoint(
        DEFAULT_CLOUD_BRAIN_MODEL,
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
    )

    assert endpoint.model == DEFAULT_CLOUD_BRAIN_MODEL
    assert endpoint.base_url == "https://openrouter.ai/api/v1"
    assert endpoint.api_key == "sk-test"
    assert endpoint.is_local_ollama is False


def test_local_ollama_default_does_not_require_cloud_credentials(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_FALLBACK_MODELS", raising=False)

    assert _uses_only_local_ollama_models() is True


def test_supported_brain_models_preserve_strict_tool_schemas():
    assert supports_strict_tool_schema("ollama:gemma4:12b-it-qat") is True
    assert supports_strict_tool_schema("gemma4:31b") is True
    assert supports_strict_tool_schema("google/gemma-4-31b-it") is True
    assert supports_strict_tool_schema(DEFAULT_CLOUD_BRAIN_MODEL) is True
    assert supports_strict_tool_schema("gpt-4.1") is True
    assert supports_strict_tool_schema("openai/gpt-6-luna") is True  # OpenAI models through OpenRouter
    assert supports_strict_tool_schema("nvidia/nemotron-3-super-120b-a12b:free") is True
    assert supports_strict_tool_schema("deepseek-chat") is False


def test_usage_field_goes_only_to_openrouter():
    # Google's OpenAI-compatible endpoint answers 400 'Unknown name "usage"' (recorded 2026-10-10).
    assert usage_extra_body("https://openrouter.ai/api/v1/") == {"usage": {"include": True}}
    assert usage_extra_body("https://generativelanguage.googleapis.com/v1beta/openai/") == {}
    assert usage_extra_body("http://ollama:11434/v1") == {}
    assert usage_extra_body(None) == {}


def test_thinking_is_a_budget_on_anthropic_and_an_effort_elsewhere():
    # Anthropic's OpenAI-compatible endpoint ignores reasoning_effort and refuses adaptive thinking (2026-10-10).
    anthropic = thinking_kwargs("https://api.anthropic.com/v1/", "high")
    assert anthropic["extra_body"] == {"thinking": {"type": "enabled", "budget_tokens": 8000}}
    assert anthropic["max_tokens"] > 8000
    assert "reasoning_effort" not in anthropic
    assert thinking_kwargs("https://openrouter.ai/api/v1", "low") == {
        "reasoning_effort": "low", "extra_body": {"usage": {"include": True}}}
    assert thinking_kwargs("https://generativelanguage.googleapis.com/v1beta/openai/", "high") == {
        "reasoning_effort": "high", "extra_body": {}}
