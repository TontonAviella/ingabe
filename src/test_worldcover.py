"""Tests for ESRI 10m Annual Land Use Land Cover integration.

Three tiers:
  1. Unit tests — pure logic (tile URLs, LUT correctness)
  2. Integration tests — endpoint + DB + map_service style pipeline
  3. Remote tests — actual S3 COG reads via render_tile (marked @pytest.mark.remote)
"""

import io
import json
import pytest
import numpy as np

from src.worldcover import (
    WORLDCOVER_CLASSES,
    LUT_ALL,
    LUT_CROPLAND,
    get_lut,
    get_tile_urls,
    get_rwanda_tile_urls,
    render_tile,
)


# ── Kigali tile coordinates (pre-computed) ──
# lat=-1.94, lon=29.87 → z=10: x=596, y=517  |  z=8: x=149, y=129
KIGALI_Z, KIGALI_X, KIGALI_Y = 10, 596, 517


# ===========================================================================
# 1. Unit tests — tile URL resolution
# ===========================================================================

class TestGetTileUrls:
    def test_rwanda_zones(self):
        urls = get_tile_urls(["35M", "36M"])
        assert len(urls) == 2
        assert any("35M" in u for u in urls)
        assert any("36M" in u for u in urls)

    def test_single_zone(self):
        urls = get_tile_urls(["35M"])
        assert len(urls) == 1
        assert "35M" in urls[0]

    def test_url_format(self):
        urls = get_tile_urls(["35M", "36M"])
        for url in urls:
            assert url.startswith("https://io-10m-annual-lulc.s3.us-west-2.amazonaws.com/")
            assert url.endswith(".tif")

    def test_rwanda_convenience(self):
        urls = get_rwanda_tile_urls()
        assert len(urls) == 2
        assert urls == get_tile_urls(["35M", "36M"])

    def test_custom_year(self):
        urls = get_tile_urls(["35M"], year=2023)
        assert "2023" in urls[0]


# ===========================================================================
# 2. Unit tests — colormap LUTs
# ===========================================================================

class TestColormaps:
    def test_worldcover_classes_count(self):
        assert len(WORLDCOVER_CLASSES) == 9

    def test_cropland_value(self):
        assert 5 in WORLDCOVER_CLASSES
        assert WORLDCOVER_CLASSES[5] == "Crops"

    def test_lut_shape(self):
        assert LUT_ALL.shape == (256, 4)
        assert LUT_CROPLAND.shape == (256, 4)

    def test_lut_dtype(self):
        assert LUT_ALL.dtype == np.uint8
        assert LUT_CROPLAND.dtype == np.uint8

    def test_lut_all_has_colors_for_classes(self):
        for val in WORLDCOVER_CLASSES:
            rgba = LUT_ALL[val]
            assert rgba[3] > 0, f"Class {val} ({WORLDCOVER_CLASSES[val]}) has zero alpha"

    def test_lut_all_nodata_transparent(self):
        assert LUT_ALL[0][3] == 0

    def test_lut_cropland_highlights_5(self):
        rgba = LUT_CROPLAND[5]
        assert rgba[3] == 255
        assert rgba[1] > rgba[0]  # green > red

    def test_lut_cropland_mutes_others(self):
        for val in WORLDCOVER_CLASSES:
            if val == 5:
                continue
            rgba = LUT_CROPLAND[val]
            assert rgba[3] < 255, f"Class {val} should be semi-transparent in cropland mode"

    def test_get_lut_all(self):
        assert np.array_equal(get_lut("all"), LUT_ALL)

    def test_get_lut_cropland(self):
        assert np.array_equal(get_lut("cropland"), LUT_CROPLAND)

    def test_get_lut_default(self):
        assert np.array_equal(get_lut("unknown"), LUT_ALL)


# ===========================================================================
# 3. Router — input validation (no network needed)
# ===========================================================================

@pytest.mark.anyio
async def test_worldcover_tile_invalid_coords(client):
    response = await client.get("/api/worldcover/-1/0/0.png")
    assert response.status_code == 400

    response = await client.get("/api/worldcover/20/0/0.png")
    assert response.status_code == 400


@pytest.mark.anyio
async def test_worldcover_tile_invalid_mode(client):
    response = await client.get("/api/worldcover/5/0/0.png?mode=invalid")
    assert response.status_code == 422


# ===========================================================================
# 4. Remote tests — real S3 COG data flow (render_tile → rio-tiler → PNG)
# ===========================================================================

