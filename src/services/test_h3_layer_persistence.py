from __future__ import annotations

from src.services.h3_layer_persistence import build_h3_risk_maplibre_layers


def test_h3_risk_style_uses_zoom_bands_for_adaptive_resolution() -> None:
    layers = build_h3_risk_maplibre_layers(
        "Ltest",
        render_3d=False,
        zoom_resolution_map=[
            {"h3_resolution": 10, "minzoom": 0, "maxzoom": 15},
            {"h3_resolution": 11, "minzoom": 15, "maxzoom": 17},
            {"h3_resolution": 12, "minzoom": 17, "maxzoom": 24},
        ],
    )

    assert len(layers) == 6
    assert layers[0]["id"] == "h3-risk-fill-Ltest-r10"
    assert layers[0]["filter"] == ["==", ["get", "h3_resolution"], 10]
    assert "minzoom" not in layers[0]
    assert layers[0]["maxzoom"] == 15.0

    assert layers[2]["id"] == "h3-risk-fill-Ltest-r11"
    assert layers[2]["filter"] == ["==", ["get", "h3_resolution"], 11]
    assert layers[2]["minzoom"] == 15.0
    assert layers[2]["maxzoom"] == 17.0

    assert layers[4]["id"] == "h3-risk-fill-Ltest-r12"
    assert layers[4]["filter"] == ["==", ["get", "h3_resolution"], 12]
    assert layers[4]["minzoom"] == 17.0
    assert "maxzoom" not in layers[4]


def test_h3_risk_style_keeps_legacy_single_layer_without_zoom_map() -> None:
    layers = build_h3_risk_maplibre_layers("Ltest", render_3d=False)

    assert len(layers) == 2
    assert layers[0]["id"] == "h3-risk-fill-Ltest"
    assert "filter" not in layers[0]
