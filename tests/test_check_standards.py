"""Tests for scripts/check_standards.py (the CODING_STANDARDS.md gate).

Each test builds a tiny fake repo under tmp_path and points the gate at it.
Runs without the app stack: `python -m pytest tests/test_check_standards.py --noconftest`.
"""

import importlib.util
import io
import json
import sys
import textwrap
from collections import Counter
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_standards", Path(__file__).resolve().parent.parent / "scripts" / "check_standards.py"
)
cs = importlib.util.module_from_spec(_SPEC)
sys.modules["check_standards"] = cs  # dataclasses resolve annotations via sys.modules
_SPEC.loader.exec_module(cs)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "ROOT", tmp_path)
    monkeypatch.setattr(cs, "BASELINE_PATH", tmp_path / "scripts" / "standards_baseline.json")
    (tmp_path / "scripts").mkdir()

    def write(rel: str, body: str) -> None:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")

    return write


def _rules(violations):
    return Counter(v.rule for v in violations)


def test_lazy_internal_import_in_domain_is_flagged(repo):
    repo("src/services/a.py", """
        def f():
            from src.services.b import g
            return g()
    """)
    found = cs.check_domain_imports()
    assert [v.detail for v in found] == ["f->src.services.b"]
    assert found[0].rule == "lazy-import"


def test_lazy_marker_third_party_and_module_level_are_allowed(repo):
    repo("src/services/a.py", """
        from src.services.b import g

        def f():
            import torch  # third-party lazy import is not cycle-dodging
            from src.services.c import heavy  # lazy: pulls in GDAL, only needed here
            return g(), heavy, torch
    """)
    assert cs.check_domain_imports() == []


def test_web_layer_import_in_domain_is_flagged(repo):
    repo("src/services/a.py", """
        from fastapi import Request
        from src.dependencies.db import get_pool
    """)
    found = cs.check_domain_imports()
    assert sorted(v.detail for v in found) == ["<module>->fastapi", "<module>->src.dependencies"]
    assert _rules(found) == {"web-import": 2}


def test_private_import_across_modules_is_flagged(repo):
    repo("src/routes/r.py", """
        from src.services.a import _helper, __version__, public
        from .sibling import _ok_shape
    """)
    found = cs.check_private_imports()
    assert sorted(v.detail for v in found) == ["src.routes.sibling._ok_shape", "src.services.a._helper"]


def test_private_import_in_tests_is_ignored(repo):
    repo("src/services/test_a.py", "from src.services.a import _helper\n")
    assert cs.check_private_imports() == []


def test_render_threshold_python(repo):
    repo("src/routes/r.py", """
        def view(ndvi, risk_count, series):
            if ndvi < 0.3:
                return "stressed"
            if risk_count > 0 or len(series) < 4:
                return "x"
    """)
    found = cs.check_render_thresholds_py()
    assert [v.detail for v in found] == ["view:ndvi<0.3"]


def test_render_threshold_typescript(repo):
    repo("frontendts/src/C.tsx", """
        const colour = ndvi < 0.2 ? "red" : "green";
        const enough = ndviPoints.length < 4;
        // ndvi < 0.5 in a comment is ignored
    """)
    found = cs.check_render_thresholds_ts()
    assert [v.detail for v in found] == ["ndvi<0.2"]


def test_duplicate_bodies(repo):
    body = """
        def {name}(x):
            \"\"\"doc differs: {name}\"\"\"
            y = x + 1
            z = y * 2
            if z > 3:
                return z
            return y
    """
    repo("src/services/a.py", body.format(name="one"))
    repo("src/routes/b.py", body.format(name="two"))
    repo("src/services/c.py", "def tiny(x):\n    return x\n\ndef tiny2(x):\n    return x\n")
    found = cs.check_duplicate_bodies()
    assert sorted((v.path, v.detail) for v in found) == [
        ("src/routes/b.py", "two"),
        ("src/services/a.py", "one"),
    ]


def _v(detail: str) -> "cs.Violation":
    return cs.Violation("web-import", "src/services/a.py", 1, detail, "msg")


def test_ratchet_passes_when_current_equals_baseline():
    v = [_v("x")]
    assert cs.evaluate(v, Counter({v[0].key: 1}), base=None) == []


def test_ratchet_fails_on_new_violation():
    failures = cs.evaluate([_v("x"), _v("y")], Counter({_v("x").key: 1}), base=None)
    assert failures[0].startswith("NEW violations (1)")


