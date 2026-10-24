#!/usr/bin/env python3
"""One snapshot of Ingabe's health from Prometheus, as JSON, for the Ingabe Health page in Claude.

Run on the server (Prometheus answers on 127.0.0.1:9090 there):  python3 scripts/health_snapshot.py
Prints {"generated_at", "checks", "alerts", "history"}:
- checks: one row per thing watched (area, check, status ok|warn|bad|unknown, value, where), from the same numbers
  and limits as the alerts in monitoring/alerts.yml;
- alerts: every alert firing now, with its area, severity, summary and where to look (one row with
  severity "none" when nothing fires);
- history: per area and hour over the last 7 days, the worst alert that fired (for the timeline).
Standard library only; read-only.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROM = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090")
# The app's component self-tests with their details (internal only; the app listens on 127.0.0.1:8000 here).
APP = os.environ.get("APP_URL", "http://127.0.0.1:8000")
TIER_AREA = {"core": "components", "ai": "ai-model", "feed": "feeds"}

# (area, check, PromQL, unit, warn at, bad at, higher is worse, where to look)
CHECKS = [
    ("app", "Live app from outside", 'min(probe_success{job="public-app"})', "updown", None, None, False,
     "gis.nozalabs.rw via Cloudflare; container mundi-app"),
    ("app", "Answer time from outside", 'max(probe_duration_seconds{job="public-app"})', "s", 1.5, 3, True,
     "container mundi-app"),
    ("app", "Server errors, last hour", "sum(increase(http_request_errors_total[1h]))", "count", 1, 10, True,
     "container mundi-app logs"),
    ("sage", "Failed turns, last hour",
     '(sum(increase(sage_turns_total{outcome="failed"}[1h])) or vector(0))'
     ' + (sum(increase(sage_llm_errors_total{stage="final",kind!="context"}[1h])) or vector(0))', "count", 1, 3, True,
     "src/routes/message_routes.py; Grafana Sage"),
    ("sage", "Turns, last 24 hours", "sum(increase(sage_turns_total[24h])) or vector(0)", "count", None, None, True,
     "Grafana Sage"),
    ("sage", "Turns left hanging, last 24 hours", "sum(increase(sage_turns_closed_unfinished_total[24h])) or vector(0)",
     "count", 1, 3, True, "Grafana Sage, Logs"),
    ("sage", "Slowest 1 in 10 answers, last hour",
     "histogram_quantile(0.9, sum by (le) (rate(sage_turn_duration_seconds_bucket[1h])))", "s", 60, 120, True,
     "slow tools in Grafana Sage"),
    ("ai-model", "Model errors, last hour", "sum(increase(sage_llm_errors_total[1h])) or vector(0)", "count", 1, 5, True,
     "AI provider status; Grafana AI model"),
    ("ai-model", "Credit or key refused, last 24 hours",
     'sum(increase(sage_llm_errors_total{kind=~"credit|auth"}[24h])) or vector(0)', "count", None, 1, True,
     "AI provider billing; API key in .env"),
    ("ai-model", "Days of AI credit left", "(sage_llm_credit_expires_timestamp_seconds - time()) / 86400", "days",
     14, 7, False, "SAGE_LLM_CREDIT_EXPIRES in .env; provider billing"),
    ("dagster", "Scheduler heartbeat", 'max(dagster_daemon_heartbeat_age_seconds{daemon_type="SCHEDULER"})', "s",
     120, 300, True, "container mundi-dagster-daemon"),
    ("dagster", "Jobs whose latest run failed", "sum(dagster_job_last_run_failed) or vector(0)", "count", None, 1, True,
     "Grafana Dagster"),
    ("dagster", "Runs stuck over 3 hours", "max(dagster_runs_stuck) or vector(0)", "count", None, 1, True,
     "container mundi-dagster-daemon"),
    ("data", "Stalest data cache", "max(pipeline_cache_age_seconds / 86400)", "days", 7, 9, True,
     "Grafana Dagster (cache age)"),
    ("backup", "Last backup run", "max(backup_last_run_success)", "okfail", None, None, False,
     "container mundi-backup"),
    ("backup", "Hours since the last good backup", "(time() - max(backup_last_success_timestamp_seconds)) / 3600", "h",
     25, 26, True, "container mundi-backup"),
    ("database", "Postgres", 'min(pg_up{job="postgres"})', "updown", None, None, False, "container postgresdb"),
    ("server", "Disk used", '1 - max(node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})',
     "share", 0.75, 0.85, True, "docker builder prune; MinIO photos"),
    ("server", "Memory available", "min(node_memory_MemAvailable_bytes) / 1e9", "GB", 4, 2, False, "Grafana Server"),
    ("monitoring", "Monitoring sources not answering", "count(up == 0) or vector(0)", "count", None, 1, True,
     "Grafana Server (What Prometheus reads)"),
    ("maps", "Maps opened by people, last 24 hours",
     'sum(increase(frontend_map_events_total{event="map_load"}[24h])) or vector(0)', "count", None, None, True,
     "Grafana Components (maps)"),
    ("maps", "Map or 3D errors in browsers, last hour",
     'sum(increase(frontend_map_events_total{event=~"map_error|deck_error|webgl_lost"}[1h])) or vector(0)', "count",
     1, 5, True, "MapLibre / deck.gl; Grafana Logs"),
    ("maps", "Tile errors in browsers, last hour", 'sum(increase(frontend_map_events_total{event="tile_error"}[1h])) or vector(0)',
     "count", 1, 5, True, "tile routes and rasterd; Grafana Logs"),
]


def component_rows() -> list[dict]:
    """One row per component self-test, with what it found (src/dependencies/component_checks.py)."""
    try:
        with urllib.request.urlopen(f"{APP}/internal/component-checks", timeout=10) as resp:
            found = json.load(resp)
    except Exception as e:  # noqa: BLE001 - the page then shows the app itself as unreachable
        return [{"area": "components", "check": "Component self-tests", "status": "bad",
                 "value": f"could not read them: {str(e)[:80]}", "where": "container mundi-app"}]
    rows = []
    for name, r in sorted(found.items(), key=lambda kv: (kv[1].get("tier", ""), kv[1].get("component", ""))):
        age_min = (time.time() - r.get("at", 0)) / 60
        # A first failure is retried in 2 minutes (amber); two in a row is a real fault (red; amber for outside feeds).
        fails = r.get("fails", 0 if r.get("ok") else 2)
        status = "ok" if not fails else "warn" if fails == 1 or r.get("tier") == "feed" else "bad"
        if age_min > 90:
            status = "unknown"
        rows.append({"area": TIER_AREA.get(r.get("tier", ""), "components"), "check": r.get("component", name),
                     "status": status, "value": f'{r.get("detail", "")} ({r.get("seconds", 0):.1f} s)',
                     "where": f"check {name}; checked {age_min:.0f} min ago"})
    return rows


def _get(path: str, **params) -> list:
    url = f"{PROM}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        body = json.load(resp)
    if body.get("status") != "success":
        raise RuntimeError(body.get("error", "Prometheus query failed"))
    return body["data"]["result"]


def _status(value: float | None, unit: str, warn, bad, higher_worse: bool) -> str:
    if value is None or value != value:  # no data, or NaN
        return "unknown"
    if unit in ("updown", "okfail"):
        return "ok" if value >= 1 else "bad"
    worse = (lambda v, t: v >= t) if higher_worse else (lambda v, t: v <= t)
    if bad is not None and worse(value, bad):
        return "bad"
    if warn is not None and worse(value, warn):
        return "warn"
    return "ok"


def _show(value: float | None, unit: str) -> str:
    if value is None or value != value:
        return "no data"
    if unit == "updown":
        return "up" if value >= 1 else "DOWN"
    if unit == "okfail":
        return "worked" if value >= 1 else "FAILED"
    if unit == "share":
        return f"{value:.0%}"
    if unit == "count":
        return f"{value:.0f}"
    if unit == "s":
        return f"{value:.1f} s" if value < 120 else f"{value / 60:.1f} min"
    return f"{value:.1f} {unit}"


def snapshot() -> dict:
    now = time.time()
    checks = []
    for area, name, expr, unit, warn, bad, higher_worse, where in CHECKS:
        try:
            result = _get("/api/v1/query", query=expr)
            value = float(result[0]["value"][1]) if result else None
        except Exception as e:  # noqa: BLE001 - one broken query is one unknown row
            value, where = None, f"{where} (query failed: {str(e)[:80]})"
        checks.append({"area": area, "check": name, "status": _status(value, unit, warn, bad, higher_worse),
                       "value": _show(value, unit), "where": where})

    checks += component_rows()

    alerts = []
    with urllib.request.urlopen(f"{PROM}/api/v1/alerts", timeout=20) as resp:
        for a in json.load(resp)["data"]["alerts"]:
            if a.get("state") != "firing":
                continue
            lab, ann = a.get("labels", {}), a.get("annotations", {})
            alerts.append({"alert": lab.get("alertname", ""), "area": lab.get("area", "other"),
                           "severity": lab.get("severity", ""), "summary": ann.get("summary", ""),
                           "where": ann.get("where", ""), "since": (a.get("activeAt") or "")[:19] + "Z"})
    worst = {"bad": [], "warn": []}
    for a in alerts:
        worst["bad" if a["severity"] == "critical" else "warn"].append(a["area"])
    for c in checks:  # an area with a firing alert is at least as bad as that alert
        if c["area"] in worst["bad"]:
            c["status"] = "bad"
        elif c["area"] in worst["warn"] and c["status"] == "ok":
            c["status"] = "warn"

    history = []
    start = int(now - 7 * 86400) // 3600 * 3600
    for series in _get("/api/v1/query_range", query='max by (area, severity) (ALERTS{alertstate="firing"})',
                       start=start, end=int(now), step=3600):
        lab = series["metric"]
        for ts, _ in series["values"]:
            history.append({"hour": datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:00Z"),
                            "area": lab.get("area", "other"),
                            "status": "bad" if lab.get("severity") == "critical" else "warn"})
    at = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for c in checks:
        c["checked_at"] = at
    if not alerts:  # the Health page reads an empty file as a failed load; one row says nothing is firing
        alerts = [{"alert": "none", "area": "", "severity": "none", "summary": "No alarm is firing", "where": "",
                   "since": at}]
    return {"generated_at": at, "checks": checks, "alerts": alerts, "history": history}


if __name__ == "__main__":
    json.dump(snapshot(), sys.stdout, indent=1)
    sys.stdout.write("\n")
