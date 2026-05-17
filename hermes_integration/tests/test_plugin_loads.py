"""Smoke tests for the ingabe-sage Hermes plugin.

These tests do NOT hit the network and do NOT require Hermes Agent to be
installed. They verify the plugin's static contract — manifest shape,
register() callable, schemas valid — using a fake `ctx` recorder.

Run from this worktree:
    python -m pytest hermes_integration/tests/

Run inside mundi-app container:
    docker exec mundi-app python -m pytest /app/hermes_integration/tests/

Why these tests exist: the Hermes plugin scaffolding is Phase-2 prerequisite
code that won't be exercised by mundi.ai's test suite for weeks. Without
these smoke tests, a typo in plugin.yaml or a rename in generated_tools.py
silently breaks Hermes integration with no signal.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import pytest

# Plugin lives at a directory with a hyphen ("ingabe-sage"), which isn't a
# valid Python package identifier. Load via importlib.util.spec_from_file_location
# instead of relying on the standard import system.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
_PLUGIN_ROOT = _PLUGIN_DIR / "ingabe-sage"


def _load_plugin_module() -> ModuleType:
    """Load `ingabe-sage/__init__.py` as a module named `ingabe_sage`.

    Cached on sys.modules so submodule relative imports (`.tools`,
    `.context`, `.generated_tools`) resolve. Also pre-registers each
    submodule.
    """
    if "ingabe_sage" in sys.modules:
        return sys.modules["ingabe_sage"]
    # Add the plugin dir to sys.path so submodule loading works.
    sys.path.insert(0, str(_PLUGIN_ROOT.parent))
    # Pre-register the submodules so `from .tools import ...` resolves
    spec = importlib.util.spec_from_file_location(
        "ingabe_sage",
        _PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingabe_sage"] = module
    # Load submodules too so __init__.py's `from .tools import ...` works
    for sub in ("context", "tools", "generated_tools", "async_bridge", "proxy"):
        sub_spec = importlib.util.spec_from_file_location(
            f"ingabe_sage.{sub}", _PLUGIN_ROOT / f"{sub}.py"
        )
        if sub_spec is None or sub_spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(sub_spec)
        sys.modules[f"ingabe_sage.{sub}"] = sub_mod
        sub_spec.loader.exec_module(sub_mod)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fake plugin context recorder
# ---------------------------------------------------------------------------


class _FakeCtx:
    """Records calls to ctx.register_tool(...) so we can assert what the
    plugin tried to register. Mirrors Hermes's real PluginContext minimally."""

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []

    def register_tool(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., str],
        check_fn: Callable[[], bool] | None = None,
        emoji: str | None = None,
    ) -> None:
        self.tools.append({
            "name": name,
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
            "emoji": emoji,
        })


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------


def test_plugin_manifest_exists_and_parses() -> None:
    """plugin.yaml must exist and be valid YAML with required fields."""
    import yaml
    manifest_path = _PLUGIN_DIR / "ingabe-sage" / "plugin.yaml"
    assert manifest_path.exists(), f"missing {manifest_path}"
    data = yaml.safe_load(manifest_path.read_text())
    assert data["name"] == "ingabe-sage"
    assert data["kind"] in {"standalone", "backend", "exclusive", "platform"}
    assert "version" in data
    assert isinstance(data.get("provides_tools", []), list)


def test_manifest_provides_tools_matches_register() -> None:
    """The tool names declared in plugin.yaml's provides_tools should match
    what register(ctx) actually registers in toolset 'ingabe-sage' (Tier 1)."""
    import yaml
    register = _load_plugin_module().register

    manifest = yaml.safe_load(
        (_PLUGIN_DIR / "ingabe-sage" / "plugin.yaml").read_text()
    )
    declared = set(manifest.get("provides_tools", []))

    ctx = _FakeCtx()
    register(ctx)
    tier1_actual = {t["name"] for t in ctx.tools if t["toolset"] == "ingabe-sage"}

    assert declared == tier1_actual, (
        f"manifest provides_tools {declared} doesn't match Tier 1 registrations "
        f"{tier1_actual}"
    )


