"""Regression coverage for persistent Sage remote COG layers."""

from src.services.remote_cog_layer import (
    build_remote_cog_maplibre_layer,
    build_remote_cog_tile_url,
    infer_remote_cog_metadata,
)


# Regression: ISSUE-003 - Sage satellite imagery disappeared after page reload.
# Found by /qa on 2026-07-09
# Report: .gstack/qa-reports/qa-report-localhost-2026-07-09.md
def test_remote_cog_style_is_reloadable_and_scoped_to_its_bounds() -> None:
    metadata = {
        "remote_cog_url": "https://example.com/scene.tif?token=a&part=1",
        "expression": "visual",
        "maxzoom": 14,
        "opacity": 0.9,
    }

    tile_url = build_remote_cog_tile_url(metadata)
    source_id, source, layer = build_remote_cog_maplibre_layer(
        "Lscene", metadata, [29.7, -2.45, 29.95, -2.2]
    )

    assert "url=https%3A%2F%2Fexample.com%2Fscene.tif%3Ftoken%3Da%26part%3D1" in tile_url
    assert source_id == "remote-cog-source-Lscene"
    assert source["tiles"] == [tile_url]
    assert source["bounds"] == [29.7, -2.45, 29.95, -2.2]
    assert source["maxzoom"] == 14
    assert layer == {
        "id": "raster-layer-Lscene",
        "type": "raster",
        "source": "remote-cog-source-Lscene",
        "paint": {"raster-opacity": 0.9},
    }


def test_earth_search_cog_url_retains_scene_provenance() -> None:
    metadata = infer_remote_cog_metadata(
        "https://sentinel-cogs.s3.us-west-2.amazonaws.com/"
        "sentinel-s2-l2a-cogs/35/M/RT/2026/7/"
        "S2B_35MRT_20260705_0_L2A/TCI.tif"
    )

    assert metadata == {
        "source_catalog": "earth_search",
        "collection": "sentinel-2-l2a",
        "scene_id": "S2B_35MRT_20260705_0_L2A",
        "scene_date": "2026-07-05",
        "platform": "sentinel-2b",
    }
