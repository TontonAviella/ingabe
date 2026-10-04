"""Sage flight recorder: the spans Langfuse receives for one Sage turn."""
from __future__ import annotations

import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.services import sage_flight_recorder as fr


@pytest.fixture
def recorded():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def _by_name(exporter: InMemorySpanExporter) -> dict:
    return {span.name: span for span in exporter.get_finished_spans()}


def test_tracing_is_off_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(fr, "_tracer", None)
    assert fr.get_tracer() is None
    with fr.sage_turn_trace(session_id="1", user_id="u") as turn:
        assert not turn.enabled
        turn.routing(user_text="hi", reason="r", categories=[], small_talk=True,
                     tools=[], shortlist=None, model="m")
        turn.generation(model="m", messages=[], tools=None, step=0).end(output="x")
        turn.tool_started("c1", "t", {})
        turn.tool_finished("c1", "{}")


def test_a_turn_records_routing_model_call_tool_and_problem_tags(recorded) -> None:
    tracer, exporter = recorded
    tools = [{"type": "function", "function": {"name": "get_forecast"}}]
    with fr.sage_turn_trace(session_id="42", user_id="user-1", metadata={"map_id": "M1"},
                            tracer=tracer) as turn:
        turn.routing(user_text="rain in Huye?", reason="agri", categories=["agriculture"],
                     small_talk=False, tools=tools, shortlist="bm25", model="nemotron")
        gen = turn.generation(
            model="nemotron",
            messages=[{"role": "system", "content": "x" * 9000}, {"role": "user", "content": "rain in Huye?"}],
            tools=tools, step=0, parameters={"max_tokens": 512},
        )
        gen.first_token()
        gen.end(output={"content": None, "tool_calls": [{"function": {"name": "get_forecast"}}]})
        turn.tool_started("call-1", "get_forecast", {"district": "Huye"})
        turn.tool_finished("call-1", json.dumps({"error": "upstream timeout"}))
        turn.set_output("Sorry, the forecast service timed out.")

    spans = _by_name(exporter)
    root, gen_span, tool = spans["sage.turn"], spans["model_call"], spans["get_forecast"]
    assert root.parent is None
    assert {gen_span.parent.span_id, tool.parent.span_id} == {root.context.span_id}
    assert gen_span.context.trace_id == root.context.trace_id

    assert root.attributes["langfuse.session.id"] == "42"
    assert root.attributes["langfuse.trace.metadata.map_id"] == "M1"
    assert root.attributes["langfuse.observation.input"] == "rain in Huye?"
    assert set(root.attributes["langfuse.trace.tags"]) == {
        "tool_error", "shortlist_bm25_fallback", "shortlist:bm25"}
    assert root.attributes["langfuse.observation.level"] == "WARNING"

    assert gen_span.attributes["langfuse.observation.type"] == "generation"
    assert gen_span.attributes["langfuse.observation.model.name"] == "nemotron"
    assert "langfuse.observation.completion_start_time" in gen_span.attributes
    shown = json.loads(gen_span.attributes["langfuse.observation.input"])
    assert shown[0]["content"] == "[system prompt, 9000 chars]"

    assert tool.attributes["langfuse.observation.type"] == "tool"
    assert json.loads(tool.attributes["langfuse.observation.input"]) == {"district": "Huye"}
    assert tool.attributes["langfuse.observation.level"] == "WARNING"
    assert tool.attributes["langfuse.observation.status_message"] == "upstream timeout"


def test_turn_does_not_nest_under_the_infra_span(recorded) -> None:
    tracer, exporter = recorded
    infra = TracerProvider().get_tracer("infra")
    with infra.start_as_current_span("app.process_chat_interaction") as outer:
        with fr.sage_turn_trace(session_id="1", user_id="u", tracer=tracer):
            pass
    root = _by_name(exporter)["sage.turn"]
    assert root.parent is None
    assert root.attributes["langfuse.trace.metadata.infra_trace_id"] == format(
        outer.get_span_context().trace_id, "032x"
    )


def test_a_crash_marks_the_turn_error_and_closes_open_steps(recorded) -> None:
    tracer, exporter = recorded
    with pytest.raises(RuntimeError):
        with fr.sage_turn_trace(session_id="1", user_id="u", tracer=tracer) as turn:
            turn.generation(model="m", messages=[], tools=None, step=0)
            raise RuntimeError("boom")
    spans = _by_name(exporter)
    root = spans["sage.turn"]
    assert root.status.status_code == trace.StatusCode.ERROR
    assert root.attributes["langfuse.observation.level"] == "ERROR"
    assert {"turn_error", "unfinished_step"} <= set(root.attributes["langfuse.trace.tags"])
    assert spans["model_call"].attributes["langfuse.observation.level"] == "WARNING"


def test_long_outputs_are_clipped() -> None:
    clipped = fr._clip("a" * 10_000, 100)
    assert clipped.startswith("a" * 100) and clipped.endswith("[+9900 chars]")
