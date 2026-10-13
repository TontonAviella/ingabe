from __future__ import annotations

from src.services import runtime_audit


def test_runtime_audit_distinguishes_installed_enabled_and_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_audit,
        "_package_status",
        lambda import_name, package_name: {
            "installed": import_name == "ultralytics",
            "version": "test",
        },
    )
    monkeypatch.setattr(
        runtime_audit,
        "_fastsam_weights_status",
        lambda: {"available": True, "path": "/app/FastSAM-s.pt"},
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    result = runtime_audit.build_runtime_audit()

    assert result["status"] == "healthy"
    assert result["runtime_policy"] == "local_only"
    assert result["components"]["fastsam"]["ready"] is True
    assert "hermes_sage" not in result["components"]
    assert "exclude" in result["components"]["harnessx"]["decision"]
