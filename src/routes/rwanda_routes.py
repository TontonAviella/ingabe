# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""REST API routes for Rwanda agriculture lakehouse.

Endpoints:
  POST /rwanda/bootstrap            - Initialize Rwanda Iceberg tables
  GET  /rwanda/tables               - List Rwanda tables
  GET  /rwanda/parcels              - Query parcels (filterable)
  GET  /rwanda/ndvi/timeseries      - NDVI time-series (H3 or parcel)
  GET  /rwanda/summary/district     - District-level NDVI summary
  GET  /rwanda/grid/h3              - Generate H3 hexagonal grid
  POST /rwanda/ml/anomalies         - Detect NDVI anomalies
  POST /rwanda/ml/classify-multispectral - Multispectral KMeans classification
  POST /rwanda/field/ndvi           - Real-time field NDVI via Sentinel Hub
  POST /rwanda/field/timeseries     - Field NDVI time series via Sentinel Hub
  GET  /rwanda/ml/classifications/latest - Latest cached crop classification
  GET  /rwanda/ml/anomalies/alerts  - Latest cached anomaly alerts
  GET  /rwanda/ml/yield-risk/latest - Latest cached yield risk assessments
  GET  /rwanda/ml/drought/status    - Latest cached drought status
  GET  /rwanda/ml/phenology/stages  - Latest cached crop growth stages
  GET  /rwanda/ndvi/cells           - Cell-level NDVI stats (ADM4)
  GET  /rwanda/ndvi/parcels         - Parcel-level NDVI stats (user-uploaded)