def test_ratchet_fails_on_stale_baseline_entry():
    failures = cs.evaluate([], Counter({_v("x").key: 1}), base=None)
    assert failures[0].startswith("FIXED debt still listed")


def test_ratchet_fails_when_baseline_grows_versus_base():
    key = _v("x").key
    failures = cs.evaluate([_v("x")], Counter({key: 1}), base=Counter())
    assert any("grew versus the base branch" in f for f in failures)


def test_hook_exit_codes(repo, monkeypatch, capsys):
    repo("src/services/a.py", "from fastapi import Request\n")
    cs.BASELINE_PATH.write_text(json.dumps({"violations": {}}))

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"stop_hook_active": False})))
    assert cs.main(["--hook"]) == 2  # Claude must fix before stopping

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"stop_hook_active": True})))
    assert cs.main(["--hook"]) == 1  # already continuing: warn, don't loop
    assert "Coding-standards gate failed" in capsys.readouterr().err


def test_write_baseline_then_gate_passes(repo):
    repo("src/services/a.py", "from fastapi import Request\n")
    assert cs.main(["--write-baseline"]) == 0
    assert cs.main([]) == 0


def test_caplog_fixture_is_flagged_in_tests(repo):
    repo("src/services/test_thing.py", """
        def test_bad(caplog):
            assert "x" in caplog.text

        async def test_good(monkeypatch):
            pass
    """)
    found = cs.check_caplog_fixture()
    assert [(v.rule, v.detail) for v in found] == [("caplog", "test_bad")]


def test_profiled_compose_service_needs_mem_limit(repo):
    repo("docker-compose.yml", """
        services:
          app:
            image: app
          extra-capped:
            image: x
            profiles: ["extra"]
            mem_limit: 512m
          extra-uncapped:
            image: y
            profiles: ["extra"]
            environment:
              A: "1"
        volumes:
          data:
    """)
    found = cs.check_compose_mem_limits()
    assert [(v.rule, v.detail) for v in found] == [("compose-mem-limit", "extra-uncapped")]


def test_long_running_compose_service_needs_restart_policy(repo):
    repo("docker-compose.yml", """
        services:
          app:
            image: app
            restart: unless-stopped
            depends_on:
              init:
                condition: service_completed_successfully
          init:
            image: busybox
          db:
            image: postgres
        volumes:
          data:
    """)
    found = cs.check_compose_restart_policies()
    assert [(v.rule, v.detail) for v in found] == [("compose-restart", "db")]


def test_claude_md_must_only_import_agents_md(repo):
    repo("AGENTS.md", "# AGENTS.md\n\n## Build\n")
    repo("CLAUDE.md", "# CLAUDE.md\n\n@AGENTS.md\n")
    assert cs.check_agents_md_sync() == []
    repo("CLAUDE.md", "# CLAUDE.md\n\n## Build\n\n<!-- gitnexus:start -->\n")
    found = cs.check_agents_md_sync()
    assert [v.rule for v in found] == ["agents-md-sync"] * 3
    assert found[0].detail == "missing @AGENTS.md import"



def test_src_module_named_like_a_dependency_is_flagged(repo):
    repo("requirements.txt", 'duckdb==1.3.2\nPyYAML==6.0\nuvicorn[standard]==0.49.0 ; python_version >= "3.9"\n')
    repo("src/geoparquet_cache.py", "x = 1\n")
    assert cs.check_shadowed_packages() == []
    repo("src/duckdb.py", "x = 1\n")
    repo("src/uvicorn/__init__.py", "")
    found = cs.check_shadowed_packages()
    assert [(v.rule, v.detail) for v in found] == [("shadow-package", "duckdb"), ("shadow-package", "uvicorn")]


def test_number_tested_for_truth_before_rounding_is_flagged(repo):
    repo("src/routes/r.py", """
        def row(r, x):
            a = round(r[3], 4) if r[3] else None
            b = float(x) if x else None
            c = round(float(x), 2) if x else None
            ok1 = round(r[3], 4) if r[3] is not None else None
            ok2 = round(r[4], 4) if r[3] else None
            return a, b, c, ok1, ok2
    """)
    found = cs.check_zero_as_missing()
    assert [(v.rule, v.line) for v in found] == [
        ("zero-as-missing", 3), ("zero-as-missing", 4), ("zero-as-missing", 5),
    ]
