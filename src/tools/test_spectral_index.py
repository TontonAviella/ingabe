"""The satellite index tool reads numbers over the box it was asked for (2026-11-23).

Sage's compute_spectral_index got a box in latitude and longitude and laid it straight onto each
band file's own grid, which is in metres (UTM). The window pointed outside every scene, so the tool
drew a layer and returned "No valid pixels" at every place tried: the Cyampirita photo, Musanze,
Nyagatare, Bugesera and Huye. Moving the box onto the scene's grid first gave 13,640 pixels over
Cyampirita from the same files.

The band files here are real GeoTIFFs on the grid Sentinel-2 uses east of 30 degrees E in Rwanda
(EPSG:32736), written around the Cyampirita photo's box.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from src.services.stac_service import STACService
from src.tools import spectral_index
from src.tools.pyd import IngabeToolCallMetaArgs
from src.tools.spectral_index import ComputeSpectralIndexArgs, _compute_stats_from_cogs, compute_spectral_index

BOX = [30.41749, -1.7009, 30.43148, -1.69289]  # west, south, east, north: the Cyampirita photo
CLOUD, VEGETATION = 9, 4  # Sentinel-2 scene classes
META = IngabeToolCallMetaArgs(
    user_uuid="user-test", conversation_id=1, map_id="MTESTAAAAAAA", project_id="PTESTBBBBBBB", session=None
)


def _band(path: Path, pixel_m: int, value_left: int, value_right: Optional[int] = None, dtype: str = "uint16") -> str:
    """A band file covering BOX with 300 m to spare; the western half holds value_left, the eastern value_right."""
    west, south, east, north = transform_bounds("EPSG:4326", "EPSG:32736", *BOX)
    left, top = west - 300, north + 300
    width = int((east - west + 600) // pixel_m) + 1
    height = int((north - south + 600) // pixel_m) + 1
    data = np.full((height, width), value_left, dtype=dtype)
    if value_right is not None:
        data[:, width // 2:] = value_right
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=1, dtype=dtype, crs="EPSG:32736",
        transform=from_origin(left, top, pixel_m, pixel_m),
    ) as dst:
        dst.write(data, 1)
    return str(path)


@pytest.fixture
def scene(tmp_path: Path) -> Dict[str, str]:
    """Red and near-infrared at 10 m, short-wave at 20 m, and a class map at 20 m whose eastern half is cloud.
    Under the cloud both bands read the same, so an unmasked NDVI there is 0."""
    return {
        "red": _band(tmp_path / "red.tif", 10, 1000, 4000),
        "nir": _band(tmp_path / "nir.tif", 10, 3000, 4000),
        "swir22": _band(tmp_path / "swir22.tif", 20, 1000),
        "scl": _band(tmp_path / "scl.tif", 20, VEGETATION, CLOUD, dtype="uint8"),
    }


def test_a_latitude_longitude_box_gets_numbers_from_a_scene_on_a_metre_grid(scene):
    stats = _compute_stats_from_cogs(scene["red"], scene["nir"], BOX, "ndvi", scene["scl"])
    assert stats is not None
    assert stats["mean"] == pytest.approx(0.5)  # (3000 - 1000) / (3000 + 1000) on the clear half
    assert stats["max"] == pytest.approx(0.5) and stats["min"] == pytest.approx(0.5)
    # the box is about 1,556 m by 886 m: about 13,800 pixels of 10 m, half of them clear
    assert 6000 < stats["valid_pixels"] < 7800
    assert stats["cloud_masked"] is True
    assert 0.4 < stats["clear_share"] < 0.6


def test_without_a_class_map_cloud_is_counted_and_the_result_says_so(scene):
    stats = _compute_stats_from_cogs(scene["red"], scene["nir"], BOX, "ndvi")
    assert stats is not None
    assert stats["cloud_masked"] is False and stats["clear_share"] is None
    assert stats["min"] == pytest.approx(0.0) and stats["max"] == pytest.approx(0.5)
    assert 0.2 < stats["mean"] < 0.3  # half the box at 0.5, half at 0
    assert 12000 < stats["valid_pixels"] < 15000


def test_a_20_m_band_lines_up_with_a_10_m_band(scene):
    stats = _compute_stats_from_cogs(scene["nir"], scene["swir22"], BOX, "nbr")
    assert stats is not None
    # western half (3000 - 1000) / 4000 = 0.5, eastern half (4000 - 1000) / 5000 = 0.6
    assert stats["min"] == pytest.approx(0.5) and stats["max"] == pytest.approx(0.6)
    assert 12000 < stats["valid_pixels"] < 15000


def test_a_box_outside_the_scene_has_no_numbers(scene):
    assert _compute_stats_from_cogs(scene["red"], scene["nir"], [29.0, -2.5, 29.01, -2.49], "ndvi", scene["scl"]) is None


def test_a_box_all_under_cloud_has_no_numbers(scene):
    east_half = [30.4275, -1.6990, 30.4310, -1.6940]
    assert _compute_stats_from_cogs(scene["red"], scene["nir"], east_half, "ndvi", scene["scl"]) is None
    assert _compute_stats_from_cogs(scene["red"], scene["nir"], east_half, "ndvi") is not None


class _Quiet:
    """Stands in for the message Sage's map gets while the layer is added."""

    def __init__(self) -> None:
        self.updates: Dict[str, Any] = {}

    async def __aenter__(self) -> "_Quiet":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


async def _ask(scene: Dict[str, str], bbox: str) -> Dict[str, Any]:
    found = {"items": [{
        "id": "S2B_36MTD_20261119_0_L2A", "datetime": "2026-11-19T08:21:00Z", "cloud_cover": 20.0,
        "assets": {name: {"href": href} for name, href in scene.items()},
    }]}
    args = ComputeSpectralIndexArgs(bbox=bbox, index="ndvi", date_from="2026-11-01", date_to="2026-11-23", layer_name="x")
    with patch.object(STACService, "search_imagery", return_value=found), \
            patch.object(spectral_index, "kue_ephemeral_action", lambda *a, **k: _Quiet()):
        return await compute_spectral_index(args, META)


async def test_the_tool_returns_numbers_for_the_photo_and_warns_when_little_of_it_is_clear(scene):
    result = await _ask(scene, ",".join(str(v) for v in BOX))
    assert result["status"] == "displayed"
    assert result["statistics"]["mean"] == pytest.approx(0.5)
    assert "warning" not in result  # half the box is clear: no warning at exactly the line or above it

    mostly_cloud = "30.4230,-1.6990,30.4310,-1.6940"  # the box's eastern part: under a quarter of it clear
    result = await _ask(scene, mostly_cloud)
    assert result["statistics"]["clear_share"] < spectral_index.MOSTLY_CLEAR
    assert result["warning"].startswith("Only ")
    assert "clear of cloud" in result["warning"]


async def test_the_tool_says_there_is_no_clear_ground_when_the_box_is_all_cloud(scene):
    result = await _ask(scene, "30.4275,-1.6990,30.4310,-1.6940")
    assert result["statistics"] is None
    assert result["warning"] == "No clear ground in this area in this scene, so there are no statistics."