"""

import asyncio
import logging
import os
from typing import Optional

import h3
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.dependencies.session import UserContext, verify_session_required
from src.services.rwanda_lakehouse import get_rwanda_lakehouse_manager

logger = logging.getLogger(__name__)

# DuckDB cache file populated by Dagster scheduled assets (rwanda_assets.py)
_DUCKDB_CACHE_PATH = "/tmp/ingabe_cache/cache.duckdb"

rwanda_router = APIRouter()


@rwanda_router.post(
    "/rwanda/bootstrap",
    operation_id="bootstrap_rwanda_tables",
    status_code=status.HTTP_201_CREATED,
)
async def bootstrap_tables(
    session: UserContext = Depends(verify_session_required),
):
    """Initialize Rwanda Iceberg namespace and core tables.

    Creates parcels, parcel_observations, and h3_ndvi_weekly tables
    if they don't already exist. Idempotent.
    """

    def _bootstrap():
        manager = get_rwanda_lakehouse_manager()
        return manager.bootstrap_tables()

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _bootstrap)
        return {"status": "ok", "tables": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Rwanda bootstrap failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Bootstrap failed: {e}",
        )


@rwanda_router.get(
    "/rwanda/tables",
    operation_id="list_rwanda_tables",
)
async def list_tables(
    session: UserContext = Depends(verify_session_required),
):
    """List all Iceberg tables in the Rwanda namespace."""

    def _list():
        manager = get_rwanda_lakehouse_manager()
        return manager.list_rwanda_tables()

    loop = asyncio.get_running_loop()
    tables = await loop.run_in_executor(None, _list)
    return {"namespace": "rwanda", "tables": tables, "count": len(tables)}


@rwanda_router.get(
    "/rwanda/parcels",
    operation_id="query_rwanda_parcels",
)
async def query_parcels(
    province: Optional[str] = Query(None, description="Filter by province"),
    district: Optional[str] = Query(None, description="Filter by district"),
    crop_type: Optional[str] = Query(None, description="Filter by crop type"),
    limit: int = Query(1000, ge=1, le=10000, description="Max rows"),
    session: UserContext = Depends(verify_session_required),
):
    """Query Rwanda farm parcels with optional filters."""

    def _query():
        manager = get_rwanda_lakehouse_manager()
        return manager.query_parcels(
            province=province,
            district=district,
            crop_type=crop_type,
            limit=limit,
        )

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _query)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Parcel query failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query failed: {e}",
        )


@rwanda_router.get(
    "/rwanda/ndvi/timeseries",
    operation_id="query_rwanda_ndvi_timeseries",
)
async def query_ndvi_timeseries(
    h3_index: Optional[str] = Query(None, description="H3 hex index (resolution 7)"),
    parcel_id: Optional[str] = Query(None, description="Parcel ID"),
    date_from: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    limit: int = Query(5000, ge=1, le=50000, description="Max rows"),
    session: UserContext = Depends(verify_session_required),
):
    """Query NDVI time-series for a specific H3 hex or parcel."""

    def _query():
        manager = get_rwanda_lakehouse_manager()
        return manager.query_ndvi_timeseries(
            h3_index=h3_index,
            parcel_id=parcel_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _query)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("NDVI timeseries query failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query failed: {e}",
        )


@rwanda_router.get(
    "/rwanda/summary/district",
    operation_id="query_rwanda_district_summary",
)
async def query_district_summary(
    province: Optional[str] = Query(None, description="Filter by province"),
    week_start: Optional[str] = Query(None, description="Week start date (YYYY-MM-DD)"),
    session: UserContext = Depends(verify_session_required),
):
    """Get district-level NDVI summary with anomaly detection.

    Joins parcels with H3 NDVI data to aggregate by admin hierarchy.
    """

    def _query():
        manager = get_rwanda_lakehouse_manager()
        return manager.query_district_summary(
            province=province,
            week_start=week_start,
        )

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _query)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("District summary query failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query failed: {e}",
        )


@rwanda_router.get(
    "/rwanda/imagery/search",
    operation_id="search_rwanda_satellite_imagery",
)
async def search_satellite_imagery(
    bbox: Optional[str] = Query(default=None, description="west,south,east,north"),
    datetime_range: Optional[str] = Query(default=None, description="ISO 8601 range: 2024-01-01/2024-06-30"),
    max_cloud_cover: float = Query(default=20.0, ge=0, le=100),
    catalog: str = Query(default="earth_search"),
    limit: int = Query(default=10, ge=1, le=50),
    session: UserContext = Depends(verify_session_required),
):
    """Search STAC catalogs for satellite imagery over Rwanda."""
    from src.services.stac_service import get_stac_service

    parsed_bbox = None
    if bbox:
        try:
            parsed_bbox = [float(x) for x in bbox.split(",")]
            if len(parsed_bbox) != 4:
                raise HTTPException(status_code=400, detail="bbox must have 4 values: west,south,east,north")
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="Invalid bbox format. Expected: west,south,east,north")

    service = get_stac_service(catalog)
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: service.search_imagery(
            bbox=parsed_bbox, datetime_range=datetime_range,
            max_cloud_cover=max_cloud_cover, limit=limit,
        )
    )

    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@rwanda_router.get(
    "/rwanda/imagery/ndvi",
    operation_id="compute_rwanda_imagery_ndvi",
)
async def compute_imagery_ndvi(
    bbox: Optional[str] = Query(default=None, description="west,south,east,north"),
    datetime_range: Optional[str] = Query(default=None, description="ISO 8601 range: 2024-01-01/2024-06-30"),
    max_cloud_cover: float = Query(default=10.0, ge=0, le=100),
    catalog: str = Query(default="earth_search"),
    session: UserContext = Depends(verify_session_required),
):
    """Compute NDVI time-series from satellite imagery over Rwanda.

    This endpoint actually downloads band data and computes NDVI statistics,
    rather than just returning metadata. Response times may be 10-30 seconds
    depending on number of scenes and network conditions.
    """
    from src.services.stac_service import get_stac_service

    parsed_bbox = None
    if bbox:
        try:
            parsed_bbox = [float(x) for x in bbox.split(",")]
            if len(parsed_bbox) != 4:
                raise HTTPException(status_code=400, detail="bbox must have 4 values: west,south,east,north")
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="Invalid bbox format. Expected: west,south,east,north")

    service = get_stac_service(catalog)
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: service.compute_ndvi_timeseries(
            bbox=parsed_bbox,
            datetime_range=datetime_range,
            max_cloud_cover=max_cloud_cover,
        )
    )

    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@rwanda_router.get(
    "/rwanda/grid/h3",
    operation_id="generate_rwanda_h3_grid",
)
async def generate_h3_grid(
    resolution: int = Query(default=7, ge=0, le=15, description="H3 resolution (0-15)"),
    bounds: str = Query(..., description="Bounding box: west,south,east,north"),
    session: UserContext = Depends(verify_session_required),
):
    """Generate H3 hexagonal grid covering specified bounds.

    Returns a GeoJSON FeatureCollection with H3 hexagon geometries.
    Resolution 7 (~5.16 km²) recommended for district-level aggregation.
    Resolution 9 recommended for parcel-level analysis.

    Args:
        resolution: H3 resolution (0-15), default 7
        bounds: Bounding box as "west,south,east,north" (WGS84)

    Returns:
        GeoJSON FeatureCollection with hexagon features containing:
        - h3_index: H3 cell identifier
        - resolution: H3 resolution level
    """

    def _generate():
        try:
            west, south, east, north = [float(x) for x in bounds.split(",")]
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid bounds format. Expected: west,south,east,north",
            )

        # Validate bounds
        if not (-180 <= west <= 180 and -180 <= east <= 180):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Longitude must be between -180 and 180",
            )
        if not (-90 <= south <= 90 and -90 <= north <= 90):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Latitude must be between -90 and 90",
            )
        if west >= east or south >= north:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid bounds: west must be < east, south must be < north",
            )

        # Create boundary polygon for H3
        boundary_polygon = {
            "type": "Polygon",
            "coordinates": [
                [
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]
            ],
        }

        # Generate H3 cells
        hex_ids = h3.geo_to_cells(boundary_polygon, res=resolution)

        # Safety limit to prevent excessive response sizes
        if len(hex_ids) > 50000:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Generated {len(hex_ids)} hexagons (limit: 50,000). "
                "Please reduce bounds or increase resolution number.",
            )

        # Convert to GeoJSON features
        features = []
        for h3_id in hex_ids:
            boundary = h3.cell_to_boundary(h3_id)
            # h3 returns (lat, lng) pairs, GeoJSON needs (lng, lat)
            coords = [[lng, lat] for lat, lng in boundary]
            coords.append(coords[0])  # close polygon

            features.append(
                {
                    "type": "Feature",
                    "properties": {"h3_index": h3_id, "resolution": resolution},
                    "geometry": {"type": "Polygon", "coordinates": [coords]},
                }
            )

        return {"type": "FeatureCollection", "features": features}

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _generate)
        logger.info(
            "Generated H3 grid: resolution=%d, cells=%d",
            resolution,
            len(result["features"]),
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("H3 grid generation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Grid generation failed: {e}",
        )


@rwanda_router.get(
    "/rwanda/ml/status",
    operation_id="rwanda_ml_status",
)
async def ml_status(
    session: UserContext = Depends(verify_session_required),
):
    """Check ML inference service status and available models."""
    from src.services.ml_inference import get_ml_service

    return get_ml_service().get_status()


@rwanda_router.post(
    "/rwanda/ml/classify",
    operation_id="rwanda_ml_classify",
)
async def classify_ndvi(
    data: dict,
    session: UserContext = Depends(verify_session_required),
):
    """Classify land cover from NDVI values.

    Request body:
        {
            "ndvi_values": [0.2, 0.5, 0.7, ...]
        }

    Returns classification by land cover type with percentages.
    """
    from src.services.ml_inference import get_ml_service

    ndvi_values = data.get("ndvi_values", [])
    if not ndvi_values:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ndvi_values list is required",
        )
    return get_ml_service().classify_ndvi(ndvi_values)


@rwanda_router.post(
    "/rwanda/ml/yield-risk",
    operation_id="rwanda_ml_yield_risk",
)
async def predict_yield_risk(
    data: dict,
    session: UserContext = Depends(verify_session_required),
):
    """Predict yield risk from NDVI time series.

    Request body:
        {
            "ndvi_timeseries": [
                {"date": "2024-01-01", "mean_ndvi": 0.5},
                {"date": "2024-01-08", "mean_ndvi": 0.55},
                ...
            ]
        }

    Returns risk level (low/normal/moderate/high/critical) with description.
    """
    from src.services.ml_inference import get_ml_service

    timeseries = data.get("ndvi_timeseries", [])
    if not timeseries:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ndvi_timeseries list is required",
        )
    return get_ml_service().predict_yield_risk(timeseries)


@rwanda_router.get(
    "/rwanda/superset/status",
    operation_id="check_superset_status",
)
async def check_superset_status(
    session: UserContext = Depends(verify_session_required),
):
    """Check if Superset service is reachable."""
    superset_url = os.environ.get("SUPERSET_URL", "http://superset:8088")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{superset_url}/health")
            available = response.status_code == 200
    except Exception as e:
        logger.warning(f"Superset health check failed: {e}")
        available = False

    return {"available": available, "url": superset_url}


@rwanda_router.post(
    "/rwanda/superset/guest-token",
    operation_id="get_superset_guest_token",
)
async def get_superset_guest_token(
    data: dict,
    session: UserContext = Depends(verify_session_required),
):
    """Get a guest token for embedding Superset dashboards.

    Request body:
        {
            "dashboard_id": "abc-123-def"
        }

    Returns guest token for secure dashboard embedding.
    """
    dashboard_id = data.get("dashboard_id")
    if not dashboard_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="dashboard_id is required",
        )

    superset_url = os.environ.get("SUPERSET_URL", "http://superset:8088")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Login to Superset
            login_response = await client.post(
                f"{superset_url}/api/v1/security/login",
                json={
                    "username": "admin",
                    "password": "admin",
                    "provider": "db",
                    "refresh": True,
                },
            )
            login_response.raise_for_status()
            access_token = login_response.json().get("access_token")

            if not access_token:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Failed to obtain Superset access token",
                )

            # Step 2: Request guest token
            guest_token_response = await client.post(
                f"{superset_url}/api/v1/security/guest_token/",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "user": {
                        "username": "guest",
                        "first_name": "Guest",
                        "last_name": "User",
                    },
                    "resources": [{"type": "dashboard", "id": dashboard_id}],
                    "rls": [],
                },
            )
            guest_token_response.raise_for_status()
            return guest_token_response.json()

    except httpx.HTTPStatusError as e:
        logger.error(f"Superset API error: {e.response.status_code} - {e.response.text}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Superset API error: {e.response.status_code}",
        )
    except Exception as e:
        logger.error(f"Failed to get Superset guest token: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to Superset: {str(e)}",
        )


@rwanda_router.get(
    "/rwanda/superset/dashboards",
    operation_id="list_superset_dashboards",
)
async def list_superset_dashboards(
    session: UserContext = Depends(verify_session_required),
):
    """List available Superset dashboards."""
    superset_url = os.environ.get("SUPERSET_URL", "http://superset:8088")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Login to Superset
            login_response = await client.post(
                f"{superset_url}/api/v1/security/login",
                json={
                    "username": "admin",
                    "password": "admin",
                    "provider": "db",
                    "refresh": True,
                },
            )
            login_response.raise_for_status()
            access_token = login_response.json().get("access_token")

            if not access_token:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Failed to obtain Superset access token",
                )

            # Step 2: Get dashboards
            dashboards_response = await client.get(
                f"{superset_url}/api/v1/dashboard/",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            dashboards_response.raise_for_status()
            data = dashboards_response.json()

            # Extract relevant fields
            dashboards = []
            for dash in data.get("result", []):
                dashboards.append({
                    "id": dash.get("id"),
                    "title": dash.get("dashboard_title"),
                    "url": dash.get("url"),
                    "status": dash.get("status"),
                })

            return {"dashboards": dashboards, "count": len(dashboards)}

    except httpx.HTTPStatusError as e:
        logger.error(f"Superset API error: {e.response.status_code} - {e.response.text}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Superset API error: {e.response.status_code}",
        )
    except Exception as e:
        logger.error(f"Failed to list Superset dashboards: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to Superset: {str(e)}",
        )


# ──────────────────────────────────────────────────────────────
# New endpoints: ML inference, Sentinel Hub, DuckDB cache queries
# ──────────────────────────────────────────────────────────────


@rwanda_router.post(
    "/rwanda/ml/anomalies",
    operation_id="rwanda_ml_detect_anomalies",
)
async def detect_anomalies(
    data: dict,
    session: UserContext = Depends(verify_session_required),
):
    """Detect anomalies in NDVI time series using z-score analysis.

    Request body:
        {
            "ndvi_timeseries": [
                {"date": "2024-01-01", "mean_ndvi": 0.5},
                {"date": "2024-01-08", "mean_ndvi": 0.3},
                ...
            ]
        }

    Returns anomaly dates, severity, and deviation from expected.
    """
    from src.services.ml_inference import get_ml_service

    timeseries = data.get("ndvi_timeseries", [])
    if not timeseries:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ndvi_timeseries list is required",
        )
    return get_ml_service().detect_anomalies(timeseries)


@rwanda_router.post(
    "/rwanda/ml/classify-multispectral",
    operation_id="rwanda_ml_classify_multispectral",
)
async def classify_multispectral(
    data: dict,
    session: UserContext = Depends(verify_session_required),
):
    """Classify land cover from multispectral satellite bands using KMeans.

    Request body:
        {
            "bands": {
                "B02": [[...], ...],  // Blue (2D array)
                "B03": [[...], ...],  // Green
                "B04": [[...], ...],  // Red
                "B08": [[...], ...]   // NIR
            }
        }

    Requires scikit-learn. Returns cluster assignments with land cover labels.
    """
    import numpy as np
    from src.services.ml_inference import get_ml_service

    bands_raw = data.get("bands", {})
    if not bands_raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bands dict is required with B03, B04, B08 arrays",
        )

    # Convert lists to numpy arrays
    bands = {}
    try:
        for band_name, values in bands_raw.items():
            bands[band_name] = np.array(values, dtype=np.float32)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid band data: {e}",
        )

    result = await asyncio.get_running_loop().run_in_executor(
        None, lambda: get_ml_service().classify_multispectral(bands)
    )

    if "error" in result:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=result["error"],
        )
    return result


@rwanda_router.post(
    "/rwanda/field/ndvi",
    operation_id="rwanda_field_ndvi",
)
async def field_ndvi(
    data: dict,
    session: UserContext = Depends(verify_session_required),
):
    """Get real-time NDVI statistics for a field polygon via Sentinel Hub.

    Request body:
        {
            "geometry": { GeoJSON Polygon },
            "date_from": "2024-01-01",   // optional, default 30d ago
            "date_to": "2024-06-30",     // optional, default today
            "index": "ndvi"              // or "multi" for NDVI+NDWI+BSI
        }

    Returns per-day statistics (mean, std, min, max, percentiles).
    Requires SH_CLIENT_ID and SH_CLIENT_SECRET environment variables.
    """
    from src.services.sentinel_hub_service import get_sentinel_hub_service

    service = get_sentinel_hub_service()
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sentinel Hub service not available (sentinelhub package not installed)",
        )

    geometry = data.get("geometry")
    if not geometry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GeoJSON geometry is required",
        )

    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: service.get_field_stats(
            geometry=geometry,
            date_from=data.get("date_from"),
            date_to=data.get("date_to"),
            index=data.get("index", "ndvi"),
        ),
    )

    if "error" in result:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result["error"],
        )
    return result


@rwanda_router.post(
    "/rwanda/field/timeseries",
    operation_id="rwanda_field_timeseries",
)
async def field_timeseries(
    data: dict,
    session: UserContext = Depends(verify_session_required),
):
    """Get NDVI time series for a field over N months via Sentinel Hub.

    Request body:
        {
            "geometry": { GeoJSON Polygon },
            "months": 6  // optional, default 6
        }
    """
    from src.services.sentinel_hub_service import get_sentinel_hub_service

    service = get_sentinel_hub_service()
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sentinel Hub service not available",
        )

    geometry = data.get("geometry")
    if not geometry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GeoJSON geometry is required",
        )

    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: service.get_field_timeseries(
            geometry=geometry,
            months=data.get("months", 6),
        ),
    )

    if "error" in result:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result["error"],
        )
    return result


@rwanda_router.get(
    "/rwanda/ml/classifications/latest",
    operation_id="rwanda_ml_classifications_latest",
)
async def get_latest_classifications(
    district: Optional[str] = Query(None, description="Filter by district"),
    limit: int = Query(100, ge=1, le=10000),
    session: UserContext = Depends(verify_session_required),
):
    """Get latest pre-computed crop classifications from DuckDB cache.

    These are populated by Dagster scheduled assets (weekly).
    Returns instantly from local cache — no remote API calls.
    """
    import duckdb

    try:
        conn = duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)

        # Create table if not exists (first run before Dagster populates)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crop_classification_cache (
                district VARCHAR,
                class_label VARCHAR,
                area_ha DOUBLE,
                pixel_count INTEGER,
                confidence DOUBLE,
                job_id VARCHAR,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Build query with optional district filter
        where_clauses = []
        params = []
        if district:
            where_clauses.append("district = ?")
            params.append(district)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT district, class_label, area_ha, pixel_count, confidence,
                   job_id, computed_at
            FROM crop_classification_cache
            {where_sql}
            ORDER BY computed_at DESC
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        if not rows:
            return {
                "source": "duckdb_cache",
                "status": "awaiting_dagster_population",
                "message": "Classification cache will be populated by weekly Dagster schedule",
                "district_filter": district,
                "results": [],
            }

        results = [
            {
                "district": r[0],
                "class_label": r[1],
                "area_ha": r[2],
                "pixel_count": r[3],
                "confidence": r[4],
                "job_id": r[5],
                "computed_at": str(r[6]) if r[6] else None,
            }
            for r in rows
        ]

        return {
            "source": "duckdb_cache",
            "status": "ok",
            "district_filter": district,
            "count": len(results),
            "results": results,
        }
    except Exception as e:
        logger.error("DuckDB classification query failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cache query failed: {e}",
        )


@rwanda_router.get(
    "/rwanda/ml/anomalies/alerts",
    operation_id="rwanda_ml_anomaly_alerts",
)
async def get_anomaly_alerts(
    severity: Optional[str] = Query(None, description="Filter: high or moderate"),
    district: Optional[str] = Query(None, description="Filter by district"),
    limit: int = Query(50, ge=1, le=1000),
    session: UserContext = Depends(verify_session_required),
):
    """Get latest anomaly alerts from DuckDB cache.

    Pre-computed by Dagster weekly anomaly scan.
    Returns instantly from local cache.
    """
    import duckdb

    try:
        conn = duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS anomaly_alerts_cache (
                district VARCHAR,
                h3_index VARCHAR,
                parcel_id VARCHAR,
                anomaly_date DATE,
                observed_ndvi DOUBLE,
                expected_ndvi DOUBLE,
                z_score DOUBLE,
                severity VARCHAR,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        where_clauses = []
        params = []
        if severity:
            where_clauses.append("severity = ?")
            params.append(severity)
        if district:
            where_clauses.append("district = ?")
            params.append(district)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT district, h3_index, parcel_id, anomaly_date,
                   observed_ndvi, expected_ndvi, z_score, severity, computed_at
            FROM anomaly_alerts_cache
            {where_sql}
            ORDER BY z_score ASC, computed_at DESC
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        if not rows:
            return {
                "source": "duckdb_cache",
                "status": "awaiting_dagster_population",
                "message": "Anomaly alerts will be populated by weekly Dagster schedule",
                "severity_filter": severity,
                "district_filter": district,
                "alerts": [],
            }

        alerts = [
            {
                "district": r[0],
                "h3_index": r[1],
                "parcel_id": r[2],
                "anomaly_date": str(r[3]) if r[3] else None,
                "observed_ndvi": r[4],
                "expected_ndvi": r[5],
                "z_score": round(r[6], 3) if r[6] else None,
                "severity": r[7],
                "computed_at": str(r[8]) if r[8] else None,
            }
            for r in rows
        ]

        return {
            "source": "duckdb_cache",
            "status": "ok",
            "severity_filter": severity,
            "district_filter": district,
            "count": len(alerts),
            "alerts": alerts,
        }
    except Exception as e:
        logger.error("DuckDB anomaly alert query failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cache query failed: {e}",
        )


@rwanda_router.get(
    "/rwanda/ml/yield-risk/latest",
    operation_id="rwanda_ml_yield_risk_latest",
)
async def get_yield_risk_latest(
    district: Optional[str] = Query(None, description="Filter by district"),
    limit: int = Query(50, ge=1, le=1000),
    session: UserContext = Depends(verify_session_required),
):
    """Get latest yield risk assessments from DuckDB cache.

    Pre-computed by Dagster weekly yield risk job (Mann-Kendall trend analysis).
    Returns instantly from local cache.
    """
    import duckdb

    try:
        conn = duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS yield_risk_cache (
                district VARCHAR,
                risk_level VARCHAR,
                risk_description VARCHAR,
                trend_slope DOUBLE,
                kendall_tau DOUBLE,
                latest_ndvi DOUBLE,
                mean_ndvi DOUBLE,
                seasonal_deviation DOUBLE,
                observations INTEGER,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        where_clauses = []
        params = []
        if district:
            where_clauses.append("district = ?")
            params.append(district)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT district, risk_level, risk_description, trend_slope,
                   kendall_tau, latest_ndvi, mean_ndvi, seasonal_deviation,
                   observations, computed_at
            FROM yield_risk_cache
            {where_sql}
            ORDER BY risk_level DESC, computed_at DESC
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        if not rows:
            return {
                "source": "duckdb_cache",
                "status": "awaiting_dagster_population",
                "message": "Yield risk cache will be populated by weekly Dagster schedule",
                "district_filter": district,
                "assessments": [],
            }

        assessments = [
            {
                "district": r[0],
                "risk_level": r[1],
                "risk_description": r[2],
                "trend_slope": round(r[3], 6) if r[3] else None,
                "kendall_tau": round(r[4], 4) if r[4] else None,
                "latest_ndvi": round(r[5], 4) if r[5] else None,
                "mean_ndvi": round(r[6], 4) if r[6] else None,
                "seasonal_deviation": round(r[7], 4) if r[7] else None,
                "observations": r[8],
                "computed_at": str(r[9]) if r[9] else None,
            }
            for r in rows
        ]

        return {
            "source": "duckdb_cache",
            "status": "ok",
            "district_filter": district,
            "count": len(assessments),
            "assessments": assessments,
        }
    except Exception as e:
        logger.error("DuckDB yield risk query failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cache query failed: {e}",
        )


@rwanda_router.get(
    "/rwanda/ml/drought/status",
    operation_id="rwanda_ml_drought_status",
)
async def get_drought_status(
    district: Optional[str] = Query(None, description="Filter by district"),
    drought_status: Optional[str] = Query(
        None,
        alias="status",
        description="Filter: severe_drought, moderate_drought, watch, normal",
    ),
    limit: int = Query(50, ge=1, le=1000),
    session: UserContext = Depends(verify_session_required),
):
    """Get latest drought status from DuckDB cache.

    Pre-computed by Dagster weekly drought scan (VCI + NDWI analysis).
    Returns instantly from local cache.
    """
    import duckdb

    try:
        conn = duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS drought_cache (
                district VARCHAR,
                drought_status VARCHAR,
                current_vci DOUBLE,
                latest_ndvi DOUBLE,
                latest_ndwi DOUBLE,
                drought_period_count INTEGER,
                description VARCHAR,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        where_clauses = []
        params = []
        if district:
            where_clauses.append("district = ?")
            params.append(district)
        if drought_status:
            where_clauses.append("drought_status = ?")
            params.append(drought_status)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT district, drought_status, current_vci, latest_ndvi,
                   latest_ndwi, drought_period_count, description, computed_at
            FROM drought_cache
            {where_sql}
            ORDER BY current_vci ASC, computed_at DESC
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        if not rows:
            return {
                "source": "duckdb_cache",
                "status": "awaiting_dagster_population",
                "message": "Drought cache will be populated by weekly Dagster schedule",
                "district_filter": district,
                "status_filter": drought_status,
                "districts": [],
            }

        districts = [
            {
                "district": r[0],
                "drought_status": r[1],
                "current_vci": round(r[2], 2) if r[2] else None,
                "latest_ndvi": round(r[3], 4) if r[3] else None,
                "latest_ndwi": round(r[4], 4) if r[4] else None,
                "drought_period_count": r[5],
                "description": r[6],
                "computed_at": str(r[7]) if r[7] else None,
            }
            for r in rows
        ]

        return {
            "source": "duckdb_cache",
            "status": "ok",
            "district_filter": district,
            "status_filter": drought_status,
            "count": len(districts),
            "districts": districts,
        }
    except Exception as e:
        logger.error("DuckDB drought status query failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cache query failed: {e}",
        )


@rwanda_router.get(
    "/rwanda/ml/phenology/stages",
    operation_id="rwanda_ml_phenology_stages",
)
async def get_phenology_stages(
    district: Optional[str] = Query(None, description="Filter by district"),
    stage: Optional[str] = Query(
        None, description="Filter: dormant, green_up, peak, senescence, harvest, stable"
    ),
    limit: int = Query(50, ge=1, le=1000),
    session: UserContext = Depends(verify_session_required),
):
    """Get latest crop growth stage analysis from DuckDB cache.

    Pre-computed by Dagster weekly phenology job (NDVI curve analysis).
    Returns instantly from local cache.
    """
    import duckdb

    try:
        conn = duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS phenology_cache (
                district VARCHAR,
                current_stage VARCHAR,
                peak_ndvi DOUBLE,
                peak_date DATE,
                green_up_start DATE,
                senescence_start DATE,
                harvest_date DATE,
                observations INTEGER,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        where_clauses = []
        params = []
        if district:
            where_clauses.append("district = ?")
            params.append(district)
        if stage:
            where_clauses.append("current_stage = ?")
            params.append(stage)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT district, current_stage, peak_ndvi, peak_date,
                   green_up_start, senescence_start, harvest_date,
                   observations, computed_at
            FROM phenology_cache
            {where_sql}
            ORDER BY computed_at DESC
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        if not rows:
            return {
                "source": "duckdb_cache",
                "status": "awaiting_dagster_population",
                "message": "Phenology cache will be populated by weekly Dagster schedule",
                "district_filter": district,
                "stage_filter": stage,
                "districts": [],
            }

        districts = [
            {
                "district": r[0],
                "current_stage": r[1],
                "peak_ndvi": round(r[2], 4) if r[2] else None,
                "peak_date": str(r[3]) if r[3] else None,
                "green_up_start": str(r[4]) if r[4] else None,
                "senescence_start": str(r[5]) if r[5] else None,
                "harvest_date": str(r[6]) if r[6] else None,
                "observations": r[7],
                "computed_at": str(r[8]) if r[8] else None,
            }
            for r in rows
        ]

        return {
            "source": "duckdb_cache",
            "status": "ok",
            "district_filter": district,
            "stage_filter": stage,
            "count": len(districts),
            "districts": districts,
        }
    except Exception as e:
        logger.error("DuckDB phenology query failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cache query failed: {e}",
        )


# ─── Weather data endpoints ──────────────────────────────────────────────


@rwanda_router.get(
    "/rwanda/weather/daily",
    operation_id="get_rwanda_weather_daily",
)
async def get_weather_daily(
    district: Optional[str] = Query(None, description="Filter by district name"),
    date_from: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    limit: int = Query(200, ge=1, le=10000),
    session: UserContext = Depends(verify_session_required),
):
    """Get daily weather statistics from DuckDB cache.

    Pre-computed by Dagster daily_weather_ingest from Copernicus AgERA5.
    Returns temperature (mean/max/min C), precipitation (mm/day),
    and solar radiation (MJ/m2/day) per district per day.
    """
    import duckdb

    try:
        conn = duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS weather_daily_cache (
                district VARCHAR,
                observation_date DATE,
                temperature_mean DOUBLE,
                temperature_max DOUBLE,
                temperature_min DOUBLE,
                precipitation DOUBLE,
                solar_radiation DOUBLE,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        where_clauses = []
        params: list = []
        if district:
            where_clauses.append("district = ?")
            params.append(district)
        if date_from:
            where_clauses.append("observation_date >= ?")
            params.append(date_from)
        if date_to:
            where_clauses.append("observation_date <= ?")
            params.append(date_to)
        if not date_from and not date_to:
            where_clauses.append(
                "observation_date >= CURRENT_DATE - INTERVAL '30 days'"
            )

        where_sql = (
            f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        )
        query = f"""
            SELECT district, observation_date, temperature_mean,
                   temperature_max, temperature_min, precipitation,
                   solar_radiation, computed_at
            FROM weather_daily_cache
            {where_sql}
            ORDER BY observation_date DESC, district
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        if not rows:
            return {
                "source": "duckdb_cache",
                "data_source": "Copernicus AgERA5",
                "status": "awaiting_dagster_population",
                "message": (
                    "Weather cache will be populated by daily_weather_ingest Dagster asset. "
                    "Ensure CDSAPI_KEY env var is set."
                ),
                "district_filter": district,
                "observations": [],
            }

        observations = [
            {
                "district": r[0],
                "date": str(r[1]) if r[1] else None,
                "temperature_mean_c": round(r[2], 1) if r[2] else None,
                "temperature_max_c": round(r[3], 1) if r[3] else None,
                "temperature_min_c": round(r[4], 1) if r[4] else None,
                "precipitation_mm_day": round(r[5], 1) if r[5] else None,
                "solar_radiation_mj_m2_day": round(r[6], 2) if r[6] else None,
                "computed_at": str(r[7]) if r[7] else None,
            }
            for r in rows
        ]

        return {
            "source": "duckdb_cache",
            "data_source": "Copernicus AgERA5 (sis-agrometeorological-indicators)",
            "status": "ok",
            "district_filter": district,
            "date_range": f"{date_from or 'last_30d'}/{date_to or 'now'}",
            "count": len(observations),
            "observations": observations,
        }
    except Exception as e:
        logger.error("DuckDB weather query failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cache query failed: {e}",
        )


