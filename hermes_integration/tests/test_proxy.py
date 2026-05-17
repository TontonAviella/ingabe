"""Unit tests for hermes_integration/plugins/ingabe-sage/proxy.py.

The proxy is the wire between Hermes (running tools) and mundi-app (executing
them). These tests stub httpx to verify:

  - HMAC signature matches what mundi-app's hermes_auth.py will accept
  - Request body, URL, headers conform to the /internal/tool-call contract
  - Each upstream status code maps to the right structured error JSON

We do NOT hit the network. We do NOT require Hermes Agent to be installed.
We load proxy.py via importlib because the plugin directory contains a
hyphen ("ingabe-sage"), which isn't a valid Python identifier.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

import pytest


_PLUGIN_ROOT = (
    Path(__file__).resolve().parent.parent / "plugins" / "ingabe-sage"
)


def _load_proxy_module() -> ModuleType:
    """Load proxy.py under the qualified name `ingabe_sage.proxy`.

    Relies on context.py being already loaded under `ingabe_sage.context`
    because proxy.py does `from .context import get_ingabe_context`.
    """
    if "ingabe_sage.proxy" in sys.modules:
        return sys.modules["ingabe_sage.proxy"]

    # Make sure the parent package + context submodule are loaded first.
    if "ingabe_sage" not in sys.modules:
        parent_spec = importlib.util.spec_from_file_location(
            "ingabe_sage",
            _PLUGIN_ROOT / "__init__.py",
            submodule_search_locations=[str(_PLUGIN_ROOT)],
        )
        assert parent_spec is not None and parent_spec.loader is not None
        parent_mod = importlib.util.module_from_spec(parent_spec)
        sys.modules["ingabe_sage"] = parent_mod
        # Don't exec parent yet — its imports would trigger context/proxy.
    # Load context first so `from .context import ...` resolves on proxy load.
    if "ingabe_sage.context" not in sys.modules:
        ctx_spec = importlib.util.spec_from_file_location(
            "ingabe_sage.context", _PLUGIN_ROOT / "context.py"
        )
        assert ctx_spec is not None and ctx_spec.loader is not None
        ctx_mod = importlib.util.module_from_spec(ctx_spec)
        sys.modules["ingabe_sage.context"] = ctx_mod
        ctx_spec.loader.exec_module(ctx_mod)

    proxy_spec = importlib.util.spec_from_file_location(
        "ingabe_sage.proxy", _PLUGIN_ROOT / "proxy.py"
    )
    assert proxy_spec is not None and proxy_spec.loader is not None
    proxy_mod = importlib.util.module_from_spec(proxy_spec)
    sys.modules["ingabe_sage.proxy"] = proxy_mod
    proxy_spec.loader.exec_module(proxy_mod)
    return proxy_mod


# ---------------------------------------------------------------------------
# Fake httpx response + client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "{}") -> None:
        self.status_code = status_code
        self.text = text


class _FakeHttpxClient:
    """Captures the single POST call the proxy makes per dispatch.

    Set `response` (or `exc` to raise) before letting the proxy call into
    the client. After the call, inspect `.last_post` for url/body/headers.
    """

    def __init__(
        self,
        response: Optional[_FakeResponse] = None,
        exc: Optional[BaseException] = None,
    ) -> None:
        self.response = response or _FakeResponse(200, "{}")
        self.exc = exc
        self.last_post: dict[str, Any] = {}

    def __enter__(self) -> "_FakeHttpxClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def post(self, url: str, content: bytes, headers: dict) -> _FakeResponse:
        self.last_post = {"url": url, "content": content, "headers": headers}
        if self.exc is not None:
            raise self.exc
        return self.response


# ---------------------------------------------------------------------------
# Test fixtures — set a complete IngabeContext + the gateway secret
# ---------------------------------------------------------------------------


@pytest.fixture
def fully_configured(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load proxy with secret + IngabeContext available via env."""
    proxy = _load_proxy_module()
    # Resolve the same context module the proxy will read from.
    ctx_mod = sys.modules["ingabe_sage.context"]

    monkeypatch.setenv("HERMES_GATEWAY_SECRET", "test-shared-secret-xxxxxxxxxxxxxxxx")
    monkeypatch.setenv("MUNDI_APP_URL", "http://mundi-app-test:8000")

    # Drop the contextvar in case a previous test set it.
    ctx_mod._current_context.set(None)

    monkeypatch.setenv("INGABE_USER_UUID", "user-aaaa")
    monkeypatch.setenv("INGABE_PARTNER_ID", "partner-bk")
    monkeypatch.setenv("INGABE_CONVERSATION_ID", "42")
    monkeypatch.setenv("INGABE_MAP_ID", "map-xyz")

    return proxy


