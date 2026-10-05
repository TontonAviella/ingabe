from src.services.h3_layer_persistence import (
    build_h3_risk_maplibre_layers,
    h3_pmtiles_maxzoom_for_zoom_map,
)


def test_h3_pmtiles_style_uses_mvt_source_layer_and_risk_score():
    layers = build_h3_risk_maplibre_layers("Labc123", render_3d=True)

    assert [layer["type"] for layer in layers] == ["fill-extrusion", "line"]
    assert all(layer["source"] == "Labc123" for layer in layers)
    assert all(layer["source-layer"] == "reprojectedfgb" for layer in layers)
    extrusion = layers[0]
    assert extrusion["paint"]["fill-extrusion-color"][1] == [
        "coalesce",
        ["get", "risk_score"],
        0,
    ]
    assert extrusion["paint"]["fill-extrusion-height"] == [
        "*",
        ["coalesce", ["get", "risk_score"], 0],
        45,
    ]


def test_h3_attention_style_is_visible_over_orthophotos():
    layers = build_h3_risk_maplibre_layers("Labc123", render_3d=False)

    fill = layers[0]
    outline = layers[1]

    # Low risk is cyan, not green, so it stays visible over vegetation.
    assert fill["paint"]["fill-color"] == [
        "step",
        ["coalesce", ["get", "risk_score"], 0],
        "#06b6d4",
        40,
        "#facc15",
        60,
        "#f97316",
        80,
        "#dc2626",
    ]
    assert fill["paint"]["fill-opacity"] == [
        "interpolate",
        ["linear"],
        ["coalesce", ["get", "risk_score"], 0],
        0,
        0.18,
        40,
        0.38,
        60,
        0.58,
        80,
        0.76,
        100,
        0.84,
    ]
    assert outline["paint"]["line-color"] == [
        "case",
        [">=", ["coalesce", ["get", "risk_score"], 0], 60],
        "#ffffff",
        "#111827",
    ]


def test_h3_pmtiles_generation_targets_high_zoom_drone_views():
    maxzoom = h3_pmtiles_maxzoom_for_zoom_map(
        [
            {"h3_resolution": 10, "minzoom": 0, "maxzoom": 15},
            {"h3_resolution": 11, "minzoom": 15, "maxzoom": 17},
            {"h3_resolution": 12, "minzoom": 17, "maxzoom": 24},
        ]
    )

    assert maxzoom == 20