# ─── Cell-level and parcel-level NDVI endpoints ──────────────────────────


@rwanda_router.get(
    "/rwanda/ndvi/cells",
    operation_id="get_cell_ndvi_stats",
)
async def get_cell_ndvi_stats(
    cell_name: Optional[str] = Query(None, description="Filter by cell name"),
    district: Optional[str] = Query(None, description="Filter by district name"),
    limit: int = Query(100, ge=1, le=5000),
    session: UserContext = Depends(verify_session_required),
):
    """Get cell-level (ADM4) NDVI statistics from DuckDB cache.

    Pre-computed nightly by Dagster from Sentinel Hub at ~12 km² resolution.
    Returns instantly from local cache.
    """
    import duckdb

    try:
        conn = duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ndvi_cell_cache (
                cell_name VARCHAR,
                district_name VARCHAR,
                week_start DATE,
                mean_ndvi DOUBLE,
                std_ndvi DOUBLE,
                min_ndvi DOUBLE,
                max_ndvi DOUBLE,
                valid_pixels INTEGER,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        where_clauses = []
        params: list = []
        if cell_name:
            where_clauses.append("cell_name ILIKE ?")
            params.append(f"%{cell_name}%")
        if district:
            where_clauses.append("district_name ILIKE ?")
            params.append(f"%{district}%")

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT cell_name, district_name, week_start,
                   mean_ndvi, std_ndvi, min_ndvi, max_ndvi,
                   valid_pixels, computed_at
            FROM ndvi_cell_cache
            {where_sql}
            ORDER BY computed_at DESC, cell_name
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        if not rows:
            return {
                "source": "duckdb_cache",
                "status": "awaiting_dagster_population",
                "message": "Cell NDVI cache will be populated by nightly Dagster schedule",
                "cell_filter": cell_name,
                "district_filter": district,
                "cells": [],
            }

        cells = [
            {
                "cell_name": r[0],
                "district_name": r[1],
                "week_start": str(r[2]) if r[2] else None,
                "mean_ndvi": round(r[3], 4) if r[3] else None,
                "std_ndvi": round(r[4], 4) if r[4] else None,
                "min_ndvi": round(r[5], 4) if r[5] else None,
                "max_ndvi": round(r[6], 4) if r[6] else None,
                "valid_pixels": r[7],
                "computed_at": str(r[8]) if r[8] else None,
            }
            for r in rows
        ]

        return {
            "source": "duckdb_cache",
            "status": "ok",
            "cell_filter": cell_name,
            "district_filter": district,
            "count": len(cells),
            "cells": cells,
        }
    except Exception as e:
        logger.error("DuckDB cell NDVI query failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cache query failed: {e}",
        )


@rwanda_router.get(
    "/rwanda/ndvi/parcels",
    operation_id="get_parcel_ndvi_stats",
)
async def get_parcel_ndvi_stats(
    parcel_name: Optional[str] = Query(None, description="Filter by parcel name"),
    layer_id: Optional[str] = Query(None, description="Filter by source layer ID"),
    limit: int = Query(100, ge=1, le=5000),
    session: UserContext = Depends(verify_session_required),
):
    """Get parcel-level NDVI statistics from DuckDB cache.

    Pre-computed nightly by Dagster from Sentinel Hub at 10m native resolution
    for user-uploaded field boundaries tagged as rwanda_parcels.
    Returns instantly from local cache.
    """
    import duckdb

    try:
        conn = duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ndvi_parcel_cache (
                parcel_id VARCHAR,
                parcel_name VARCHAR,
                layer_id VARCHAR,
                week_start DATE,
                mean_ndvi DOUBLE,
                std_ndvi DOUBLE,
                min_ndvi DOUBLE,
                max_ndvi DOUBLE,
                valid_pixels INTEGER,
                area_ha DOUBLE,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        where_clauses = []
        params: list = []
        if parcel_name:
            where_clauses.append("parcel_name ILIKE ?")
            params.append(f"%{parcel_name}%")
        if layer_id:
            where_clauses.append("layer_id = ?")
            params.append(layer_id)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT parcel_id, parcel_name, layer_id, week_start,
                   mean_ndvi, std_ndvi, min_ndvi, max_ndvi,
                   valid_pixels, area_ha, computed_at
            FROM ndvi_parcel_cache
            {where_sql}
            ORDER BY computed_at DESC, parcel_name
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        if not rows:
            return {
                "source": "duckdb_cache",
                "status": "awaiting_data",
                "message": (
                    "No parcel NDVI data yet. Upload field boundaries through Mundi UI "
                    "and tag them with rwanda_parcels=true in layer metadata. "
                    "The nightly Dagster pipeline will pick them up automatically."
                ),
                "parcel_filter": parcel_name,
                "layer_filter": layer_id,
                "parcels": [],
            }

        parcels = [
            {
                "parcel_id": r[0],
                "parcel_name": r[1],
                "layer_id": r[2],
                "week_start": str(r[3]) if r[3] else None,
                "mean_ndvi": round(r[4], 4) if r[4] else None,
                "std_ndvi": round(r[5], 4) if r[5] else None,
                "min_ndvi": round(r[6], 4) if r[6] else None,
                "max_ndvi": round(r[7], 4) if r[7] else None,
                "valid_pixels": r[8],
                "area_ha": r[9],
                "computed_at": str(r[10]) if r[10] else None,
            }
            for r in rows
        ]

        return {
            "source": "duckdb_cache",
            "status": "ok",
            "parcel_filter": parcel_name,
            "layer_filter": layer_id,
            "count": len(parcels),
            "parcels": parcels,
        }
    except Exception as e:
        logger.error("DuckDB parcel NDVI query failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cache query failed: {e}",
        )
