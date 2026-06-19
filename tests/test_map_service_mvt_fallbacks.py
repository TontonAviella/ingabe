from src.postgis_tiles import MVT_LAYER_NAME
from src.services.map_service import (
    VISIBLE_H3_RISK_COLOR,
    VISIBLE_H3_RISK_OPACITY,
    append_mvt_layers_with_legacy_source_fallbacks,
    vector_source_maxzoom,
)


def test_mvt_style_layers_get_legacy_source_layer_fallbacks():
    target_layers: list[dict] = []
    append_mvt_layers_with_legacy_source_fallbacks(
        target_layers,
        [
            {
                "id": "h3-risk-fill-Labc-r10",
                "type": "fill",
                "source": "Labc",
                "source-layer": MVT_LAYER_NAME,
                "paint": {"fill-color": "#facc15"},
            }
        ],
    )

    assert [layer["id"] for layer in target_layers] == [
        "h3-risk-fill-Labc-r10",
        "h3-risk-fill-Labc-r10-legacy-reprojected",
    ]
    assert target_layers[0]["source-layer"] == MVT_LAYER_NAME
    assert target_layers[1]["source-layer"] == "reprojected"
    assert target_layers[1]["metadata"]["legacy_source_layer_fallback"] is True


def test_existing_h3_attention_layers_get_visible_runtime_style():
    target_layers: list[dict] = []
    append_mvt_layers_with_legacy_source_fallbacks(
        target_layers,
        [
            {
                "id": "h3-risk-fill-Labc-r10",
                "type": "fill",
                "source": "Labc",
                "source-layer": MVT_LAYER_NAME,
                "filter": ["==", ["get", "h3_resolution"], 12],
                "paint": {
                    "fill-color": "#22c55e",
                    "fill-opacity": 0.58,
                },
            }
        ],
    )

    assert target_layers[0]["paint"]["fill-color"] == VISIBLE_H3_RISK_COLOR
    assert target_layers[0]["paint"]["fill-opacity"] == VISIBLE_H3_RISK_OPACITY
    assert target_layers[0]["filter"] == [
        "any",
        ["==", ["get", "h3_resolution"], 12],
        ["==", ["get", "h3_resolution"], "12"],
    ]
    assert target_layers[1]["paint"]["fill-color"] == VISIBLE_H3_RISK_COLOR
    assert target_layers[1]["paint"]["fill-opacity"] == VISIBLE_H3_RISK_OPACITY
    assert target_layers[1]["filter"] == [
        "any",
        ["==", ["get", "h3_resolution"], 12],
        ["==", ["get", "h3_resolution"], "12"],
    ]


def test_vector_source_maxzoom_accepts_pmtiles_metadata():
    assert vector_source_maxzoom({"pmtiles_maxzoom": 20}) == 20
    assert vector_source_maxzoom({"pmtiles_maxzoom": "20"}) == 20
    assert vector_source_maxzoom({"pmtiles_maxzoom": 99}) is None
    assert vector_source_maxzoom({}) is None
