"""Request traces (src/wsgi.py TraceRequestMiddleware): a span per request named by the full route template, router
prefix included, and outside calls traced without their query string (keys travel there)."""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import src.wsgi as wsgi


@pytest.mark.anyio
async def test_a_request_span_is_named_by_its_full_route(auth_client, monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(wsgi, "_http_tracer", provider.get_tracer("test"))

    await auth_client.get("/api/maps/MNOTAREALMAP/tree")  # a prefixed router's route (404: no such map)
    await auth_client.get("/no/route/here/at-all")

    names = [s.name for s in exporter.get_finished_spans()]
    assert "GET /api/maps/{map_id}/tree" in names  # not "GET /{map_id}/tree": that would merge routes
    assert "GET other" in names
    tree = next(s for s in exporter.get_finished_spans() if s.name == "GET /api/maps/{map_id}/tree")
    assert tree.attributes["http.route"] == "/api/maps/{map_id}/tree"


def test_outside_calls_lose_their_query_string():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("t").start_as_current_span("GET") as span:
        wsgi._url_without_query(span, "https://detect.roboflow.com/model/1?api_key=SECRET&confidence=40")
    (s,) = exporter.get_finished_spans()
    assert s.attributes["http.url"] == "https://detect.roboflow.com/model/1"
    assert "SECRET" not in str(dict(s.attributes))
