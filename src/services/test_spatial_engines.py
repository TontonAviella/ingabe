from __future__ import annotations

import asyncio

from src.services.spatial_engines import get_spatial_engine_capabilities


def test_spatial_engine_capabilities_reports_geoai_and_geolibre_status() -> None:
    status = asyncio.run(
        get_spatial_engine_capabilities(
            include_rasterd=False,
            include_geokernel=False,
            include_whitebox=False,
            include_tessera=False,
        )
    )

    assert "samgeo_segmentation" in status
    assert "geolibre_wasm" in status
    assert status["samgeo_segmentation"]["note"]
    assert status["geolibre_wasm"]["note"]
    assert "installed" in status["samgeo_segmentation"]
    assert "installed" in status["geolibre_wasm"]