@pytest.mark.remote
class TestRenderTileRealData:
    """Tests that actually read ESRI LULC COGs from S3.

    These require network access to io-10m-annual-lulc.s3.us-west-2.amazonaws.com.
    Run with: pytest -xvs -m remote src/test_worldcover.py
    """

    def test_render_tile_kigali_all_mode(self):
        """render_tile with Kigali coords returns a real PNG with visible pixels."""
        png_bytes = render_tile(KIGALI_X, KIGALI_Y, KIGALI_Z, mode="all")

        assert png_bytes is not None, "render_tile returned None — tile wrongly classified as outside extent"
        assert len(png_bytes) > 1000, (
            f"PNG is only {len(png_bytes)} bytes — likely a transparent/empty tile, not real data"
        )

        # Decode the PNG and verify it has non-transparent pixels
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        assert img.size == (256, 256), f"Expected 256x256, got {img.size}"
        assert img.mode == "RGBA"

        pixels = np.array(img)
        alpha = pixels[:, :, 3]
        non_transparent_count = int(np.sum(alpha > 0))
        assert non_transparent_count > 1000, (
            f"Only {non_transparent_count} non-transparent pixels — expected significant land cover data for Kigali"
        )

        # Verify we see multiple land cover classes (Kigali has built-up + cropland + vegetation)
        opaque_mask = alpha > 0
        unique_colors = set()
        for r, g, b, a in pixels[opaque_mask]:
            unique_colors.add((int(r), int(g), int(b)))
        assert len(unique_colors) >= 2, (
            f"Only {len(unique_colors)} unique colour(s) — expected multiple land cover classes near Kigali"
        )

    def test_render_tile_kigali_cropland_mode(self):
        """Cropland mode returns PNG with green highlights and muted background."""
        png_bytes = render_tile(KIGALI_X, KIGALI_Y, KIGALI_Z, mode="cropland")

        assert png_bytes is not None
        assert len(png_bytes) > 500

        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        pixels = np.array(img)
        alpha = pixels[:, :, 3]

        # Should have some visible pixels
        assert int(np.sum(alpha > 0)) > 500

        # Verify we see the cropland highlight green (34, 197, 94) or the muted gray (120, 120, 120)
        has_highlight = False
        has_muted = False
        for r, g, b, a in pixels[alpha > 0]:
            if int(g) > 150 and int(r) < 100:  # greenish = cropland highlight
                has_highlight = True
            if int(a) < 200 and int(r) == int(g) == int(b):  # gray + semi-transparent = muted
                has_muted = True
        assert has_highlight or has_muted, (
            "Expected cropland highlight (green) or muted (gray) pixels — saw neither"
        )

    def test_render_tile_outside_rwanda_returns_none(self):
        """Tile far from Rwanda should return None (TileOutsideBounds)."""
        # North pole area at z=10
        result = render_tile(512, 100, 10, mode="all")
        assert result is None, "Expected None for tile outside Rwanda/WorldCover extent"


# ===========================================================================
# 5. Endpoint integration — real data through the HTTP layer
# ===========================================================================

@pytest.mark.remote
@pytest.mark.anyio
async def test_worldcover_endpoint_kigali_real_png(client):
    """GET /api/worldcover/10/596/517.png returns a real colormapped PNG."""
    response = await client.get(
        f"/api/worldcover/{KIGALI_Z}/{KIGALI_X}/{KIGALI_Y}.png?mode=all"
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers.get("access-control-allow-origin") == "*"
    assert "max-age" in response.headers.get("cache-control", "")

    png_bytes = response.content
    assert len(png_bytes) > 1000, (
        f"Endpoint returned only {len(png_bytes)} bytes — not real WorldCover data"
    )

    # Decode and verify actual pixel content
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes))
    assert img.size == (256, 256)

    pixels = np.array(img)
    non_transparent = int(np.sum(pixels[:, :, 3] > 0))
    assert non_transparent > 1000, f"Only {non_transparent} visible pixels from endpoint"


@pytest.mark.remote
@pytest.mark.anyio
async def test_worldcover_endpoint_cropland_mode(client):
    """Cropland mode via endpoint returns different data than 'all' mode."""
    resp_all = await client.get(
        f"/api/worldcover/{KIGALI_Z}/{KIGALI_X}/{KIGALI_Y}.png?mode=all"
    )
    resp_crop = await client.get(
        f"/api/worldcover/{KIGALI_Z}/{KIGALI_X}/{KIGALI_Y}.png?mode=cropland"
    )

    assert resp_all.status_code == 200
    assert resp_crop.status_code == 200

    # The two modes should produce different PNGs
    assert resp_all.content != resp_crop.content, (
        "all and cropland modes returned identical bytes — colormap switching may be broken"
    )


