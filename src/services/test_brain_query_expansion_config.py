from src.services.brain_embeddings import _resolve_query_expansion_endpoint


def _clear_chat_env(monkeypatch):
    for name in (
        "BRAIN_QUERY_EXPANSION_MODEL",
        "OPENAI_MODEL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OLLAMA_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_query_expansion_defaults_to_local_gemma4(monkeypatch):
    _clear_chat_env(monkeypatch)

    endpoint = _resolve_query_expansion_endpoint()

    assert endpoint.model == "gemma4:12b-it-qat"
    assert endpoint.api_key == "ollama"
    assert endpoint.is_local_ollama is True


def test_query_expansion_can_use_hosted_nemotron(monkeypatch):
    _clear_chat_env(monkeypatch)
    monkeypatch.setenv("OPENAI_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")

    endpoint = _resolve_query_expansion_endpoint()

    assert endpoint.model == "nvidia/nemotron-3-super-120b-a12b:free"
    assert endpoint.base_url == "https://openrouter.ai/api/v1"
    assert endpoint.api_key == "sk-test"
    assert endpoint.is_local_ollama is False


def test_query_expansion_prefers_explicit_local_gemma4(monkeypatch):
    _clear_chat_env(monkeypatch)
    monkeypatch.setenv("OPENAI_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
    monkeypatch.setenv("BRAIN_QUERY_EXPANSION_MODEL", "ollama:gemma4:12b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1")

    endpoint = _resolve_query_expansion_endpoint()

    assert endpoint.model == "gemma4:12b"
    assert endpoint.base_url == "http://host.docker.internal:11434/v1"
    assert endpoint.api_key == "ollama"
    assert endpoint.is_local_ollama is True
