#!/usr/bin/env python3
"""One snapshot of Ingabe's health from Prometheus, as JSON, for the Ingabe Health page in Claude.

Run on the server (Prometheus answers on 127.0.0.1:9090 there):  python3 scripts/health_snapshot.py
Prints {"generated_at", "checks", "alerts", "history", "alarm_rules", "logs", "sage_tools", "sage_rules",
"sage_turns"}:
- checks: one row per thing watched (area, check, status ok|warn|bad|unknown, value, where, and to dig deeper: limit,
  the limits in words, and measured, the exact query or self-test), from the same numbers and limits as the alerts in
  monitoring/alerts.yml;
- alerts: every alert firing now, with its area, severity, summary and where to look (one row with
  severity "none" when nothing fires);
- history: per area and hour over the last 7 days, the worst alert that fired (for the timeline);
- alarm_rules: every alarm rule (the system's rules), its state now, what it watches and its exact condition;
- logs: per container, error lines in the last 24 hours (count and the latest three), from Loki through Grafana
  (GRAFANA_MCP_TOKEN, the read-only Viewer token in ~/ingabe/.env);
- sage_tools, sage_rules, sage_turns: what Sage did over 7 days, per tool, per rule and turn by turn (the app's
  /internal/sage-turns, from the Sage turn log; src/services/sage_turn_log.py);
- sage_ratings: people's 👍/👎 on Sage's answers over 7 days, each with the turn that wrote it;
- routes: per page or API route over 24 hours, calls, server errors and time (Tempo's span metrics in Prometheus);
- slow_queries: the database queries that took the most time since Postgres started (pg_stat_statements, read as
  the admin over the container's local socket; query texts have their values replaced by $1, $2...);
- browser_errors: errors in people's browsers over 7 days, grouped by message (Loki, logger src.browser_errors).
Standard library only; read-only.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROM = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090")
# The app's component self-tests with their details (internal only; the app listens on 127.0.0.1:8000 here).
APP = os.environ.get("APP_URL", "http://127.0.0.1:8000")
GRAFANA = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"
# Lines that count as errors in any container's log (the same words as the Grafana Logs dashboard).
ERROR_LINES = '(?i)\\b(error|exception|traceback|fatal|panic|critical)\\b'
# ...unless the line's own level says otherwise (Grafana logs each query, and queries say "error").
NOT_ERRORS = '(?i)(level=(info|debug|warn)|"level": ?"(INFO|DEBUG|WARNING)")'
# Ingabe's containers (not the helpers Claude Code starts, which get random names).
OWN = '(mundi|ingabe|mundiai)-.+'
TIER_AREA = {"core": "components", "ai": "ai-model", "feed": "feeds"}
# Component checks that belong to an earlier step than "app features": the data stores and sign-in.
CHECK_AREA = {"postgis": "database", "redis": "database", "minio": "database", "workos": "app"}
# The order problems are traced in, bottom up (the same steps as the morning message on Roger's phone).
STEP_OF = {"server": "1 Server", "containers": "2 Containers", "database": "3 Data stores", "app": "4 Public access",
           "sage": "5 App features", "ai-model": "5 App features", "maps": "5 App features",
           "components": "5 App features", "requests": "5 App features", "browsers": "5 App features",
           "dagster": "6 Data jobs", "data": "6 Data jobs", "feeds": "7 Outside data",
           "backup": "8 Backups", "monitoring": "9 Monitoring"}


def step_of(area: str) -> str:
    return STEP_OF.get(area, "9 Monitoring")

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
    ("server", "Disk full in (forecast from the last day)",
     '(max(node_filesystem_avail_bytes{mountpoint="/"}) / clamp_min(-deriv(max(node_filesystem_avail_bytes{mountpoint="/"})[24h:10m]), 1)) / 86400',
     "days", 30, 7, False, "docker builder prune; MinIO photos; Loki logs"),
    ("server", "CPU taken by the hosting company (steal)", 'avg(rate(node_cpu_seconds_total{mode="steal"}[10m]))', "share",
     0.05, 0.1, True, "Contabo (oversold host)"),
    ("server", "Server restarts, last 24 hours", "changes(max(node_boot_time_seconds)[24h:5m]) or vector(0)", "count", 1, None,
     True, "Contabo control panel"),
    ("server", "Clock synchronised", "min(node_timex_sync_status)", "updown", None, None, False, "timedatectl on the server"),
    ("containers", "Containers running", 'count(ingabe_container_running == 1)', "count", None, None, True, "docker ps"),
    ("containers", "Essential containers not running",
     'count(ALERTS{alertname=~"ContainerMissing.*",alertstate="firing"}) or vector(0)', "count", None, 1, True,
     "docker ps; the alarm names which one"),
    ("containers", "Containers restarting (more than twice an hour)",
     'count(changes(ingabe_container_started_at_seconds[1h]) > 2) or vector(0)', "count", None, 1, True,
     "docker logs of that container"),
    ("containers", "Containers near their memory limit",
     'count(ingabe_container_memory_bytes / (ingabe_container_memory_limit_bytes > 0) > 0.9) or vector(0)',
     "count", 1, None, True, "Grafana Components (memory)"),
    ("database", "Postgres connections used", 'sum(pg_stat_activity_count{job="postgres"}) / max(pg_settings_max_connections{job="postgres"})',
     "share", 0.6, 0.8, True, "the app pool, Dagster, Grafana"),
    ("database", "Redis", "min(redis_up)", "updown", None, None, False, "container redis"),
    ("database", "Redis memory used", "max(redis_memory_used_bytes / (redis_memory_max_bytes > 0))", "share", None, None, True,
     "Redis maxmemory (2 GB)"),
    ("database", "File storage (MinIO)", "min(minio_cluster_health_status)", "updown", None, None, False, "container minio"),
    ("app", "HTTPS certificate days left", '(min(probe_ssl_earliest_cert_expiry{job="public-app"}) - time()) / 86400', "days",
     30, 14, False, "Cloudflare SSL/TLS"),
    ("app", "Domain nozalabs.rw days left", "vector(1820620800 - time()) / 86400", "days", 90, 60, False,
     "the .rw registrar (AOS); update monitoring/alerts.yml after renewing"),
    ("monitoring", "Logs arriving in Grafana (lines a minute)", "sum(rate(loki_distributor_lines_received_total[15m])) * 60",
     "count", None, 0, False, "containers mundi-alloy, mundi-loki"),
    ("monitoring", "Alarms that failed to reach your phone, last 24 hours",
     "sum(increase(alertmanager_notifications_failed_total[24h])) or vector(0)", "count", None, 1, True,
     "containers mundi-alertmanager, mundi-alert-notifier"),
    ("maps", "Maps opened by people, last 24 hours",
     'sum(increase(frontend_map_events_total{event="map_load"}[24h])) or vector(0)', "count", None, None, True,
     "Grafana Components (maps)"),
    ("maps", "Map or 3D errors in browsers, last hour",
     'sum(increase(frontend_map_events_total{event=~"map_error|deck_error|webgl_lost"}[1h])) or vector(0)', "count",
     1, 5, True, "MapLibre / deck.gl; Grafana Logs"),
    ("maps", "Tile errors in browsers, last hour", 'sum(increase(frontend_map_events_total{event="tile_error"}[1h])) or vector(0)',
     "count", 1, 5, True, "tile routes and rasterd; Grafana Logs"),
    ("requests", "Routes with server errors, last hour",
     'count(sum by (span_name) (increase(traces_spanmetrics_calls_total{span_kind="SPAN_KIND_SERVER",'
     'status_code="STATUS_CODE_ERROR"}[1h])) >= 1) or vector(0)', "count", 1, 3, True,
     "Requests by route below; Grafana Requests"),
    ("requests", "Slowest route, last hour (1 in 20 requests)",
     'max(histogram_quantile(0.95, sum by (span_name, le) (rate(traces_spanmetrics_latency_bucket{'
     'span_kind="SPAN_KIND_SERVER",span_name!~".*upload.*"}[1h]))))', "s", 5, 10, True,
     "Requests by route below; a slow trace in Grafana Requests"),
    ("browsers", "Errors in people's browsers, last hour", "sum(increase(frontend_errors_total[1h])) or vector(0)",
     "count", 1, 10, True, "Errors in people's browsers below"),
    ("browsers", "Screens that crashed, last 24 hours",
     'sum(increase(frontend_errors_total{kind=~"react_crash|app_init_failed"}[24h])) or vector(0)', "count", 1, None,
     True, "Errors in people's browsers below"),
    ("database", "Slowest query on average, last hour",
     'max(sum by (queryid) (increase(pg_stat_statements_seconds_total{datname="mundidb"}[1h]))'
     ' / sum by (queryid) (increase(pg_stat_statements_calls_total{datname="mundidb"}[1h])))', "s", 2, 5, True,
     "Slowest database queries below"),
    ("sage", "Answers marked wrong (👎), last 7 days",
     'sum(increase(sage_answer_ratings_total{rating="down"}[7d])) or vector(0)', "count", 1, 3, True,
     "Sage: rated answers below"),
    ("monitoring", "Request traces arriving", "(sum(rate(tempo_distributor_spans_received_total[30m])) > bool 0)",
     "updown", None, None, False, "container mundi-tempo"),
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
        area = CHECK_AREA.get(name) or TIER_AREA.get(r.get("tier", ""), "components")
        rows.append({"area": area, "check": r.get("component", name),
                     "status": status, "value": f'{r.get("detail", "")} ({r.get("seconds", 0):.1f} s)',
                     "where": f"check {name}; checked {age_min:.0f} min ago",
                     "limit": "Asked to do its job with a known answer every 30 minutes; amber after one failure "
                              "(retried in 2 minutes), red after two in a row (outside feeds stay amber).",
                     "measured": f"component self-test '{name}' (src/dependencies/component_checks.py)"})
    return rows


def _limit_words(unit: str, warn, bad, higher_worse: bool) -> str:
    if unit == "updown":
        return "Red when down."
    if unit == "okfail":
        return "Red when it failed."
    if warn is None and bad is None:
        return "Shown for context; no limit."
    side = "or more" if higher_worse else "or less"
    parts = [f"amber at {_show(warn, unit)} {side}" if warn is not None else "",
             f"red at {_show(bad, unit)} {side}" if bad is not None else ""]
    text = "; ".join(p for p in parts if p)
    return text[:1].upper() + text[1:] + "."


def alarm_rules() -> list[dict]:
    """Every alarm rule Prometheus runs (monitoring/alerts.yml), with its state now."""
    try:
        with urllib.request.urlopen(f"{PROM}/api/v1/rules", timeout=20) as resp:
            groups = json.load(resp)["data"]["groups"]
    except Exception as e:  # noqa: BLE001 - one row saying so; the rest of the snapshot still goes out
        return [{"step": "9 Monitoring", "area": "monitoring", "alarm": "(rules)", "severity": "", "state": "quiet",
                 "since": "", "watches": f"could not read the alarm rules: {str(e)[:120]}", "where": "Prometheus",
                 "condition": "", "for_minutes": 0, "healthy": False, "rule_error": str(e)[:200], "group": ""}]
    rows = []
    for g in groups:
        for r in g["rules"]:
            if r.get("type") != "alerting":
                continue
            lab, ann = r.get("labels", {}), r.get("annotations", {})
            active = [a.get("activeAt", "") for a in r.get("alerts", [])]
            rows.append({
                "step": step_of(lab.get("area", "monitoring")), "area": lab.get("area", ""), "alarm": r["name"],
                "severity": lab.get("severity", ""),
                "state": {"firing": "firing", "pending": "starting"}.get(r.get("state", ""), "quiet"),
                "since": (min(active)[:19] + "Z") if active else "",
                "watches": ann.get("summary", ""), "where": ann.get("where", ""),
                "condition": " ".join(r.get("query", "").split()),
                "for_minutes": round(r.get("duration", 0) / 60),
                "healthy": r.get("health") == "ok", "rule_error": r.get("lastError", ""),
                "group": g["name"],
            })
    order = {"firing": 0, "starting": 1, "quiet": 2}
    rows.sort(key=lambda x: (x["step"], order[x["state"]], x["alarm"]))
    return rows


def _env(name: str) -> str:
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get(name, "")


def _loki(path: str, **params) -> list:
    token = _env("GRAFANA_MCP_TOKEN")
    if not token:
        raise RuntimeError("GRAFANA_MCP_TOKEN is not in .env")
    url = f"{GRAFANA}/api/datasources/proxy/uid/loki/loki/api/v1/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["data"]["result"]


def _log_text(line: str) -> str:
    """A log line as words: the app logs JSON (level, logger, message); other containers plain text."""
    try:
        obj = json.loads(line)
        if isinstance(obj, dict):
            text = " ".join(str(obj.get(k, "")) for k in ("level", "logger", "message") if obj.get(k))
            exc = obj.get("exception") or obj.get("exc_info")
            if isinstance(exc, dict):  # src/logging_json.py: {"type", "message", "traceback": [...]}
                text += f" | {exc.get('type', '')}: {exc.get('message', '')}"
            elif exc:
                last = str(exc).strip().splitlines()[-1:]
                text += (" | " + last[0]) if last else ""
            line = text or line
    except ValueError:
        pass
    line = " ".join(line.split())
    return line if len(line) <= 300 else line[:299] + "…"


def log_rows(now: float) -> list[dict]:
    """Per container, error lines in the last 24 hours: how many and the latest three."""
    try:
        counts = _loki("query", query=f'sum by (container) (count_over_time({{container=~"{OWN}"}} |~ `{ERROR_LINES}` !~ `{NOT_ERRORS}` [24h]))',
                       time=int(now))
    except Exception as e:  # noqa: BLE001 - the page then says the logs could not be read
        return [{"container": "(logs)", "step": "9 Monitoring", "errors_24h": None,
                 "latest": f"could not read the logs: {str(e)[:120]}", "latest_at": ""}]
    rows = []
    for series in counts:
        name = series["metric"].get("container", "")
        if not name or name.startswith("grafana-mcp"):
            continue
        n = int(float(series["value"][1]))
        lines = []
        try:
            streams = _loki("query_range", query=f'{{container="{name}"}} |~ `{ERROR_LINES}` !~ `{NOT_ERRORS}`', limit=3,
                            direction="backward", start=int((now - 86400) * 1e9), end=int(now * 1e9))
            entries = sorted((v for st in streams for v in st["values"]), key=lambda v: v[0], reverse=True)[:3]
            lines = [(datetime.fromtimestamp(int(ts) / 1e9, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
                      _log_text(text)) for ts, text in entries]
        except Exception as e:  # noqa: BLE001 - keep the count; say the lines could not be read
            lines = [("", f"could not read the lines: {str(e)[:120]}")]
        rows.append({"container": name, "step": step_of(CONTAINER_AREA.get(_service(name), "monitoring")),
                     "errors_24h": n, "latest_at": lines[0][0] if lines else "",
                     "latest": "\n".join(f"{at[11:16]} {text}" if at else text for at, text in lines)})
    rows.sort(key=lambda r: -(r["errors_24h"] or 0))
    return rows


# Which step a container's errors belong to (compose service name -> area).
CONTAINER_AREA = {"postgresdb": "database", "redis": "database", "minio": "database", "app": "components",
                  "rasterd": "maps", "ollama": "components", "dagster-daemon": "dagster", "backup": "backup"}


def _service(container: str) -> str:
    """The compose service of a container: mundi-app -> app, ingabe-redis-1 -> redis."""
    name = container.removeprefix("mundi-").removeprefix("ingabe-").removeprefix("mundiai-")
    return name.rsplit("-", 1)[0] if name[-1:].isdigit() and "-" in name else name


def sage_tables() -> dict[str, list[dict]]:
    """What Sage did over 7 days, from the app (internal only)."""
    try:
        with urllib.request.urlopen(f"{APP}/internal/sage-turns?days=7", timeout=30) as resp:
            found = json.load(resp)
    except Exception as e:  # noqa: BLE001 - one row saying so; the page shows it
        note = f"could not read Sage's turn log: {str(e)[:120]}"
        return {"sage_tools": [{"tool": "(none)", "calls": 0, "last_error": note}],
                "sage_rules": [{"rule": "(none)", "name": note, "turns": 0}],
                "sage_turns": [{"started_at": "", "outcome": "unknown", "steps": note}]}
    empty = "no Sage turns recorded in the last 7 days"
    ratings = (found.get("ratings") or {}).get("rated") or [
        {"rated_at": "", "rating": "none", "answer": "No answer was rated in the last 7 days"}]
    return {"sage_tools": found.get("tools") or [{"tool": "(none)", "calls": 0, "last_error": empty}],
            "sage_rules": found.get("rules") or [{"rule": "(none)", "name": "No rule stepped in", "turns": 0}],
            "sage_turns": found.get("turns") or [{"started_at": "", "outcome": "none", "steps": empty}],
            "sage_ratings": ratings}


def route_rows() -> list[dict]:
    """Per route over 24 hours: calls, server errors and time, from the traces (Tempo span metrics)."""
    base = 'span_kind="SPAN_KIND_SERVER"'
    try:
        calls = _get("/api/v1/query", query=f"sum by (span_name) (increase(traces_spanmetrics_calls_total{{{base}}}[24h]))")
        errors = _get("/api/v1/query", query=f'sum by (span_name) (increase(traces_spanmetrics_calls_total{{{base},'
                                             f'status_code="STATUS_CODE_ERROR"}}[24h]))')
        quant = {q: _get("/api/v1/query", query=f"histogram_quantile({q}, sum by (span_name, le) "
                                                 f"(rate(traces_spanmetrics_latency_bucket{{{base}}}[24h])))")
                 for q in (0.5, 0.95)}
    except Exception as e:  # noqa: BLE001 - one row saying so
        return [{"route": "(routes)", "calls": None, "note": f"could not read the routes: {str(e)[:120]}"}]
    by = lambda res: {r["metric"].get("span_name", ""): float(r["value"][1]) for r in res}  # noqa: E731
    c, er, p50, p95 = by(calls), by(errors), by(quant[0.5]), by(quant[0.95])
    rows = []
    for route, n in c.items():
        if n < 0.5:
            continue
        e = er.get(route, 0.0)
        rows.append({"route": route, "calls": round(n), "server_errors": round(e),
                     "error_share": round(e / n, 3) if n else 0,
                     "median_s": _num(p50.get(route)), "p95_s": _num(p95.get(route))})
    rows.sort(key=lambda r: (-r["server_errors"], -(r["p95_s"] or 0)))
    return rows or [{"route": "(routes)", "calls": None, "note": "no traced requests in the last 24 hours"}]


def _num(v: float | None) -> float | None:
    return None if v is None or v != v or v in (float("inf"), float("-inf")) else round(v, 2)


SLOW_QUERIES_SQL = """
SELECT coalesce(json_agg(q), '[]'::json) FROM (
  SELECT left(regexp_replace(query, '\\s+', ' ', 'g'), 400) AS query, calls,
         round((total_exec_time / 1000)::numeric, 1) AS total_s,
         round(mean_exec_time::numeric, 1) AS mean_ms, round(max_exec_time::numeric, 1) AS max_ms, rows,
         round((100 * total_exec_time / nullif(sum(total_exec_time) OVER (), 0))::numeric, 1) AS share_pct
  FROM pg_stat_statements s JOIN pg_database d ON d.oid = s.dbid
  WHERE d.datname = 'mundidb' AND query NOT ILIKE '%pg_stat_statements%'
  ORDER BY total_exec_time DESC LIMIT 20) q
