"""Component self-tests: a check that fails, raises, or hangs is recorded as failing with why; due checks run and
others wait; results render as Prometheus lines; the map-health beacon counts only known events."""

import asyncio
import json
import time

import pytest

from src.dependencies import component_checks as cc
from src.dependencies import health_metrics as hm


class FakeRedis:
    def __init__(self):
        self.h: dict[str, str] = {}
        self.kv: dict[str, str] = {}

    async def hgetall(self, key):
        return {k.encode(): v.encode() for k, v in self.h.items()}

    async def hset(self, key, mapping):
        self.h.update(mapping)

    async def hincrby(self, key, field, by):
        self.h[field] = str(int(self.h.get(field, 0)) + by)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def expire(self, key, s):
        return True

    async def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key, 0)) + 1)
        return int(self.kv[key])


def _check(name, run, every=300, timeout=1.0, tier="core"):
    return cc.Check(name, name.title(), tier, every, timeout, run)


@pytest.mark.anyio
async def test_failures_are_recorded_with_the_reason():
    async def wrong():
        raise cc.CheckFailed("12 districts, expected 30")

    async def crashes():
        raise ConnectionError("refused")

    async def hangs():
        await asyncio.sleep(10)
        return "never"

    async def fine():
        return "30 districts"

    assert (await cc.run_one(_check("a", fine)))["ok"] == 1
    r = await cc.run_one(_check("b", wrong))
    assert r["ok"] == 0 and r["detail"] == "12 districts, expected 30"
    r = await cc.run_one(_check("c", crashes))
    assert r["ok"] == 0 and r["detail"].startswith("ConnectionError: refused")
    r = await cc.run_one(_check("d", hangs, timeout=0.05))
    assert r["ok"] == 0 and r["detail"].startswith("no answer within")


@pytest.mark.anyio
async def test_only_due_checks_run_and_results_render(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(cc, "get_async_redis", lambda: fake)
    ran = []

    async def fast():
        ran.append("fast")
        return "ok"

    async def slow():
        ran.append("slow")
        raise cc.CheckFailed("tile was empty")

    monkeypatch.setattr(cc, "CHECKS", (_check("fast", fast, every=600), _check("slow", slow, every=3600, tier="feed")))
    now = time.time()
    assert await cc.run_due(now) == ["fast", "slow"]
    text = "\n".join(await cc.metric_lines())
    assert 'component_check_ok{check="slow",component="Slow",tier="feed"} 1' in text  # one failure: retried first
    # The failed check is retried after RETRY_AFTER_S; the healthy one waits its full interval.
    assert await cc.run_due(now + cc.RETRY_AFTER_S + 1) == ["slow"]
    assert ran == ["fast", "slow", "slow"]
    text = "\n".join(await cc.metric_lines())
    assert 'component_check_ok{check="fast",component="Fast",tier="core"} 1' in text
    assert 'component_check_ok{check="slow",component="Slow",tier="feed"} 0' in text  # two in a row
    assert "# TYPE component_check_last_run_timestamp_seconds gauge" in text
    stored = json.loads(fake.h["slow"])
    assert stored["detail"] == "tile was empty" and stored["fails"] == 2


def test_tile_numbers_over_kigali():
    assert cc._tile_xy(30.06, -1.95, 12) == (2390, 2070)


@pytest.mark.anyio
async def test_map_health_counts_only_known_events(monkeypatch):
    from src.routes import client_health_routes as routes

    fake = FakeRedis()
    monkeypatch.setattr(hm, "_redis", lambda: fake)
    monkeypatch.setattr(routes, "get_async_redis", lambda: fake)
    for event in ("map_load", "map_load", "tile_error", "<script>"):
        await routes.client_health(routes.ClientHealthEvent(event=event), session=None)  # type: ignore[arg-type]
    assert fake.h == {"frontend_map_events_total|event=map_load": "2", "frontend_map_events_total|event=tile_error": "1"}
    for _ in range(routes.PER_USER_PER_MINUTE + 10):  # a loop in someone's browser is capped
        await routes.client_health(routes.ClientHealthEvent(event="map_error"), session=None)  # type: ignore[arg-type]
    assert int(fake.h["frontend_map_events_total|event=map_error"]) == routes.PER_USER_PER_MINUTE - 3  # 3 used above


@pytest.mark.anyio
async def test_minio_check_uses_the_shared_client_and_always_cleans_up(monkeypatch):
    import src.utils as utils

    calls = []

    class SharedS3:
        async def put_object(self, **kw):
            calls.append("put")

        async def get_object(self, **kw):
            raise ConnectionError("read failed")

        async def delete_object(self, **kw):
            calls.append("delete")

        async def __aexit__(self, *a):  # closing the app's shared client would break every later upload
            raise AssertionError("the shared client must not be closed")

    shared = SharedS3()

    async def get_client():
        return shared

    monkeypatch.setattr(utils, "get_async_s3_client", get_client)
    monkeypatch.setattr(utils, "get_bucket_name", lambda: "b")
    result = await cc.run_one(_check("minio", cc.check_minio))
    assert result["ok"] == 0 and "read failed" in result["detail"]
    assert calls == ["put", "delete"]  # deleted even though the read failed


@pytest.mark.anyio
async def test_check_details_are_internal_only(client):
    assert (await client.get("/internal/component-checks", headers={"Cf-Connecting-Ip": "203.0.113.9"})).status_code == 404
    assert (await client.get("/internal/component-checks")).status_code == 200
