"""The satellite search's NDVI sample measures the searched area (2026-10-07).

The search tool sampled NDVI only when the first scene had "B04"/"B08" assets, which Earth Search
never has (it names them red/nir), and the sampler read the centre 5 x 5 km of the scene: for a
Rwanda search the first scene's centre was ~150 km from Kigali, outside Rwanda. Each test reads
real GeoTIFFs with rasterio: NDVI 0.5 on the west half of the scene, 0.2 on the east half.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from src.services import stac_service
from src.services.legacy_tool_shim import LEGACY_HANDLERS, LegacyToolContext
from src.services.stac_service import STACService

UTM_35S = "EPSG:32735"  # Earth Search's CRS for Rwanda's western tiles (35M..)
WEST, NORTH, PIXEL = 790_000.0, 9_782_000.0, 10.0  # near 29.6 E, 1.97 S
WIDTH, HEIGHT = 200, 100  # 2 km x 1 km


def _wgs84(left: float, bottom: float, right: float, top: float) -> List[float]:
    return list(transform_bounds(UTM_35S, "EPSG:4326", left, bottom, right, top))


SCENE_BBOX = _wgs84(WEST, NORTH - HEIGHT * PIXEL, WEST + WIDTH * PIXEL, NORTH)
# The halves, 100 m inside their edges so no pixel of the other half is read.
WEST_HALF = _wgs84(WEST + 100, NORTH - 900, WEST + 900, NORTH - 100)
EAST_HALF = _wgs84(WEST + 1100, NORTH - 900, WEST + 1900, NORTH - 100)


@pytest.fixture
def bands(tmp_path: Path) -> Dict[str, str]:
    """Red and NIR GeoTIFFs: NDVI 0.5 on the west half, 0.2 on the east half; 0 is nodata, as in L2A."""
    red = np.full((HEIGHT, WIDTH), 2000, dtype=np.uint16)
    red[:, : WIDTH // 2] = 1000
    nir = np.full((HEIGHT, WIDTH), 3000, dtype=np.uint16)
    profile = {
        "driver": "GTiff", "dtype": "uint16", "count": 1, "width": WIDTH, "height": HEIGHT,
        "crs": UTM_35S, "transform": from_origin(WEST, NORTH, PIXEL, PIXEL), "nodata": 0,
    }
    paths = {}
    for name, data in (("red", red), ("nir", nir)):
        paths[name] = str(tmp_path / f"{name}.tif")
        with rasterio.open(paths[name], "w", **profile) as dst:
            dst.write(data, 1)
    return paths


def _item(item_id: str, scene_bbox: Optional[List[float]], bands: Dict[str, str]) -> Dict[str, Any]:
    """An item as search_imagery returns it (_search_http / _search_pystac), Earth Search band names."""
    cog = "image/tiff; application=geotiff; profile=cloud-optimized"
    return {
        "id": item_id,
        "datetime": "2026-10-05T08:21:09.871000Z",
        "cloud_cover": 1.912325,
        "platform": "sentinel-2c",
        "bbox": scene_bbox,
        "assets": {name: {"href": href, "type": cog} for name, href in bands.items()},
    }


@pytest.fixture
def service(monkeypatch) -> STACService:
    monkeypatch.setattr(stac_service, "_PYSTAC_CLIENT_AVAILABLE", False)  # no catalog needed
    return STACService("earth_search")


@pytest.mark.parametrize(("area", "ndvi"), [(WEST_HALF, 0.5), (EAST_HALF, 0.2)])
def test_the_sample_is_ndvi_of_the_asked_area(service, bands, area, ndvi):
    sample = service.compute_ndvi_sample([_item("scene", SCENE_BBOX, bands)], area)
    assert sample["mean_ndvi"] == pytest.approx(ndvi, abs=0.005)
    assert sample["bbox_share_in_scene"] == 1.0
    assert sample["source_item_id"] == "scene"


def test_the_scene_covering_most_of_the_area_is_read(service, bands):
    west, south, east, north = WEST_HALF
    sliver_bbox = [east - (east - west) * 0.1, south, east + 1.0, north]  # covers 10% of the area
    # The sliver's red and NIR are swapped: reading it would give NDVI -0.5.
    sliver = _item("sliver", sliver_bbox, {"red": bands["nir"], "nir": bands["red"]})

    sample = service.compute_ndvi_sample([sliver, _item("scene", SCENE_BBOX, bands)], WEST_HALF)

    assert sample["source_item_id"] == "scene"
    assert sample["mean_ndvi"] == pytest.approx(0.5, abs=0.005)


def test_no_overlapping_scene_is_an_error(service, bands):
    elsewhere = [30.5, -3.2, 30.6, -3.1]
    items = [_item("elsewhere", elsewhere, bands), _item("no-bbox", None, bands)]
    assert "overlaps" in service.compute_ndvi_sample(items, WEST_HALF)["error"]


def test_an_inverted_bbox_is_an_error(service, bands):
    west, south, east, north = WEST_HALF
    assert "west < east" in service.compute_ndvi_sample([_item("scene", SCENE_BBOX, bands)], [east, south, west, north])["error"]


async def test_the_search_tool_returns_the_sample_of_the_searched_area(service, bands, monkeypatch):
    def search_imagery(bbox, datetime_range, max_cloud_cover, limit):
        # The envelope search_imagery builds, around the one scene the catalog would return.
        return service._wrap_search_results(
            [_item("scene", SCENE_BBOX, bands)], ["sentinel-2-l2a"], bbox, "2026-09-07/2026-10-07", max_cloud_cover
        )

    monkeypatch.setattr(service, "search_imagery", search_imagery)
    monkeypatch.setattr(stac_service, "get_stac_service", lambda: service)
    ctx = LegacyToolContext(
        user_id="user-test", partner_id="partner-test", conversation_id=1, map_id="MTESTAAAAAAA",
        project_id="PTESTBBBBBBB", conn=None, arguments={"bbox": ",".join(str(v) for v in EAST_HALF)},
    )

    result = await LEGACY_HANDLERS["search_satellite_imagery"](ctx)

    assert result["status"] == "success"
    assert result["search_results"]["items"][0]["id"] == "scene"
    assert result["ndvi_sample"]["mean_ndvi"] == pytest.approx(0.2, abs=0.005)
    assert result["ndvi_sample"]["bbox_share_in_scene"] == 1.0