# ---------------------------------------------------------------------------
# register(ctx) shape tests
# ---------------------------------------------------------------------------


def test_register_is_callable_with_fake_ctx() -> None:
    """register(ctx) should not error when given a minimal ctx with
    register_tool(). This is the contract Hermes's PluginManager relies on."""
    register = _load_plugin_module().register
    ctx = _FakeCtx()
    register(ctx)
    assert len(ctx.tools) > 0, "register() registered zero tools"


def test_register_emits_both_toolsets() -> None:
    """Plugin registers two toolsets:
        ingabe-sage          — Tier 1 native, runs in-process in the gateway
        ingabe-sage-proxied  — Tier 2 HMAC-proxy to mundi-app /internal/tool-call
    """
    register = _load_plugin_module().register
    ctx = _FakeCtx()
    register(ctx)
    toolsets = {t["toolset"] for t in ctx.tools}
    assert "ingabe-sage" in toolsets
    assert "ingabe-sage-proxied" in toolsets


def test_register_tier1_has_at_least_search_and_whoami() -> None:
    register = _load_plugin_module().register
    ctx = _FakeCtx()
    register(ctx)
    tier1_names = {t["name"] for t in ctx.tools if t["toolset"] == "ingabe-sage"}
    assert "search_location" in tier1_names
    assert "ingabe_whoami" in tier1_names


def test_register_tier2_has_expected_tool_count() -> None:
    """Tier 2 (proxied) should have many tools (60+ from tools.json plus
    Pydantic-derived). Loose assertion to allow codegen to evolve."""
    register = _load_plugin_module().register
    ctx = _FakeCtx()
    register(ctx)
    tier2_count = sum(1 for t in ctx.tools if t["toolset"] == "ingabe-sage-proxied")
    assert tier2_count >= 50, (
        f"Tier 2 should have many tools; got {tier2_count}. "
        "Did generated_tools.py get regenerated? Or import broken?"
    )


def test_no_duplicate_tool_names_across_toolsets() -> None:
    """A tool name must appear in exactly one toolset. Duplicate registration
    confuses Hermes's lookup and overrides handler dispatch."""
    register = _load_plugin_module().register
    ctx = _FakeCtx()
    register(ctx)
    names = [t["name"] for t in ctx.tools]
    seen: set[str] = set()
    dupes: set[str] = set()
    for n in names:
        if n in seen:
            dupes.add(n)
        seen.add(n)
    assert not dupes, f"Duplicate tool registrations: {dupes}"


# ---------------------------------------------------------------------------
# Schema validity tests
# ---------------------------------------------------------------------------


def test_all_schemas_have_required_openai_function_calling_shape() -> None:
    """Every registered schema must conform to OpenAI function-calling spec:
        { 'name': str, 'description': str, 'parameters': { 'type': 'object',
        'properties': {...}, 'required': [...] } }
    A malformed schema crashes the LLM call with HTTP 400 at runtime — fail
    here instead, where it's debuggable."""
    register = _load_plugin_module().register
    ctx = _FakeCtx()
    register(ctx)
    for t in ctx.tools:
        s = t["schema"]
        n = t["name"]
        assert isinstance(s, dict), f"{n}: schema is not a dict"
        assert s.get("name") == n, f"{n}: schema.name mismatch ({s.get('name')!r})"
        assert isinstance(s.get("description"), str), f"{n}: missing description"
        assert s["description"], f"{n}: empty description"
        params = s.get("parameters")
        assert isinstance(params, dict), f"{n}: parameters is not a dict"
        assert params.get("type") == "object", f"{n}: parameters.type != 'object'"
        # properties must be a dict (may be empty for no-arg tools)
        assert isinstance(params.get("properties", {}), dict), (
            f"{n}: parameters.properties not a dict"
        )
        # required must be a list if present
        if "required" in params:
            assert isinstance(params["required"], list), (
                f"{n}: parameters.required not a list"
            )


