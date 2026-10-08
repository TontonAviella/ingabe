import pytest

from src.services.map_service import style_for_native_render


@pytest.mark.anyio
async def test_sources_the_renderer_cannot_fetch_are_left_out_with_their_layers(tmp_path):
    style = {
        "sources": {
            "basemap": {"type": "raster", "tiles": ["https://tiles.example/{z}/{x}/{y}.png"]},
            "worldcover-source-Lwc1": {"type": "raster", "tiles": ["/api/worldcover/{z}/{x}/{y}.png?mode=all"]},
            "Lvector1": {"type": "vector", "url": "pmtiles://http://minio:9000/b/k.pmtiles?sig=1"},
            "pointer-positions": {"type": "geojson", "data": {"type": "FeatureCollection", "features": []}},
        },
        "layers": [
            {"id": "basemap", "type": "raster", "source": "basemap"},
            {"id": "raster-layer-Lwc1", "type": "raster", "source": "worldcover-source-Lwc1"},
            {"id": "Lvector1-fill", "type": "fill", "source": "Lvector1"},
        ],
    }

    drawable, left_out = await style_for_native_render(style, str(tmp_path))

    assert left_out == ["worldcover-source-Lwc1"]
    assert set(drawable["sources"]) == {"basemap", "Lvector1", "pointer-positions"}
    assert [ml["id"] for ml in drawable["layers"]] == ["basemap", "Lvector1-fill"]
    assert "worldcover-source-Lwc1" in style["sources"], "the caller's style is not changed"
