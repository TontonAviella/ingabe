from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from src.services import geolibre_runner
from src.services.geolibre_runner import GeolibreRunInput, run_geolibre_tool


def _install_fake_geolibre(monkeypatch):
    fake = ModuleType("geolibre_wasm")

    def list_tools():
        return ["write_geoparquet", "spectral_index", "slope"]

    def list_manifests():
        return [
            {
                "id": "write_geoparquet",
                "source": "geolibre",
                "category": "Conversion",
                "parameters": [{"name": "input"}, {"name": "output"}],
            },
            {
                "id": "spectral_index",
                "source": "geolibre",
                "category": "Raster",
                "parameters": [{"name": "input"}, {"name": "index"}],
            },
            {
                "id": "slope",
                "source": "whitebox",
                "category": "Raster",
                "parameters": [{"name": "input"}, {"name": "output"}],
            },
        ]

    def run_tool(tool, args=None, input=None):
        assert tool == "write_geoparquet"
        assert args == ["--input=/work/in.geojson", "--output=/work/out.parquet"]
        assert input == {"in.geojson": b'{"type":"FeatureCollection","features":[]}'}
        return SimpleNamespace(
            exit_code=0,
            stdout=["reading input vector", "writing GeoParquet"],
            files={"out.parquet": b"PAR1"},
        )

    fake.list_tools = list_tools
    fake.list_manifests = list_manifests
    fake.run_tool = run_tool
    fake.runtime_path = lambda: "/tmp/geolibre-cli.wasm"
    monkeypatch.setitem(sys.modules, "geolibre_wasm", fake)
    geolibre_runner._manifest_by_id.cache_clear()
    return fake


def test_run_geolibre_tool_emits_safe_evidence(monkeypatch):
    _install_fake_geolibre(monkeypatch)
    evidence: list[tuple[str, dict]] = []
    posthog: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        geolibre_runner,
        "record_pipeline_evidence",
        lambda event, props: evidence.append((event, dict(props))) or True,
    )

    def fake_capture(event, *, distinct_id=None, properties=None, groups=None):
        posthog.append((event, dict(properties or {})))
        return True

    monkeypatch.setattr(geolibre_runner, "capture_backend_event", fake_capture)

    result = run_geolibre_tool(
        GeolibreRunInput(
            tool_id="write_geoparquet",
            args=["--input=/work/in.geojson", "--output=/work/out.parquet"],
            input_files={"in.geojson": b'{"type":"FeatureCollection","features":[]}'},
            source_category="open_buildings",
            pipeline_family="open_buildings_geolibre",
            analysis_domain="housing",
            evidence_kind="open_buildings_vector_conversion",
        )
    )

    assert result["status"] == "success"
    assert result["tool_source"] == "geolibre"
    assert result["output_file_count"] == 1
    assert result["output_files"][0]["media_kind"] == "geoparquet"

    assert [event for event, _props in evidence] == [
        "geolibre_tool_completed",
        "geospatial_pipeline_flow_completed",
    ]
    assert [event for event, _props in posthog] == [
        "geolibre_tool_completed",
        "geospatial_pipeline_flow_completed",
    ]
    props = evidence[0][1]
    assert props["tool_id"] == "write_geoparquet"
    assert props["source_category"] == "open_buildings"
    assert props["success"] is True
    assert "url" not in props


def test_list_geolibre_tool_manifests_filters(monkeypatch):
    _install_fake_geolibre(monkeypatch)

    result = geolibre_runner.list_geolibre_tool_manifests(
        search="",
        source="geolibre",
        category="Raster",
        limit=10,
    )

    assert result["status"] == "success"
    assert result["matched_count"] == 1
    assert result["tools"][0]["id"] == "spectral_index"
