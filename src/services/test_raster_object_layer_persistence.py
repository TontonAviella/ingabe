from __future__ import annotations

from src.postgis_tiles import MVT_LAYER_NAME
from src.services.raster_object_layer_persistence import (
    build_raster_object_candidate_maplibre_layers,
    object_pmtiles_maxzoom,
)


def test_object_candidate_pmtiles_maxzoom_defaults_to_roof_detail(monkeypatch) -> None:
    monkeypatch.delenv("MUNDI_OBJECT_CANDIDATE_PMTILES_MAXZOOM", raising=False)
    assert object_pmtiles_maxzoom() == 22

    monkeypatch.setenv("MUNDI_OBJECT_CANDIDATE_PMTILES_MAXZOOM", "9")
    assert object_pmtiles_maxzoom() == 14

    monkeypatch.setenv("MUNDI_OBJECT_CANDIDATE_PMTILES_MAXZOOM", "28")
    assert object_pmtiles_maxzoom() == 22


def test_object_candidate_style_uses_visible_mvt_fill_and_outline() -> None:
    layers = build_raster_object_candidate_maplibre_layers("Lobjects")

    assert [layer["type"] for layer in layers] == ["fill", "line"]
    assert all(layer["source"] == "Lobjects" for layer in layers)
    assert all(layer["source-layer"] == MVT_LAYER_NAME for layer in layers)
    assert layers[0]["id"] == "raster-object-candidates-fill-Lobjects"
    assert layers[0]["paint"]["fill-color"][0] == "match"
    assert "candidate_class" in str(layers[0]["paint"]["fill-color"])
    assert layers[0]["paint"]["fill-opacity"][0] == "interpolate"
    assert layers[0]["paint"]["fill-outline-color"] == "#0f172a"
    assert layers[1]["id"] == "raster-object-candidates-outline-Lobjects"
    assert layers[1]["paint"]["line-color"][0] == "case"
    assert layers[1]["paint"]["line-opacity"] == 0.98
