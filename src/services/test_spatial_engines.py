from __future__ import annotations

import asyncio

from src.services.spatial_engines import get_spatial_engine_capabilities


def test_spatial_engine_capabilities_reports_geolibre_status_without_canceled_models() -> None:
    status = asyncio.run(
        get_spatial_engine_capabilities(
            include_rasterd=False,
            include_geokernel=False,
            include_whitebox=False,
            include_tessera=False,
        )
    )

    assert "geolibre_wasm" in status
    assert status["geolibre_wasm"]["note"]
    assert "installed" in status["geolibre_wasm"]
    assert "fastsam" in status
    assert "ready" in status["fastsam"]
    assert status["segment_geospatial"]["active_for"] == []
    assert "deliberately excluded" in status["segment_geospatial"]["decision"]
