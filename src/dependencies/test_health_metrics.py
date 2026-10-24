"""Sage and AI-model health counters: kinds of model errors, the Prometheus text they render to, counting through
Redis, and /metrics refusing requests that came in through a public front end."""

import pytest

from src.dependencies import health_metrics as hm


class _Err(Exception):
    def __init__(self, text, status=None):
        super().__init__(text)
        self.status_code = status


@pytest.mark.parametrize("err,kind", [
    (_Err("Error code: 400 - Your credit balance is too low to access the Anthropic API", 400), "credit"),
    (_Err("Insufficient credits", 402), "credit"),
    (_Err("invalid x-api-key", 401), "auth"),
    (_Err("Rate limit exceeded", 429), "rate_limit"),
    (_Err("Overloaded", 529), "overloaded"),
    (_Err("Internal server error", 500), "upstream_5xx"),
    (_Err("prompt is too long: maximum context length", 400), "context"),
    (_Err("invalid tool call arguments", 400), "bad_request"),
    (_Err("something else"), "other"),
])
def test_ai_model_errors_are_sorted_by_kind(err, kind):
    assert hm.llm_error_kind(err) == kind


def test_counters_render_as_prometheus_text():
    text = "\n".join(hm.render({
        "sage_turns_total|outcome=completed": "7",
        "sage_turn_duration_seconds_bucket|le=5.0": "2",
        "sage_turn_duration_seconds_bucket|le=+Inf": "7",
        "sage_turn_duration_seconds_sum|": "180.5",
        "sage_turn_duration_seconds_count|": "7",
        "sage_tool_calls_total|outcome=timeout,tool=get_insurance_intelligence": "1",
    }))
    assert "# TYPE sage_turns_total counter" in text
    assert 'sage_turns_total{outcome="completed"} 7' in text
    assert "# TYPE sage_turn_duration_seconds histogram" in text
    assert text.count("# TYPE sage_turn_duration_seconds histogram") == 1
    assert 'sage_turn_duration_seconds_bucket{le="+Inf"} 7' in text
    assert "sage_turn_duration_seconds_sum 180.5" in text
    assert 'sage_tool_calls_total{outcome="timeout",tool="get_insurance_intelligence"} 1' in text


def test_credit_end_is_exported_only_when_set(monkeypatch):
    monkeypatch.setenv("SAGE_LLM_CREDIT_EXPIRES", "2026-11-05")
    assert hm._credit_expiry_line()[-1] == "sage_llm_credit_expires_timestamp_seconds 1793836800"
    monkeypatch.setenv("SAGE_LLM_CREDIT_EXPIRES", "5 Nov")
    assert hm._credit_expiry_line() == []
    monkeypatch.delenv("SAGE_LLM_CREDIT_EXPIRES")
    assert hm._credit_expiry_line() == []


class _FakeRedis:
    def __init__(self):
        self.h: dict[str, float] = {}

    async def hincrby(self, key, field, by):
        self.h[field] = self.h.get(field, 0) + by

    async def hincrbyfloat(self, key, field, by):
        self.h[field] = self.h.get(field, 0) + by

    async def hgetall(self, key):
        return {k.encode(): str(v).encode() for k, v in self.h.items()}

    def pipeline(self, transaction=False):
        outer = self

        class _P:
            calls = []

            def hincrby(self, *a):
                self.calls.append(outer.hincrby(*a))

            def hincrbyfloat(self, *a):
                self.calls.append(outer.hincrbyfloat(*a))

            async def execute(self):
                for c in self.calls:
                    await c

        return _P()


@pytest.mark.anyio
async def test_counting_goes_through_redis_and_never_raises(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(hm, "_redis", lambda: fake)
    await hm.count("sage_turns_total", {"outcome": "completed"})
    await hm.count("sage_turns_total", {"outcome": "completed"})
    await hm.observe("sage_turn_duration_seconds", 20.0)
    lines = "\n".join(await hm.metric_lines())
    assert 'sage_turns_total{outcome="completed"} 2' in lines
    assert 'sage_turn_duration_seconds_bucket{le="15.0"}' not in lines  # 20 s is above 15
    assert 'sage_turn_duration_seconds_bucket{le="30.0"} 1' in lines

    def broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr(hm, "_redis", broken)
    await hm.count("sage_turns_total", {"outcome": "completed"})  # no exception
    assert "sage_metrics_unavailable 1" in "\n".join(await hm.metric_lines())


@pytest.mark.anyio
async def test_metrics_are_internal_only(client):
    inside = await client.get("/metrics")
    assert inside.status_code == 200 and "http_requests_total" in inside.text
    for header in ("X-Forwarded-For", "Cf-Connecting-Ip"):
        outside = await client.get("/metrics", headers={header: "203.0.113.9"})
        assert outside.status_code == 404 and "http_requests_total" not in outside.text


@pytest.mark.anyio
async def test_each_tool_call_is_counted_by_outcome(monkeypatch):
    import asyncio

    from src.routes import message_routes

    fake = _FakeRedis()
    monkeypatch.setattr(hm, "_redis", lambda: fake)
    monkeypatch.setattr(message_routes, "_tool_timeout_seconds", lambda: 0.05)

    async def ok():
        return {"status": "success"}

    async def reported_error():
        return {"status": "error", "error": "no data"}

    async def stalls():
        await asyncio.sleep(5)

    async def crashes():
        raise ValueError("boom")

    await message_routes._within_tool_limit("t", ok())
    await message_routes._within_tool_limit("t", reported_error())
    stopped = await message_routes._within_tool_limit("t", stalls())
    with pytest.raises(ValueError):
        await message_routes._within_tool_limit("t", crashes())
    assert stopped["status"] == "error"
    assert fake.h == {"sage_tool_calls_total|outcome=ok,tool=t": 1, "sage_tool_calls_total|outcome=error,tool=t": 2,
                      "sage_tool_calls_total|outcome=timeout,tool=t": 1}