# ===========================================================================
# 6. Map service integration — worldcover layer → style.json tile source
# ===========================================================================

@pytest.mark.anyio
async def test_worldcover_layer_appears_in_map_style(auth_client):
    """Create a map, insert a worldcover raster layer, verify style.json has the tile source."""
    from src.utils import generate_id
    from src.structures import async_conn

    # 1. Create a fresh map
    map_response = await auth_client.post(
        "/api/maps/create", json={"title": "WorldCover Style Test"}
    )
    assert map_response.status_code == 200
    map_id = map_response.json()["id"]

    layer_id = generate_id(prefix="L")
    style_id = generate_id(prefix="S")
    meta = json.dumps({"worldcover": True, "worldcover_mode": "cropland"})

    # 2. Insert a worldcover raster layer directly (same as the tool handler does)
    async with async_conn("test_worldcover_style") as conn:
        # Get owner from the map
        row = await conn.fetchrow(
            "SELECT owner_uuid FROM user_mundiai_maps WHERE id = $1", map_id
        )
        owner_uuid = row["owner_uuid"]

        await conn.execute(
            """
            INSERT INTO map_layers
            (layer_id, owner_uuid, name, type, metadata, bounds, source_map_id,
             created_on, last_edited)
            VALUES ($1, $2, 'ESA WorldCover — Cropland', 'raster',
                    $3, $4, $5,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            layer_id, owner_uuid, meta,
            [28.86, -2.84, 30.90, -1.05], map_id,
        )

        await conn.execute(
            """
            INSERT INTO layer_styles (style_id, layer_id, style_json, created_by, created_on)
            VALUES ($1, $2, '[]', $3, CURRENT_TIMESTAMP)
            """,
            style_id, layer_id, owner_uuid,
        )

        await conn.execute(
            """
            INSERT INTO map_layer_styles (map_id, layer_id, style_id)
            VALUES ($1, $2, $3)
            """,
            map_id, layer_id, style_id,
        )

        await conn.execute(
            """
            UPDATE user_mundiai_maps
            SET layers = CASE
                WHEN layers IS NULL THEN ARRAY[$1]
                ELSE array_append(layers, $1)
            END
            WHERE id = $2
            """,
            layer_id, map_id,
        )

    # 3. Fetch style.json — this exercises the real map_service.py code path
    style_response = await auth_client.get(f"/api/maps/{map_id}/style.json")
    assert style_response.status_code == 200
    style_json = style_response.json()

    # 4. Verify the worldcover tile source exists in the style
    sources = style_json.get("sources", {})
    worldcover_source = None
    for source_name, source_details in sources.items():
        if source_details.get("type") == "raster" and "tiles" in source_details:
            tiles = source_details["tiles"]
            if any("worldcover" in t and "mode=cropland" in t for t in tiles):
                worldcover_source = source_details
                break

    assert worldcover_source is not None, (
        f"WorldCover tile source not found in style.json sources. "
        f"Sources: {json.dumps(sources, indent=2)}"
    )

    # Verify tile URL structure
    tile_url = worldcover_source["tiles"][0]
    assert "/api/worldcover/{z}/{x}/{y}.png" in tile_url
    assert "mode=cropland" in tile_url
    assert worldcover_source["tileSize"] == 256
    assert worldcover_source.get("maxzoom") == 14

    # 5. Verify the raster layer entry exists in the style layers
    layers = style_json.get("layers", [])
    raster_layer = next(
        (l for l in layers if l.get("id") == f"raster-layer-{layer_id}"),
        None,
    )
    assert raster_layer is not None, (
        f"Raster layer 'raster-layer-{layer_id}' not found in style.json layers"
    )
    assert raster_layer["type"] == "raster"
    # Should have opacity paint property
    assert raster_layer.get("paint", {}).get("raster-opacity") == 0.85


# ===========================================================================
# 7. Tool definition check
# ===========================================================================

def test_add_land_cover_tool_in_tools_json():
    with open("src/geoprocessing/tools.json") as f:
        tools = json.load(f)

    tool = next((t for t in tools if t["function"]["name"] == "add_land_cover_layer"), None)
    assert tool is not None, "add_land_cover_layer tool not found in tools.json"

    params = tool["function"]["parameters"]
    assert "mode" in params["properties"]
    assert params["properties"]["mode"]["enum"] == ["all", "cropland"]
