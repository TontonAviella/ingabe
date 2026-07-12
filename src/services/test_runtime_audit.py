from __future__ import annotations

from src.services import runtime_audit


def test_runtime_audit_distinguishes_installed_enabled_and_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_audit,
        "_package_status",
        lambda import_name, package_name: {
            "installed": import_name in {"ultralytics", "hermes_cli"},
            "version": "test",
        },
    )
    monkeypatch.setattr(
        runtime_audit,
        "_fastsam_weights_status",
        lambda: {"available": True, "path": "/app/FastSAM-s.pt"},
    )
    monkeypatch.setattr(
        runtime_audit,
        "geolibre_runner_status",
        lambda include_manifest_sample=False: {"status": "success", "tool_count": 747},
    )
    monkeypatch.setattr(runtime_audit.Path, "is_dir", lambda _self: True)
    monkeypatch.delenv("MUNDI_USE_HERMES", raising=False)
    monkeypatch.delenv("MUNDI_TOOL_CALL_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SECRET", raising=False)

    result = runtime_audit.build_runtime_audit()

    assert result["status"] == "healthy"
    assert result["runtime_policy"] == "local_only"
    assert result["components"]["fastsam"]["ready"] is True
    assert result["components"]["hermes_sage"]["installed"] is True
    assert result["components"]["hermes_sage"]["enabled"] is False
    assert result["components"]["hermes_sage"]["ready"] is False
    assert "exclude" in result["components"]["harnessx"]["decision"]
