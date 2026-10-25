"""Phone alarms (scripts/alert_notifier.py): each alarm becomes a plain message saying what broke, what it means
for people, where to look and how to get it fixed; fixed alarms say so; nothing is sent without a channel."""

import importlib.util
import json
import pathlib

_spec = importlib.util.spec_from_file_location(
    "alert_notifier", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "alert_notifier.py")
notifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notifier)  # type: ignore[union-attr]


def _payload(status="firing", **labels):
    return {"status": status, "alerts": [{
        "status": status,
        "labels": {"alertname": "BackupFailed", "area": "backup", "severity": "critical", **labels},
        "annotations": {"summary": "The last nightly database backup failed",
                        "where": "container mundi-backup (/var/log/backup.log)"},
        "startsAt": "2026-10-25T00:05:00Z", "endsAt": "2026-10-25T09:30:00Z",
    }]}


def test_a_critical_alarm_reads_plainly():
    [msg] = notifier.format_group(_payload())
    assert msg["title"] == "RED: Backups" and msg["priority"] == 4
    body = msg["message"]
    assert body.startswith("The last nightly database backup failed")
    assert "What it means: Last night's copy of the database is missing or bad." in body
    assert "Where to look: container mundi-backup" in body
    assert "Since: 25 Oct 02:05 Kigali" in body
    assert 'say "fix the BackupFailed alarm"' in body


def test_a_fixed_alarm_says_so():
    [msg] = notifier.format_group(_payload(status="resolved"))
    assert msg["title"] == "Fixed: Backups" and msg["priority"] == 2
    assert "Back to normal since 25 Oct 11:30 Kigali" in msg["message"]


def test_a_component_alarm_names_the_component():
    [msg] = notifier.format_group(_payload(alertname="ComponentCheckFailing", area="components", component="PostGIS"))
    assert "What it means: PostGIS is not doing its job" in msg["message"]


def test_a_warning_is_not_red():
    [msg] = notifier.format_group(_payload(severity="warning"))
    assert msg["title"] == "Needs a look: Backups" and msg["priority"] == 3


def test_nothing_is_sent_without_a_channel(monkeypatch):
    monkeypatch.setattr(notifier, "NTFY_TOPIC", "")
    assert notifier.send({"title": "x", "message": "y"}) is False


def test_messages_go_to_the_private_channel_with_the_health_page(monkeypatch):
    sent = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        sent.append(json.loads(req.data))
        return Resp()

    monkeypatch.setattr(notifier, "NTFY_TOPIC", "ingabe-test-channel")
    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen)
    assert notifier.send({"title": "RED: Backups", "message": "m", "priority": 4})
    assert sent[0]["topic"] == "ingabe-test-channel" and sent[0]["click"] == notifier.HEALTH_PAGE_URL
    assert [a["label"] for a in sent[0]["actions"]] == ["Health page", "Grafana"]


def test_the_morning_check_says_all_clear_or_lists_problems(monkeypatch):
    def fake_urlopen(url, timeout):
        url = url if isinstance(url, str) else url.full_url
        if url.endswith("/api/v1/alerts"):
            body = {"data": {"alerts": state["alerts"]}}
        elif url.endswith("/api/v1/rules"):
            body = {"data": {"groups": [{"rules": [1] * 31}]}}
        elif "component_check_ok" in url:
            body = {"data": {"result": [{"value": [0, "1"]}] * 19}}
        else:
            body = {"data": {"result": [{"value": [0, "0.29"]}]}}

        class Resp:
            def read(self):
                return json.dumps(body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Resp()

    state = {"alerts": []}
    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen)
    clear = notifier.daily_summary()
    assert clear["title"] == "Morning check: all clear"
    lines = clear["message"].splitlines()
    assert lines[:9] == ["1 Server ✓", "2 Containers ✓", "3 Data stores ✓", "4 Public access ✓", "5 App features ✓",
                         "6 Data jobs ✓", "7 Outside data ✓", "8 Backups ✓", "9 Monitoring ✓"]
    assert "31 alarms watching; 19/19 parts did their job" in lines[9] and "0.3 s" in lines[9]
    state["alerts"] = [{"state": "firing", "labels": {"area": "sage", "alertname": "SageSlow"},
                        "annotations": {"summary": "1 in 10 Sage answers took over 2 minutes"}}]
    problem = notifier.daily_summary()
    assert problem["title"] == "Morning check: 1 problem"
    assert "5 App features ✗ 1 in 10 Sage answers took over 2 minutes" in problem["message"].splitlines()
    assert "1 Server ✓" in problem["message"].splitlines()


def test_the_morning_check_reports_when_monitoring_is_unreadable(monkeypatch):
    def broken(url, timeout):
        raise ConnectionError("prometheus down")

    monkeypatch.setattr(notifier.urllib.request, "urlopen", broken)
    msg = notifier.daily_summary()
    assert msg["title"] == "Morning check: monitoring problem" and "prometheus down" in msg["message"]


def test_new_areas_read_plainly():
    [msg] = notifier.format_group(_payload(alertname="DiskFullWithin7Days", area="server", severity="warning"))
    assert msg["title"] == "Needs a look: Server" and "will be soon" in msg["message"]
    [msg] = notifier.format_group(_payload(alertname="ContainerKilledOutOfMemory", area="containers"))
    assert msg["title"] == "RED: Containers"
