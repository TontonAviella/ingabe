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


def _band(path: Path, pixel_m: int, value_west: int, value_east: Optional[int] = None, dtype: str = "uint16") -> str:
    """A band file covering BOX with 300 m to spare. West of one line it holds value_west, east of it value_east;
    the line lies a whole number of 20 m from the file's left edge, so 10 m and 20 m files split at the same place."""
    west, south, east, north = transform_bounds("EPSG:4326", "EPSG:32736", *BOX)
    left, top = west - 300, north + 300
    width = int((east - west + 600) // pixel_m) + 1
    height = int((north - south + 600) // pixel_m) + 1
    data = np.full((height, width), value_west, dtype=dtype)
    if value_east is not None:
        split_m = 20 * int((east - west + 600) // 40)  # metres from the left edge, about the middle
        data[:, split_m // pixel_m:] = value_east
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
    assert 6600 < stats["valid_pixels"] < 7200
    assert stats["cloud_masked"] is True
    assert 0.47 < stats["clear_share"] < 0.53


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


WEST_PART = "30.4180,-1.6990,30.4230,-1.6940"  # all clear
MOSTLY_CLOUD = "30.4225,-1.6990,30.4310,-1.6940"  # the eastern part: about a quarter of it clear
ALL_CLOUD = "30.4275,-1.6990,30.4310,-1.6940"


async def test_the_tool_returns_numbers_and_warns_when_little_of_the_area_is_clear(scene):
    result = await _ask(scene, WEST_PART)
    assert result["status"] == "displayed"
    assert result["statistics"]["mean"] == pytest.approx(0.5)
    assert result["statistics"]["clear_share"] == pytest.approx(1.0, abs=0.02)
    assert "warning" not in result

    result = await _ask(scene, MOSTLY_CLOUD)
    assert 0.1 < result["statistics"]["clear_share"] < 0.4
    assert result["statistics"]["mean"] == pytest.approx(0.5)
    assert result["warning"].startswith(f"Only {result['statistics']['clear_share']:.0%} of this area was clear ground")


async def test_the_tool_says_there_is_no_clear_ground_when_the_box_is_all_cloud(scene):
    result = await _ask(scene, ALL_CLOUD)
    assert result["statistics"] is None
    assert result["warning"].startswith("No clear ground in this area in this scene")


async def test_a_read_that_fails_is_not_reported_as_cloud(scene):
    result = await _ask({**scene, "red": scene["red"] + ".missing"}, WEST_PART)
    assert result["statistics"] is None
    assert result["warning"] == "The scene's files could not be read, so there are no statistics."


async def test_without_a_class_map_the_tool_says_cloud_was_not_left_out(scene):
    result = await _ask({k: v for k, v in scene.items() if k != "scl"}, WEST_PART)
    assert result["statistics"]["cloud_masked"] is False
    assert result["warning"] == "Cloud could not be left out of these statistics in this scene."


def test_a_box_reaching_past_the_scene_counts_the_missing_ground_as_not_clear(scene):
    # the file reaches 300 m past the box; this box reaches about 1,100 m further west than the file does
    wider = [BOX[0] - 0.0126, BOX[1], BOX[0] + 0.004, BOX[3]]
    stats = _compute_stats_from_cogs(scene["red"], scene["nir"], wider, "ndvi", scene["scl"])
    assert stats is not None and stats["mean"] == pytest.approx(0.5)
    assert 0.3 < stats["clear_share"] < 0.5  # about 745 m of the box's 1,850 m lie inside the file


@pytest.mark.parametrize("bbox", [
    "30.43,-1.70,30.41,-1.69",  # west and east swapped
    "30.41,-1.69,30.43,-1.70",  # south and north swapped
    "30.41,-1.70,nan,-1.69",
    "30.41,-91,30.43,-1.69",
])
async def test_a_box_that_is_not_a_box_is_refused_before_any_search(bbox):
    args = ComputeSpectralIndexArgs(bbox=bbox, index="ndvi", date_from="2026-11-01", date_to="2026-11-23", layer_name="x")
    with patch.object(STACService, "search_imagery", side_effect=AssertionError("searched")):
        result = await compute_spectral_index(args, META)
    assert result["status"] == "error" and result["error"].startswith("bbox must be west,south,east,north")
