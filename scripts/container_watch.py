#!/usr/bin/env python3
"""Every container's state for Prometheus, read straight from Docker (runs as the container-watch service).

cAdvisor (v0.49 and v0.52) reports no containers on Docker 29 with the containerd image store, so this asks the
Docker API instead and writes a node-exporter textfile (health-textfile volume) every INTERVAL seconds:

  ingabe_container_running{name}            1 running, 0 stopped (exited, restarting, paused, dead)
  ingabe_container_started_at_seconds{name} when it last started; changes on every restart and recreate
  ingabe_container_restart_count{name}      Docker's restart count (restart policy)
  ingabe_container_oom_killed{name}         1 if its last stop was an out-of-memory kill
  ingabe_container_memory_bytes{name}       memory in use (as `docker stats` shows it: without page cache)
  ingabe_container_memory_limit_bytes{name} its mem_limit; 0 when it has none
  ingabe_container_watch_last_run_seconds   heartbeat; the container alarms stay quiet when this is stale

Standard library only; talks to /var/run/docker.sock (mounted read-only) and only reads.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s container-watch: %(message)s")
log = logging.getLogger("container-watch")

SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
OUT = os.environ.get("TEXTFILE", "/textfile/containers.prom")
INTERVAL = int(os.environ.get("INTERVAL", "30"))
# Containers Claude Code starts and stops on demand (scripts/grafana_mcp.sh) are not part of Ingabe.
SKIP_PREFIXES = ("grafana-mcp",)


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 20):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def docker_get(path: str):
    conn = _UnixConnection(SOCKET)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"docker {path.split('?')[0]} -> {resp.status}")
        return json.loads(body)
    finally:
        conn.close()


def _epoch(ts: str) -> float:
    """Docker's StartedAt ('2026-10-25T12:00:00.123456789Z'; '0001-01-01T00:00:00Z' when never started)."""
    if not ts or ts.startswith("0001-"):
        return 0.0
    return datetime.fromisoformat(ts[:19] + "+00:00").timestamp()


def _memory_in_use(stats: dict) -> float:
    """Memory as `docker stats` reports it: usage minus inactive page cache (cgroup v2 and v1)."""
    mem = stats.get("memory_stats") or {}
    usage = mem.get("usage") or 0
    extra = mem.get("stats") or {}
    cache = extra.get("inactive_file", extra.get("total_inactive_file", 0))
    return float(max(usage - cache, 0))


def container_rows(cid: str) -> dict | None:
    info = docker_get(f"/containers/{cid}/json")
    name = info["Name"].lstrip("/")
    if name.startswith(SKIP_PREFIXES):
        return None
    state = info.get("State") or {}
    running = bool(state.get("Running")) and not state.get("Restarting") and not state.get("Paused")
    memory = 0.0
    if running:
        try:
            memory = _memory_in_use(docker_get(f"/containers/{cid}/stats?stream=false&one-shot=true"))
        except Exception as e:  # noqa: BLE001 - memory is optional; the running state still counts
            log.warning("no memory for %s: %s", name, e)
    return {
        "name": name,
        "running": 1 if running else 0,
        "started_at": _epoch(state.get("StartedAt", "")),
        "restart_count": info.get("RestartCount", 0),
        "oom_killed": 1 if state.get("OOMKilled") else 0,
        "memory": memory,
        "memory_limit": (info.get("HostConfig") or {}).get("Memory") or 0,
    }


METRICS = [
    ("running", "ingabe_container_running", "1 if the container is running"),
    ("started_at", "ingabe_container_started_at_seconds", "When the container last started (unix time)"),
    ("restart_count", "ingabe_container_restart_count", "Restarts by Docker's restart policy"),
    ("oom_killed", "ingabe_container_oom_killed", "1 if the container's last stop was an out-of-memory kill"),
    ("memory", "ingabe_container_memory_bytes", "Memory in use, without page cache"),
    ("memory_limit", "ingabe_container_memory_limit_bytes", "The container's memory limit; 0 = none"),
]


def render(rows: list[dict], now: float) -> str:
    lines = []
    for key, metric, help_text in METRICS:
        lines += [f"# HELP {metric} {help_text}", f"# TYPE {metric} gauge"]
        for row in sorted(rows, key=lambda r: r["name"]):
            lines.append(f'{metric}{{name="{row["name"]}"}} {row[key]}')
    lines += ["# HELP ingabe_container_watch_last_run_seconds When the container watch last read Docker",
              "# TYPE ingabe_container_watch_last_run_seconds gauge",
              f"ingabe_container_watch_last_run_seconds {now:.0f}"]
    return "\n".join(lines) + "\n"


def write_once() -> int:
    ids = [c["Id"] for c in docker_get("/containers/json?all=1")]
    with ThreadPoolExecutor(max_workers=8) as pool:  # stats take ~1 s each; read them side by side
        rows = [r for r in pool.map(container_rows, ids) if r]
    tmp = f"{OUT}.tmp"
    with open(tmp, "w") as f:
        f.write(render(rows, time.time()))
    os.replace(tmp, OUT)  # node-exporter never reads a half-written file
    return len(rows)


def main() -> None:
    log.info("writing %s every %d s", OUT, INTERVAL)
    while True:
        try:
            write_once()
        except Exception as e:  # noqa: BLE001 - a stale heartbeat raises ContainerWatchStale
            log.error("could not read Docker: %s", e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
