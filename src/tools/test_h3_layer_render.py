"""H3 risk layers are saved when possible; the inline preview is only the fallback."""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest

from src.services.h3_layer_persistence import PersistedH3Layer
from src.tools import h3_layer_render

META = SimpleNamespace(user_uuid="u", conversation_id=1, map_id="M1", project_id="P1")


def _result():
    feature = {"type": "Feature", "geometry": None, "properties": {"risk_score": 70}}
    return {"status": "success", "geojson": {"type": "FeatureCollection", "features": [feature]}}


@pytest.fixture
def sent(monkeypatch):
    updates: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_action(conversation_id, label, **kwargs):
        payload = SimpleNamespace(updates={})
        yield payload
        updates.append(payload.updates)

    monkeypatch.setattr(h3_layer_render, "kue_ephemeral_action", fake_action)
    return updates


@pytest.mark.anyio
async def test_a_saved_layer_is_announced_and_the_geojson_left_out(monkeypatch, sent):
    kinds = []

    async def fake_persist(**kwargs):
        kinds.append(kwargs["analysis_kind"])
        return PersistedH3Layer(layer_id="L1", style_id="S1", pmtiles_key="k.pmtiles", geoparquet_key=None,
                                pmtiles_maxzoom=20, bounds=None, feature_count=1, geometry_type="polygon")

    monkeypatch.setattr(h3_layer_render, "persist_h3_spatial_insight_layer", fake_persist)
    result = _result()
    persisted = await h3_layer_render.render_h3_risk_layer(
        result, meta=META, layer_name="Building Exposure - X", render_3d=False, bounds=None,
        analysis_kind="open_buildings_h3_exposure")
    h3_layer_render.compact_h3_geojson(result, persisted)
    assert kinds == ["open_buildings_h3_exposure"]
    assert sent == [{"h3_layer_persisted": {"layer_id": "L1", "name": "Building Exposure - X", "pmtiles": True,
                                            "geoparquet": False, "pmtiles_maxzoom": 20, "feature_count": 1}}]
    assert result["layer_id"] == "L1" and result["engines"]["transport"]["current"] == "pmtiles_vector_layer"
    assert "omitted" in result["geojson"] and result["geojson_feature_count"] == 1


@pytest.mark.anyio
async def test_when_saving_fails_the_preview_says_it_will_not_survive_a_reload(monkeypatch, sent):
    async def broken_persist(**kwargs):
        raise RuntimeError("tippecanoe failed")

    monkeypatch.setattr(h3_layer_render, "persist_h3_spatial_insight_layer", broken_persist)
    result = _result()
    persisted = await h3_layer_render.render_h3_risk_layer(
        result, meta=META, layer_name="Spatial Risk - X", render_3d=False, bounds=None, analysis_kind="h3_spatial_insight")
    h3_layer_render.compact_h3_geojson(result, persisted)
    assert persisted is None and "add_geojson_layer" in sent[0]
    assert result["engines"]["transport"]["current"] == "inline_geojson_preview_fallback"
    assert "reload" in result["map_note"]
    assert json.loads(result["geojson"])["features"][0]["properties"]["risk_score"] == 70