def test_all_handlers_are_callable() -> None:
    """Every registered handler must be callable with (args_dict, **kw)."""
    register = _load_plugin_module().register
    ctx = _FakeCtx()
    register(ctx)
    for t in ctx.tools:
        assert callable(t["handler"]), f"{t['name']}: handler not callable"


def test_proxy_handler_reports_config_error_when_secret_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proxied Tier 2 handlers must surface a structured config_error when
    HERMES_GATEWAY_SECRET is missing — that's an operator misconfiguration
    we want the LLM to see clearly, not a 500.
    """
    register = _load_plugin_module().register
    monkeypatch.delenv("HERMES_GATEWAY_SECRET", raising=False)
    ctx = _FakeCtx()
    register(ctx)
    handler = next(
        t for t in ctx.tools if t["toolset"] == "ingabe-sage-proxied"
    )["handler"]
    result = handler({"some": "args"}, task_id="cfg-test")
    assert isinstance(result, str), f"proxy handler returned {type(result)} not str"
    parsed = json.loads(result)
    assert parsed.get("status") == "config_error"
    assert "HERMES_GATEWAY_SECRET" in parsed.get("message", "")


def test_proxy_handler_reports_context_missing_when_no_partner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the secret IS configured but no IngabeContext is set (no
    partner_id / conversation_id), proxy handlers must short-circuit with
    context_missing rather than dispatching to mundi-app with empty IDs."""
    register = _load_plugin_module().register
    monkeypatch.setenv("HERMES_GATEWAY_SECRET", "test-secret-not-real")
    # Strip any INGABE_* env that could populate a default context.
    for k in list(os.environ):
        if k.startswith("INGABE_"):
            monkeypatch.delenv(k, raising=False)
    ctx = _FakeCtx()
    register(ctx)
    handler = next(
        t for t in ctx.tools if t["toolset"] == "ingabe-sage-proxied"
    )["handler"]
    result = handler({"layer_id": "L1"}, task_id="ctx-test")
    parsed = json.loads(result)
    assert parsed.get("status") == "context_missing"


def test_whoami_handler_returns_no_context_when_unset() -> None:
    """ingabe_whoami should report context_source='none' when no env vars
    are set and no contextvar is configured."""
    register = _load_plugin_module().register

    # Make sure no INGABE_* env vars leak from the parent shell
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("INGABE_")}
    try:
        ctx = _FakeCtx()
        register(ctx)
        whoami = next(t for t in ctx.tools if t["name"] == "ingabe_whoami")
        result = whoami["handler"]({}, task_id="whoami-test")
        parsed = json.loads(result)
        assert parsed.get("context_source") == "none"
        assert parsed.get("user_uuid") is None
    finally:
        # Restore env
        for k, v in saved.items():
            os.environ[k] = v


def test_whoami_handler_reads_env_context_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """ingabe_whoami should pull user_uuid + partner_id from INGABE_* env
    vars when no contextvar is set."""
    register = _load_plugin_module().register

    monkeypatch.setenv("INGABE_USER_UUID", "test-user-abc")
    monkeypatch.setenv("INGABE_PARTNER_ID", "bk-insurance")
    monkeypatch.setenv("INGABE_MAP_ID", "test-map-xyz")

    ctx = _FakeCtx()
    register(ctx)
    whoami = next(t for t in ctx.tools if t["name"] == "ingabe_whoami")
    result = whoami["handler"]({}, task_id="env-test")
    parsed = json.loads(result)
    assert parsed.get("user_uuid") == "test-user-abc"
    assert parsed.get("partner_id") == "bk-insurance"
    assert parsed.get("map_id") == "test-map-xyz"
    assert parsed.get("context_source") == "contextvar-or-env"
