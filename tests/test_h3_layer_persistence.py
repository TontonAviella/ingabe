from src.services.h3_layer_persistence import build_h3_risk_maplibre_layers
from src.services.h3_spatial_insight import H3SpatialInsightInput, create_h3_spatial_insight


def test_h3_transport_contract_targets_pmtiles_and_geoparquet():
    result = create_h3_spatial_insight(
        H3SpatialInsightInput(
            location_label="Tiny area",
            bbox=[30.0, -2.0, 30.01, -1.99],
            h3_resolution=9,
            domain="agriculture",
            analysis_goal="screen crop stress",
            risk_factors_json='{"rainfall_mm_24h": 25}',
            exposure_geojson="",
            max_hexes=5000,
        )
    )

    assert result["status"] == "success"
    transport = result["engines"]["transport"]
    assert transport["internal"] == "geojson_feature_collection"
    assert transport["browser_target"] == "MVT/PMTiles"
    assert transport["analytics_target"] == "GeoParquet"


def test_h3_spatial_insight_blocks_maps_without_real_evidence():
    result = create_h3_spatial_insight(
        H3SpatialInsightInput(
            location_label="Basemap only",
            bbox=[30.0, -2.0, 30.01, -1.99],
            h3_resolution=9,
            domain="housing",
            analysis_goal="guess from satellite basemap only",
            risk_factors_json="",
            exposure_geojson="",
            max_hexes=5000,
        )
    )

    assert result["status"] == "error"
    assert "needs at least one real evidence source" in result["error"]


def test_h3_spatial_insight_ignores_metadata_as_evidence():
    result = create_h3_spatial_insight(
        H3SpatialInsightInput(
            location_label="Metadata only",
            bbox=[30.0, -2.0, 30.01, -1.99],
            h3_resolution=9,
            domain="agriculture",
            analysis_goal="verify persisted H3 risk render path",
            risk_factors_json='{"domain":"agriculture","source":"satellite basemap","soil_saturation":"unknown"}',
            exposure_geojson="",
            max_hexes=5000,
        )
    )

    assert result["status"] == "error"
    assert "No layer was created from basemap imagery alone" in result["error"]


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
        90,
        "#e879f9",
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
