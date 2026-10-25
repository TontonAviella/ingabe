"""scripts/container_watch.py: the textfile it writes for Prometheus."""

import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "container_watch", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "container_watch.py")
assert _spec is not None and _spec.loader is not None
watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch)


def _info(name, running=True, restarting=False, oom=False, started="2026-10-25T12:00:00.123456789Z", limit=0):
    return {"Name": f"/{name}", "RestartCount": 2, "HostConfig": {"Memory": limit},
            "State": {"Running": running, "Restarting": restarting, "Paused": False, "OOMKilled": oom,
                      "StartedAt": started}}


def _fake_docker(monkeypatch, infos, usage=500, inactive=100):
    def get(path):
        if path.startswith("/containers/json"):
            return [{"Id": i} for i in infos]
        cid = path.split("/")[2]
        if "/stats" in path:
            return {"memory_stats": {"usage": usage, "stats": {"inactive_file": inactive}}}
        return infos[cid]
    monkeypatch.setattr(watch, "docker_get", get)


def test_running_container_reports_memory_without_cache(monkeypatch):
    _fake_docker(monkeypatch, {"a": _info("mundi-app", limit=1000)})
    row = watch.container_rows("a")
    assert row["running"] == 1 and row["memory"] == 400 and row["memory_limit"] == 1000
    assert row["started_at"] == 1792929600.0  # 2026-10-25 12:00:00 UTC
    assert row["restart_count"] == 2


def test_restarting_container_counts_as_not_running(monkeypatch):
    _fake_docker(monkeypatch, {"a": _info("ingabe-redis-1", restarting=True, oom=True)})
    row = watch.container_rows("a")
    assert row["running"] == 0 and row["oom_killed"] == 1 and row["memory"] == 0


def test_never_started_and_mcp_containers(monkeypatch):
    _fake_docker(monkeypatch, {"a": _info("ingabe-minio-1", running=False, started="0001-01-01T00:00:00Z"),
                               "b": _info("grafana-mcp-123")})
    assert watch.container_rows("a")["started_at"] == 0.0
    assert watch.container_rows("b") is None


def test_textfile_is_written_whole(monkeypatch, tmp_path):
    _fake_docker(monkeypatch, {"a": _info("mundi-app"), "b": _info("ingabe-redis-1", running=False)})
    out = tmp_path / "containers.prom"
    monkeypatch.setattr(watch, "OUT", str(out))
    assert watch.write_once() == 2
    text = out.read_text()
    assert 'ingabe_container_running{name="mundi-app"} 1' in text
    assert 'ingabe_container_running{name="ingabe-redis-1"} 0' in text
    assert "ingabe_container_watch_last_run_seconds " in text
    assert not (tmp_path / "containers.prom.tmp").exists()
