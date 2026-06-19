from src.postgis_tiles import MVT_LAYER_NAME
from src.services.map_service import append_mvt_layers_with_legacy_source_fallbacks


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