"""


def slow_query_rows() -> list[dict]:
    """The 20 queries that took the most database time since Postgres started (pg_stat_statements)."""
    try:
        out = subprocess.run(
            ["docker", "exec", "-u", "postgres", "ingabe-postgresdb-1", "psql", "-U", "mundi_admin", "-d", "mundidb",
             "-At", "-c", SLOW_QUERIES_SQL], capture_output=True, text=True, timeout=30, check=True).stdout
        rows = json.loads(out.strip() or "[]")
    except Exception as e:  # noqa: BLE001 - one row saying so (e.g. the extension is not created yet)
        detail = getattr(e, "stderr", "") or str(e)
        return [{"query": f"could not read the slow queries: {str(detail).strip()[:160]}", "calls": None}]
    return rows or [{"query": "no queries recorded yet", "calls": None}]


def browser_error_rows(now: float) -> list[dict]:
    """Errors in people's browsers over 7 days, grouped by kind and message (the app logs each one)."""
    try:
        streams = _loki("query_range", query='{container="mundi-app"} |= "src.browser_errors"', limit=1000,
                        direction="backward", start=int((now - 7 * 86400) * 1e9), end=int(now * 1e9))
    except Exception as e:  # noqa: BLE001 - one row saying so
        return [{"message": f"could not read the browser errors: {str(e)[:120]}", "kind": "", "count": None}]
    groups: dict[tuple, dict] = {}
    for st in streams:
        for ts, line in st["values"]:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("logger") != "src.browser_errors":
                continue
            key = (obj.get("kind", ""), str(obj.get("message", "")).removeprefix(f"browser {obj.get('kind', '')}: "))
            g = groups.setdefault(key, {"kind": key[0], "message": key[1][:300], "count": 0, "pages": set(),
                                        "people": set(), "latest_at": "", "source": "", "stack": "", "build": ""})
            g["count"] += 1
            g["pages"].add(str(obj.get("page", ""))[:80])
            g["people"].add(str(obj.get("user_id", "")))
            at = datetime.fromtimestamp(int(ts) / 1e9, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
            if at > g["latest_at"]:
                g.update(latest_at=at, source=str(obj.get("source", ""))[:200], stack=str(obj.get("stack", ""))[:800],
                         build=str(obj.get("build", "")))
    rows = []
    for g in groups.values():
        g["pages"] = ", ".join(sorted(g["pages"]))[:300]
        g["people"] = len(g["people"])
        rows.append(g)
    rows.sort(key=lambda r: (r["kind"] not in ("react_crash", "app_init_failed"), -r["count"]))
    return rows or [{"message": "No errors in people's browsers in the last 7 days", "kind": "", "count": 0}]


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
    if unit == "days" and value > 365:
        return "over a year"
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
                       "value": _show(value, unit), "where": where,
                       "limit": _limit_words(unit, warn, bad, higher_worse), "measured": expr})

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
        c["step"] = step_of(c["area"])
    checks.sort(key=lambda c: c["step"])
    for a in alerts:
        a["step"] = step_of(a["area"])
    for h in history:
        h["step"] = step_of(h["area"])
    if not alerts:  # the Health page reads an empty file as a failed load; one row says nothing is firing
        alerts = [{"alert": "none", "area": "", "severity": "none", "summary": "No alarm is firing", "where": "",
                   "since": at}]
    return {"generated_at": at, "checks": checks, "alerts": alerts, "history": history,
            "alarm_rules": alarm_rules(), "logs": log_rows(now), **sage_tables(), "routes": route_rows(),
            "slow_queries": slow_query_rows(), "browser_errors": browser_error_rows(now)}


if __name__ == "__main__":
    json.dump(snapshot(), sys.stdout, indent=1)
    sys.stdout.write("\n")
