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
            risk_factors_json="",
            exposure_geojson="",
            max_hexes=5000,
        )
    )

    assert result["status"] == "success"
    transport = result["engines"]["transport"]
    assert transport["internal"] == "geojson_feature_collection"
    assert transport["browser_target"] == "MVT/PMTiles"
    assert transport["analytics_target"] == "GeoParquet"


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
