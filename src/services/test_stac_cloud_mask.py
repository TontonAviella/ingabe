"""NDVI read from Sentinel-2 scenes leaves cloudy pixels out (2026-10-07).

compute_ndvi_for_bbox, behind admin NDVI, SAR->NDVI training, the NDVI-stats fallback and the
search sample, had no cloud mask: on Gasabo, 41-46% cloudy scenes read NDVI 0.14-0.23 where
clear scenes read 0.32-0.36. Each test reads real GeoTIFFs with rasterio: red/NIR at 10 m with
NDVI 0.5 on clear ground and a bright cloud block (NDVI ~0.04), and the 20 m scene classification
band (SCL) marking that block as cloud (9), as Earth Search serves them ("red", "nir", "scl").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from src.services import stac_service
from src.services.stac_service import STACService

UTM_35S = "EPSG:32735"
WEST, NORTH = 790_000.0, 9_782_000.0
WIDTH, HEIGHT = 200, 100  # 10 m pixels: 2 km x 1 km
CLOUD_COLS = slice(100, 200)  # the east half is under cloud


def _wgs84(left: float, bottom: float, right: float, top: float) -> List[float]:
    return list(transform_bounds(UTM_35S, "EPSG:4326", left, bottom, right, top))


SCENE_BBOX = _wgs84(WEST, NORTH - 1000, WEST + 2000, NORTH)
WHOLE_SCENE = _wgs84(WEST + 50, NORTH - 950, WEST + 1950, NORTH - 50)
UNDER_CLOUD = _wgs84(WEST + 1100, NORTH - 900, WEST + 1900, NORTH - 100)


def _write(path: Path, data: np.ndarray, pixel: float) -> str:
    profile = {
        "driver": "GTiff", "dtype": str(data.dtype), "count": 1, "width": data.shape[1], "height": data.shape[0],
        "crs": UTM_35S, "transform": from_origin(WEST, NORTH, pixel, pixel), "nodata": 0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return str(path)


@pytest.fixture
def bands(tmp_path: Path) -> Dict[str, str]:
    red = np.full((HEIGHT, WIDTH), 1000, dtype=np.uint16)
    nir = np.full((HEIGHT, WIDTH), 3000, dtype=np.uint16)  # NDVI 0.5
    red[:, CLOUD_COLS] = 7000
    nir[:, CLOUD_COLS] = 7600  # bright cloud: NDVI ~0.04
    scl = np.full((HEIGHT // 2, WIDTH // 2), 4, dtype=np.uint8)  # 4 = vegetation
    scl[:, 50:100] = 9  # cloud, high probability (20 m pixels)
    return {
        "red": _write(tmp_path / "red.tif", red, 10.0),
        "nir": _write(tmp_path / "nir.tif", nir, 10.0),
        "scl": _write(tmp_path / "scl.tif", scl, 20.0),
    }


def _item(bands: Dict[str, str]) -> Dict[str, Any]:
    """An item as search_imagery returns it, Earth Search band names."""
    cog = "image/tiff; application=geotiff; profile=cloud-optimized"
    return {
        "id": "scene", "datetime": "2026-09-20T08:21:09Z", "cloud_cover": 41.4, "platform": "sentinel-2c",
        "bbox": SCENE_BBOX, "assets": {name: {"href": href, "type": cog} for name, href in bands.items()},
    }


@pytest.fixture
def service(monkeypatch) -> STACService:
    monkeypatch.setattr(stac_service, "_PYSTAC_CLIENT_AVAILABLE", False)
    return STACService("earth_search")


def test_cloudy_pixels_are_left_out(service, bands):
    result = service.compute_ndvi_for_bbox(_item(bands), WHOLE_SCENE)
    assert result["cloud_masked"] is True
    assert result["mean_ndvi"] == pytest.approx(0.5, abs=0.005)
    assert result["masked_pixel_count"] > 0


def test_without_a_classification_band_the_result_says_unmasked(service, bands):
    unmasked = {k: v for k, v in bands.items() if k != "scl"}
    result = service.compute_ndvi_for_bbox(_item(unmasked), WHOLE_SCENE)
    assert result["cloud_masked"] is False
    assert result["masked_pixel_count"] is None
    assert result["mean_ndvi"] < 0.4  # the cloud drags it down: why the mask matters


def test_an_area_entirely_under_cloud_has_no_ndvi(service, bands):
    result = service.compute_ndvi_for_bbox(_item(bands), UNDER_CLOUD)
    assert result["error"] == "Every pixel in the bbox is cloud, shadow, snow or nodata in this scene"
