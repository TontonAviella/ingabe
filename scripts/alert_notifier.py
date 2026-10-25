#!/usr/bin/env python3
"""Ingabe alerts to Roger's phone through ntfy (https://ntfy.sh), straight from the server, day and night.

Alertmanager posts every alarm (monitoring/alerts.yml) here as it fires and when it is fixed; each becomes one
push in plain words: what broke, what it means for people, where to look, and that Claude Code can fix it. Tapping
it opens the Ingabe Health page. Every morning at DAILY_HOUR_UTC (06:00 UTC = 08:00 Kigali) a summary is sent
even when all is fine, so a silent alert system shows up as a missing morning message.

Standard library only. Configured by environment: NTFY_TOPIC (the private channel; nothing is sent without it),
NTFY_URL (default https://ntfy.sh), PROMETHEUS_URL, HEALTH_PAGE_URL, GRAFANA_URL, DAILY_HOUR_UTC.
Listens on :8080 inside the Docker network only (no published port): POST /alertmanager, POST /test, GET /healthz.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s alert-notifier: %(message)s")
log = logging.getLogger("alert-notifier")

NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
HEALTH_PAGE_URL = os.environ.get("HEALTH_PAGE_URL", "https://claude.ai/artifact/TYQoLin7n8s9gPN7rznxdH")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "https://grafana.nozalabs.rw").rstrip("/")
DAILY_HOUR_UTC = int(os.environ.get("DAILY_HOUR_UTC", "6"))
KIGALI = timezone(timedelta(hours=2))

# The order problems are traced in, bottom up: the morning check reports every step, ✓ or what is wrong.
STEPS = [
    ("1 Server", ["server"]),
    ("2 Containers", ["containers"]),
    ("3 Data stores", ["database"]),
    ("4 Public access", ["app"]),
    ("5 App features", ["sage", "ai-model", "maps", "components"]),
    ("6 Data jobs", ["dagster", "data"]),
    ("7 Outside data", ["feeds"]),
    ("8 Backups", ["backup"]),
    ("9 Monitoring", ["monitoring"]),
]

# What an alarm in each area means for the people using Ingabe (alerts.yml labels every rule with an area).
AREA = {
    "server": ("Server", "The Contabo server is short of disk, memory or CPU, or will be soon."),
    "containers": ("Containers", "A part of the system has stopped, keeps restarting or is running out of memory."),
    "app": ("Public access", "People may not be able to open gis.nozalabs.rw."),
    "feeds": ("Outside data", "An outside data source (satellite, rain, weather) is not answering; answers using it may be missing."),
    "sage": ("Sage", "Sage may fail or be slow to answer people."),
    "ai-model": ("AI model", "Sage cannot answer without its AI model."),
    "components": ("Components", "A part of the app is not doing its job; features that use it may fail."),
    "maps": ("Maps", "Maps may not load or show tiles for people."),
    "dagster": ("Data jobs", "Scheduled data updates (NDVI, drought, weather, land cover) are not running."),
    "data": ("Data freshness", "Sage is answering from data that is getting old."),
    "backup": ("Backups", "Last night's copy of the database is missing or bad."),
    "database": ("Data stores", "The database, cache or file storage is failing or nearly full."),
    "monitoring": ("Monitoring", "Part of the monitoring is blind, so problems there would go unseen."),
}


def _kigali(ts: str) -> str:
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return when.astimezone(KIGALI).strftime("%d %b %H:%M") + " Kigali"


def format_group(payload: dict) -> list[dict]:
    """One ntfy message per alert in an Alertmanager webhook payload."""
    messages = []
    for alert in payload.get("alerts", []):
        labels, notes = alert.get("labels", {}), alert.get("annotations", {})
        area, sev, name = labels.get("area", "monitoring"), labels.get("severity", "warning"), labels.get("alertname", "")
        area_name, meaning = AREA.get(area, (area.title(), ""))
        if area == "components" and labels.get("component"):
            meaning = f"{labels['component']} is not doing its job; features that use it may fail."
        summary = notes.get("summary", name)
        if alert.get("status") == "resolved":
            messages.append({
                "title": f"Fixed: {area_name}", "priority": 2, "tags": ["white_check_mark"],
                "message": f"{summary}\nBack to normal since {_kigali(alert.get('endsAt', ''))}.",
            })
            continue
        lines = [summary]
        if meaning:
            lines.append(f"What it means: {meaning}")
        if notes.get("where"):
            lines.append(f"Where to look: {notes['where']}")
        since = _kigali(alert.get("startsAt", ""))
        if since:
            lines.append(f"Since: {since}")
        lines.append(f'To fix: open Claude Code and say "fix the {name} alarm".')
        critical = sev == "critical"
        messages.append({
            "title": f"{'RED' if critical else 'Needs a look'}: {area_name}",
            "priority": 4 if critical else 3,
            "tags": ["rotating_light"] if critical else ["warning"],
            "message": "\n".join(lines),
        })
    return messages


def send(msg: dict) -> bool:
    if not NTFY_TOPIC:
        log.warning("NTFY_TOPIC is not set; not sent: %s", msg.get("title"))
        return False
    body = {"topic": NTFY_TOPIC, "click": HEALTH_PAGE_URL,
            "actions": [{"action": "view", "label": "Health page", "url": HEALTH_PAGE_URL},
                        {"action": "view", "label": "Grafana", "url": GRAFANA_URL}], **msg}
    req = urllib.request.Request(NTFY_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    log.info("sent: %s", msg.get("title"))
                    return True
        except Exception as e:  # noqa: BLE001 - retried; a lost push is logged loudly
            log.warning("ntfy send failed (try %d): %s", attempt + 1, e)
            time.sleep(5 * (attempt + 1))
    log.error("ntfy message lost after 3 tries: %s", msg.get("title"))
    return False


def _prom(query: str) -> list:
    url = f"{PROMETHEUS_URL}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.load(resp)["data"]["result"]


def daily_summary() -> dict:
    """The morning message: all clear, or what needs attention, from Prometheus."""
    try:
        with urllib.request.urlopen(f"{PROMETHEUS_URL}/api/v1/alerts", timeout=15) as resp:
            firing = [a for a in json.load(resp)["data"]["alerts"] if a.get("state") == "firing"]
        rules = sum(len(g["rules"]) for g in json.load(urllib.request.urlopen(f"{PROMETHEUS_URL}/api/v1/rules",
                                                                              timeout=15))["data"]["groups"])
        checks = _prom("component_check_ok")
        ok_checks = sum(1 for c in checks if c["value"][1] == "1")
        answer = _prom('max(probe_duration_seconds{job="public-app"})')
        speed = f"{float(answer[0]['value'][1]):.1f} s" if answer else "unknown"
    except Exception as e:  # noqa: BLE001 - the morning message then says monitoring itself is broken
        return {"title": "Morning check: monitoring problem", "priority": 4, "tags": ["rotating_light"],
                "message": f"Could not read the monitoring on the server ({str(e)[:120]}). "
                           'Open Claude Code and say "the morning check could not read Prometheus".'}
    lines = []
    for step, areas in STEPS:
        wrong = sorted({a["annotations"].get("summary", a["labels"].get("alertname", "")) for a in firing
                        if a["labels"].get("area") in areas})
        known = {x for _, xs in STEPS for x in xs}
        if step.startswith("9"):  # anything with an area outside the steps is reported under monitoring
            wrong += sorted({a["annotations"].get("summary", "") for a in firing if a["labels"].get("area") not in known})
        lines.append(f"{step} ✓" if not wrong else f"{step} ✗ " + "; ".join(wrong[:3]))
    footer = (f"{rules} alarms watching; {ok_checks}/{len(checks)} parts did their job; site answers in {speed}.")
    if not firing:
        return {"title": "Morning check: all clear", "priority": 2, "tags": ["sunny"],
                "message": "\n".join(lines) + "\n" + footer}
    return {"title": f"Morning check: {len(firing)} problem{'s' if len(firing) != 1 else ''}", "priority": 4,
            "tags": ["warning"],
            "message": "\n".join(lines) + "\n" + footer + "\nSee the Health page; Claude Code can fix them."}


def _daily_loop() -> None:
    last_sent = None
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == DAILY_HOUR_UTC and last_sent != now.date():
            send(daily_summary())
            last_sent = now.date()
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: str = "") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):  # noqa: N802 - http.server API
        self._reply(200, "ok") if self.path == "/healthz" else self._reply(404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 1_000_000))
        if self.path == "/alertmanager":
            try:
                payload = json.loads(raw)
            except ValueError:
                return self._reply(400, "bad json")
            sent = [send(m) for m in format_group(payload)]
            return self._reply(200 if all(sent) else 502, f"{sum(sent)}/{len(sent)} sent")
        if self.path == "/test":
            ok = send({"title": "Ingabe alerts are connected", "priority": 3, "tags": ["tada"],
                       "message": "This is a test. Real alarms will arrive here when something breaks and when it is "
                                  "fixed, plus a check-in every morning at 08:00 Kigali time."})
            return self._reply(200 if ok else 502, "sent" if ok else "not sent")
        if self.path == "/daily":
            ok = send(daily_summary())
            return self._reply(200 if ok else 502, "sent" if ok else "not sent")
        return self._reply(404)

    def log_message(self, format, *args):  # noqa: A002 - http.server API; quiet access log
        return


def main() -> None:
    if not NTFY_TOPIC:
        log.error("NTFY_TOPIC is not set: alarms will be logged here but not pushed")
    threading.Thread(target=_daily_loop, daemon=True).start()
    log.info("listening on :8080; daily check at %02d:00 UTC", DAILY_HOUR_UTC)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