# ---------------------------------------------------------------------------
# Happy path: proxy signs and POSTs to /internal/tool-call
# ---------------------------------------------------------------------------


def test_proxy_signs_body_with_hmac_sha256_and_posts_to_internal_tool_call(
    fully_configured: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mundi-app's hermes_auth.py verifies hex(HMAC-SHA256(secret, body)).
    This test reconstructs the same digest from the captured body bytes and
    asserts the proxy sends a matching X-Hermes-Signature header."""
    proxy = fully_configured
    fake = _FakeHttpxClient(
        response=_FakeResponse(200, json.dumps({"result": {"value": 0.62}}))
    )
    monkeypatch.setattr(proxy.httpx, "Client", lambda **kw: fake)

    result_str = proxy.proxy_tool_call(
        tool_name="compute_zonal_stats",
        arguments={"layer_id": "L1", "band": 1},
        task_id="t-abc",
    )

    # URL: configured base + /internal/tool-call
    assert fake.last_post["url"] == "http://mundi-app-test:8000/internal/tool-call"

    # Body: well-formed JSON, includes IngabeContext fields, tool_name, args
    body_bytes: bytes = fake.last_post["content"]
    body = json.loads(body_bytes)
    assert body["tool_name"] == "compute_zonal_stats"
    assert body["arguments"] == {"layer_id": "L1", "band": 1}
    assert body["partner_id"] == "partner-bk"
    assert body["user_id"] == "user-aaaa"
    assert body["conversation_id"] == "42"

    # Signature: hex(HMAC-SHA256(secret, raw_body)) — must match the wire bytes
    expected_sig = hmac.new(
        b"test-shared-secret-xxxxxxxxxxxxxxxx",
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    assert fake.last_post["headers"]["X-Hermes-Signature"] == expected_sig
    assert fake.last_post["headers"]["Content-Type"] == "application/json"

    # Result passthrough: proxy returns the upstream body verbatim on 200
    parsed = json.loads(result_str)
    assert parsed == {"result": {"value": 0.62}}


# ---------------------------------------------------------------------------
# Error paths: each maps to a stable `status` taxonomy
# ---------------------------------------------------------------------------


def test_proxy_returns_upstream_unavailable_on_503(
    fully_configured: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 from mundi-app — either MUNDI_TOOL_CALL_ENABLED=0 or dispatch
    not wired yet. Both are expected during the staged PR #54 → PR #55
    rollout. Surface as upstream_unavailable so the LLM can apologize."""
    proxy = fully_configured
    fake = _FakeHttpxClient(response=_FakeResponse(503, "Service Unavailable"))
    monkeypatch.setattr(proxy.httpx, "Client", lambda **kw: fake)

    parsed = json.loads(proxy.proxy_tool_call("any_tool", {}, task_id="t"))
    assert parsed["status"] == "upstream_unavailable"
    assert parsed["upstream_status"] == 503


def test_proxy_returns_auth_failed_on_401(
    fully_configured: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401 means the gateway and app disagreed on the shared secret."""
    proxy = fully_configured
    fake = _FakeHttpxClient(response=_FakeResponse(401, "unauthorized"))
    monkeypatch.setattr(proxy.httpx, "Client", lambda **kw: fake)

    parsed = json.loads(proxy.proxy_tool_call("any_tool", {}, task_id="t"))
    assert parsed["status"] == "auth_failed"


def test_proxy_returns_upstream_error_on_500(
    fully_configured: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """500 = mundi-app crashed dispatching — preserve the status for ops."""
    proxy = fully_configured
    fake = _FakeHttpxClient(response=_FakeResponse(500, "internal error"))
    monkeypatch.setattr(proxy.httpx, "Client", lambda **kw: fake)

    parsed = json.loads(proxy.proxy_tool_call("any_tool", {}, task_id="t"))
    assert parsed["status"] == "upstream_error"
    assert parsed["upstream_status"] == 500


def test_proxy_returns_network_error_on_connect_failure(
    fully_configured: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When httpx itself raises (connection refused, DNS, timeout), the
    proxy must catch it and return JSON. Hermes treats raised exceptions
    as turn-killers; we want one bad tool call to be recoverable."""
    proxy = fully_configured
    fake = _FakeHttpxClient(exc=proxy.httpx.ConnectError("connection refused"))
    monkeypatch.setattr(proxy.httpx, "Client", lambda **kw: fake)

    parsed = json.loads(proxy.proxy_tool_call("any_tool", {}, task_id="t"))
    assert parsed["status"] == "network_error"


def test_proxy_returns_upstream_error_on_non_json_2xx(
    fully_configured: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mundi-app must return JSON on 2xx. If something proxies HTML in
    (nginx config drift, etc.), surface that as an error rather than
    letting the LLM choke on the raw HTML."""
    proxy = fully_configured
    fake = _FakeHttpxClient(response=_FakeResponse(200, "<html>oops</html>"))
    monkeypatch.setattr(proxy.httpx, "Client", lambda **kw: fake)

    parsed = json.loads(proxy.proxy_tool_call("any_tool", {}, task_id="t"))
    assert parsed["status"] == "upstream_error"


# ---------------------------------------------------------------------------
# Pre-flight: missing secret / missing context (before any HTTP attempt)
# ---------------------------------------------------------------------------


def test_proxy_short_circuits_with_config_error_when_secret_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _load_proxy_module()
    monkeypatch.delenv("HERMES_GATEWAY_SECRET", raising=False)
    # Also clear context so we hit the secret check first
    for k in list(__import__("os").environ):
        if k.startswith("INGABE_"):
            monkeypatch.delenv(k, raising=False)

    parsed = json.loads(proxy.proxy_tool_call("any_tool", {}, task_id="t"))
    assert parsed["status"] == "config_error"
    # Should never have constructed an httpx client — no last_post to inspect


def test_proxy_short_circuits_with_context_missing_when_no_partner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _load_proxy_module()
    ctx_mod = sys.modules["ingabe_sage.context"]
    ctx_mod._current_context.set(None)

    monkeypatch.setenv("HERMES_GATEWAY_SECRET", "secret-yes")
    for k in list(__import__("os").environ):
        if k.startswith("INGABE_"):
            monkeypatch.delenv(k, raising=False)

    parsed = json.loads(proxy.proxy_tool_call("any_tool", {}, task_id="t"))
    assert parsed["status"] == "context_missing"


# ---------------------------------------------------------------------------
# make_proxy_handler factory
# ---------------------------------------------------------------------------


def test_make_proxy_handler_closes_over_tool_name(
    fully_configured: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """make_proxy_handler(name)(args, **kw) must dispatch with the closed-
    over name, not whatever's in args or kw."""
    proxy = fully_configured
    captured = {}

    def fake_proxy(*, tool_name: str, arguments: dict, task_id: Any) -> str:
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        captured["task_id"] = task_id
        return json.dumps({"ok": True})

    monkeypatch.setattr(proxy, "proxy_tool_call", fake_proxy)

    handler = proxy.make_proxy_handler("compute_zonal_stats")
    result = handler({"layer_id": "L1"}, task_id="task-7")

    assert captured == {
        "tool_name": "compute_zonal_stats",
        "arguments": {"layer_id": "L1"},
        "task_id": "task-7",
    }
    assert json.loads(result) == {"ok": True}
