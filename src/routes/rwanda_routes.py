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
import re
from typing import Optional

import h3
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from src.dependencies.session import UserContext, verify_session_required
from src.services.rwanda_lakehouse import get_rwanda_lakehouse_manager

logger = logging.getLogger(__name__)

rwanda_router = APIRouter()


# ── Admin-level bbox lookup ──────────────────────────────────────────────
# Looks up pre-computed bounding boxes from PostGIS boundary tables.
# Supports district (ADM2), sector (ADM3), and cell (ADM4) levels.
# Returns [west, south, east, north] or None if not found.

async def _lookup_admin_bbox(
    district: Optional[str] = None,
    sector: Optional[str] = None,
    cell: Optional[str] = None,
) -> Optional[list[float]]:
    """Look up a bounding box from Rwanda admin boundary tables in PostGIS.

    Priority: cell > sector > district (most specific wins).
    Returns [west, south, east, north] in WGS84, or None if not found.
    """
    from src.structures import get_async_db_connection

    if not (district or sector or cell):
        return None

    async with get_async_db_connection() as conn:
        try:
            if cell:
                row = await conn.fetchrow(
                    "SELECT bbox_west, bbox_south, bbox_east, bbox_north "
                    "FROM rwanda_cell_boundaries WHERE LOWER(cell_name) = LOWER($1) LIMIT 1",
                    cell,
                )
                if row:
                    return [row[0], row[1], row[2], row[3]]

            if sector:
                try:
                    row = await conn.fetchrow(
                        "SELECT bbox_west, bbox_south, bbox_east, bbox_north "
                        "FROM rwanda_sector_boundaries WHERE LOWER(sector_name) = LOWER($1) LIMIT 1",
                        sector,
                    )
                    if row:
                        return [row[0], row[1], row[2], row[3]]
                except Exception:
                    try:
                        row = await conn.fetchrow(
                            "SELECT MIN(bbox_west), MIN(bbox_south), MAX(bbox_east), MAX(bbox_north) "
                            "FROM rwanda_cell_boundaries WHERE LOWER(sector_name) = LOWER($1)",
                            sector,
                        )
                        if row and row[0] is not None:
                            return [row[0], row[1], row[2], row[3]]
                    except Exception:
                        pass

            if district:
                row = await conn.fetchrow(
                    "SELECT bbox_west, bbox_south, bbox_east, bbox_north "
                    "FROM rwanda_district_boundaries WHERE LOWER(district) = LOWER($1) LIMIT 1",
                    district,
                )
                if row:
                    return [row[0], row[1], row[2], row[3]]

        except Exception as e:
            logger.warning("Admin bbox lookup failed: %s", e)

    return None


async def _lookup_admin_geometry(
    district: Optional[str] = None,
    sector: Optional[str] = None,
    cell: Optional[str] = None,
) -> Optional[dict]:
    """Look up a GeoJSON geometry from Rwanda admin boundary tables in PostGIS.

    Delegates to the shared admin_boundaries service which handles caching,
    read-replica routing, and sector fallback via cell union.
    """
    from src.services.admin_boundaries import lookup_admin_geometry
    return await lookup_admin_geometry(district=district, sector=sector, cell=cell)


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
    district: Optional[str] = Query(default=None, description="Rwanda district name (ADM2) — auto-resolves bbox"),
    sector: Optional[str] = Query(default=None, description="Rwanda sector name (ADM3) — auto-resolves bbox"),
    cell: Optional[str] = Query(default=None, description="Rwanda cell name (ADM4) — auto-resolves bbox"),
    datetime_range: Optional[str] = Query(default=None, description="ISO 8601 range: 2024-01-01/2024-06-30"),
    max_cloud_cover: float = Query(default=20.0, ge=0, le=100),
    catalog: str = Query(default="earth_search"),
    limit: int = Query(default=10, ge=1, le=50),
    session: UserContext = Depends(verify_session_required),
):
    """Search STAC catalogs for satellite imagery over Rwanda.

    Spatial filtering: provide `bbox` directly, OR use `district`/`sector`/`cell`
    to automatically resolve the bounding box from PostGIS admin boundaries.
    Most specific wins: cell > sector > district.
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
    elif district or sector or cell:
        # Auto-resolve bbox from admin boundary tables
        parsed_bbox = await _lookup_admin_bbox(district=district, sector=sector, cell=cell)
        if parsed_bbox is None:
            name = cell or sector or district
            raise HTTPException(
                status_code=404,
                detail=f"Admin area '{name}' not found in boundary tables. "
                "Run the Dagster rwanda_admin_boundaries asset first.",
            )

    service = get_stac_service(catalog)
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: service.search_imagery(
            bbox=parsed_bbox, datetime_range=datetime_range,
            collections=None,  # Uses catalog default (Sentinel-2 L2A)
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
    district: Optional[str] = Query(default=None, description="Rwanda district name (ADM2) — auto-resolves bbox"),
    sector: Optional[str] = Query(default=None, description="Rwanda sector name (ADM3) — auto-resolves bbox"),
    cell: Optional[str] = Query(default=None, description="Rwanda cell name (ADM4) — auto-resolves bbox"),
    datetime_range: Optional[str] = Query(default=None, description="ISO 8601 range: 2024-01-01/2024-06-30"),
    max_cloud_cover: float = Query(default=10.0, ge=0, le=100),
    catalog: str = Query(default="earth_search"),
    session: UserContext = Depends(verify_session_required),
):
    """Compute NDVI time-series from satellite imagery over Rwanda.

    This endpoint actually downloads band data and computes NDVI statistics.
    Provide `bbox` directly, OR use `district`/`sector`/`cell` to auto-resolve.
    Response times may be 10-30 seconds depending on scenes and network.
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
    elif district or sector or cell:
        parsed_bbox = await _lookup_admin_bbox(district=district, sector=sector, cell=cell)
        if parsed_bbox is None:
            name = cell or sector or district
            raise HTTPException(
                status_code=404,
                detail=f"Admin area '{name}' not found in boundary tables.",
            )

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
            "geometry": { GeoJSON Polygon },   // OR use district/sector/cell below
            "district": "Kigali",              // auto-resolves geometry from PostGIS
            "sector": "Nyarugenge",            // more specific than district
            "cell": "Muhima",                  // most specific (ADM4)
            "date_from": "2024-01-01",         // optional, default 30d ago
            "date_to": "2024-06-30",           // optional, default today
            "index": "ndvi",                   // or "multi" for NDVI+NDWI+BSI
            "collection": "SENTINEL2_L2A"      // or "sentinel-2-l1c", "s1", etc.
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

    # Auto-resolve geometry from admin boundary tables if not provided directly
    if not geometry:
        admin_district = data.get("district")
        admin_sector = data.get("sector")
        admin_cell = data.get("cell")

        if admin_district or admin_sector or admin_cell:
            geometry = await _lookup_admin_geometry(
                district=admin_district, sector=admin_sector, cell=admin_cell,
            )
            if geometry is None:
                name = admin_cell or admin_sector or admin_district
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Admin area '{name}' not found in boundary tables.",
                )

    if not geometry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GeoJSON geometry OR district/sector/cell name is required",
        )

    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: service.get_field_stats(
            geometry=geometry,
            date_from=data.get("date_from"),
            date_to=data.get("date_to"),
            index=data.get("index", "ndvi"),
            collection=data.get("collection"),
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
            "geometry": { GeoJSON Polygon },  // OR use district/sector/cell
            "district": "Kigali",
            "sector": "Nyarugenge",
            "cell": "Muhima",
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

    # Auto-resolve geometry from admin boundary tables if not provided
    if not geometry:
        admin_district = data.get("district")
        admin_sector = data.get("sector")
        admin_cell = data.get("cell")

        if admin_district or admin_sector or admin_cell:
            geometry = await _lookup_admin_geometry(
                district=admin_district, sector=admin_sector, cell=admin_cell,
            )
            if geometry is None:
                name = admin_cell or admin_sector or admin_district
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Admin area '{name}' not found in boundary tables.",
                )

    if not geometry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GeoJSON geometry OR district/sector/cell name is required",
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
    """Get latest pre-computed crop classifications from PostgreSQL cache.

    These are populated by Dagster scheduled assets (weekly).
    Returns instantly from local cache — no remote API calls.
    """
    from src.structures import get_async_db_connection

    try:
        async with get_async_db_connection() as pg_conn:
            # Build query with optional district filter
            where_clauses = []
            params = []
            param_idx = 1
            if district:
                where_clauses.append(f"district = ${param_idx}")
                params.append(district)
                param_idx += 1

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            query = f"""
                SELECT district, class_label, area_ha, pixel_count, confidence,
                       job_id, computed_at
                FROM crop_classification_cache
                {where_sql}
                ORDER BY computed_at DESC
                LIMIT ${param_idx}
            """
            params.append(limit)

            rows = await pg_conn.fetch(query, *params)

        if not rows:
            return {
                "source": "postgres_cache",
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
            "source": "postgres_cache",
            "status": "ok",
            "district_filter": district,
            "count": len(results),
            "results": results,
        }
    except Exception as e:
        logger.error("PostgreSQL classification query failed: %s", e)
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
    """Get latest anomaly alerts from PostgreSQL cache.

    Pre-computed by Dagster weekly anomaly scan.
    Returns instantly from local cache.
    """
    from src.structures import get_async_db_connection

    try:
        async with get_async_db_connection() as pg_conn:
            where_clauses = []
            params = []
            param_idx = 1
            if severity:
                where_clauses.append(f"severity = ${param_idx}")
                params.append(severity)
                param_idx += 1
            if district:
                where_clauses.append(f"district = ${param_idx}")
                params.append(district)
                param_idx += 1

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            query = f"""
                SELECT district, h3_index, parcel_id, anomaly_date,
                       observed_ndvi, expected_ndvi, z_score, severity, computed_at
                FROM anomaly_alerts_cache
                {where_sql}
                ORDER BY z_score ASC, computed_at DESC
                LIMIT ${param_idx}
            """
            params.append(limit)

            rows = await pg_conn.fetch(query, *params)

        if not rows:
            return {
                "source": "postgres_cache",
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
            "source": "postgres_cache",
            "status": "ok",
            "severity_filter": severity,
            "district_filter": district,
            "count": len(alerts),
            "alerts": alerts,
        }
    except Exception as e:
        logger.error("PostgreSQL anomaly alert query failed: %s", e)
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
    """Get latest yield risk assessments from PostgreSQL cache.

    Pre-computed by Dagster weekly yield risk job (Mann-Kendall trend analysis).
    Returns instantly from local cache.
    """
    from src.structures import get_async_db_connection

    try:
        async with get_async_db_connection() as pg_conn:
            where_clauses = []
            params = []
            param_idx = 1
            if district:
                where_clauses.append(f"district = ${param_idx}")
                params.append(district)
                param_idx += 1

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            query = f"""
                SELECT district, risk_level, risk_description, trend_slope,
                       kendall_tau, latest_ndvi, mean_ndvi, seasonal_deviation,
                       observations, computed_at
                FROM yield_risk_cache
                {where_sql}
                ORDER BY risk_level DESC, computed_at DESC
                LIMIT ${param_idx}
            """
            params.append(limit)

            rows = await pg_conn.fetch(query, *params)

        if not rows:
            return {
                "source": "postgres_cache",
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
            "source": "postgres_cache",
            "status": "ok",
            "district_filter": district,
            "count": len(assessments),
            "assessments": assessments,
        }
    except Exception as e:
        logger.error("PostgreSQL yield risk query failed: %s", e)
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
    """Get latest drought status from PostgreSQL cache.

    Pre-computed by Dagster weekly drought scan (VCI + NDWI analysis).
    Returns instantly from local cache.
    """
    from src.structures import get_async_db_connection

    try:
        async with get_async_db_connection() as pg_conn:
            where_clauses = []
            params = []
            param_idx = 1
            if district:
                where_clauses.append(f"district = ${param_idx}")
                params.append(district)
                param_idx += 1
            if drought_status:
                where_clauses.append(f"drought_status = ${param_idx}")
                params.append(drought_status)
                param_idx += 1

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            query = f"""
                SELECT district, drought_status, current_vci, latest_ndvi,
                       latest_ndwi, drought_period_count, description, computed_at
                FROM drought_cache
                {where_sql}
                ORDER BY current_vci ASC, computed_at DESC
                LIMIT ${param_idx}
            """
            params.append(limit)

            rows = await pg_conn.fetch(query, *params)

        if not rows:
            return {
                "source": "postgres_cache",
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
            "source": "postgres_cache",
            "status": "ok",
            "district_filter": district,
            "status_filter": drought_status,
            "count": len(districts),
            "districts": districts,
        }
    except Exception as e:
        logger.error("PostgreSQL drought status query failed: %s", e)
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
    """Get latest crop growth stage analysis from PostgreSQL cache.

    Pre-computed by Dagster weekly phenology job (NDVI curve analysis).
    Returns instantly from local cache.
    """
    from src.structures import get_async_db_connection

    try:
        async with get_async_db_connection() as pg_conn:
            where_clauses = []
            params = []
            param_idx = 1
            if district:
                where_clauses.append(f"district = ${param_idx}")
                params.append(district)
                param_idx += 1
            if stage:
                where_clauses.append(f"current_stage = ${param_idx}")
                params.append(stage)
                param_idx += 1

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            query = f"""
                SELECT district, current_stage, peak_ndvi, peak_date,
                       green_up_start, senescence_start, harvest_date,
                       observations, computed_at
                FROM phenology_cache
                {where_sql}
                ORDER BY computed_at DESC
                LIMIT ${param_idx}
            """
            params.append(limit)

            rows = await pg_conn.fetch(query, *params)

        if not rows:
            return {
                "source": "postgres_cache",
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
            "source": "postgres_cache",
            "status": "ok",
            "district_filter": district,
            "stage_filter": stage,
            "count": len(districts),
            "districts": districts,
        }
    except Exception as e:
        logger.error("PostgreSQL phenology query failed: %s", e)
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
    """Get daily weather statistics from PostgreSQL cache.

    Pre-computed by Dagster daily_weather_ingest from Copernicus AgERA5.
    Returns temperature (mean/max/min C), precipitation (mm/day),
    and solar radiation (MJ/m2/day) per district per day.
    """
    from src.structures import get_async_db_connection

    try:
        async with get_async_db_connection() as pg_conn:
            where_clauses = []
            params: list = []
            param_idx = 1
            if district:
                where_clauses.append(f"district = ${param_idx}")
                params.append(district)
                param_idx += 1
            if date_from:
                where_clauses.append(f"observation_date >= ${param_idx}")
                params.append(date_from)
                param_idx += 1
            if date_to:
                where_clauses.append(f"observation_date <= ${param_idx}")
                params.append(date_to)
                param_idx += 1
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
                LIMIT ${param_idx}
            """
            params.append(limit)

            rows = await pg_conn.fetch(query, *params)

        if not rows:
            return {
                "source": "postgres_cache",
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
            "source": "postgres_cache",
            "data_source": "Copernicus AgERA5 (sis-agrometeorological-indicators)",
            "status": "ok",
            "district_filter": district,
            "date_range": f"{date_from or 'last_30d'}/{date_to or 'now'}",
            "count": len(observations),
            "observations": observations,
        }
    except Exception as e:
        logger.error("PostgreSQL weather query failed: %s", e)
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
    """Get cell-level (ADM4) NDVI statistics from PostgreSQL cache.

    Pre-computed nightly by Dagster from Sentinel Hub at ~12 km² resolution.
    Returns instantly from local cache.
    """
    from src.structures import get_async_db_connection

    try:
        async with get_async_db_connection() as pg_conn:
            where_clauses = []
            params: list = []
            param_idx = 1
            if cell_name:
                where_clauses.append(f"cell_name ILIKE ${param_idx}")
                params.append(f"%{cell_name}%")
                param_idx += 1
            if district:
                # Use exact match (case-insensitive) to avoid cross-country
                # false positives. E.g. "Kigali" should not match Uganda districts.
                where_clauses.append(f"LOWER(district_name) = LOWER(${param_idx})")
                params.append(district)
                param_idx += 1

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            query = f"""
                SELECT cell_name, district_name, week_start,
                       mean_ndvi, std_ndvi, min_ndvi, max_ndvi,
                       valid_pixels, computed_at
                FROM ndvi_cell_cache
                {where_sql}
                ORDER BY computed_at DESC, cell_name
                LIMIT ${param_idx}
            """
            params.append(limit)

            rows = await pg_conn.fetch(query, *params)

        if not rows:
            return {
                "source": "postgres_cache",
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
            "source": "postgres_cache",
            "status": "ok",
            "cell_filter": cell_name,
            "district_filter": district,
            "count": len(cells),
            "cells": cells,
        }
    except Exception as e:
        logger.error("PostgreSQL cell NDVI query failed: %s", e)
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
    """Get parcel-level NDVI statistics from PostgreSQL cache.

    Pre-computed nightly by Dagster from Sentinel Hub at 10m native resolution
    for user-uploaded field boundaries tagged as rwanda_parcels.
    Returns instantly from local cache.
    """
    from src.structures import get_async_db_connection

    try:
        async with get_async_db_connection() as pg_conn:
            where_clauses = []
            params: list = []
            param_idx = 1
            if parcel_name:
                where_clauses.append(f"parcel_name ILIKE ${param_idx}")
                params.append(f"%{parcel_name}%")
                param_idx += 1
            if layer_id:
                where_clauses.append(f"layer_id = ${param_idx}")
                params.append(layer_id)
                param_idx += 1

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            query = f"""
                SELECT parcel_id, parcel_name, layer_id, week_start,
                       mean_ndvi, std_ndvi, min_ndvi, max_ndvi,
                       valid_pixels, area_ha, computed_at
                FROM ndvi_parcel_cache
                {where_sql}
                ORDER BY computed_at DESC, parcel_name
                LIMIT ${param_idx}
            """
            params.append(limit)

            rows = await pg_conn.fetch(query, *params)

        if not rows:
            return {
                "source": "postgres_cache",
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
            "source": "postgres_cache",
            "status": "ok",
            "parcel_filter": parcel_name,
            "layer_filter": layer_id,
            "count": len(parcels),
            "parcels": parcels,
        }
    except Exception as e:
        logger.error("PostgreSQL parcel NDVI query failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cache query failed: {e}",
        )


# ── Vector tile endpoints ────────────────────────────────────────────────
# These serve pre-computed PMTiles from S3 (generated by the
# nightly_ndvi_vector_tiles Dagster asset).  PMTiles is a single-file
# archive of vector tiles that supports HTTP byte-range requests, so
# MapLibre GL can fetch individual tiles on demand.
#
# Benefits over raster XYZ tiles:
#   - Native spatial filtering (district/sector/cell in feature properties)
#   - Dynamic styling on the frontend (color by NDVI value)
#   - 10-50x smaller than equivalent raster tiles
#   - No server-side rendering — MapLibre renders on the GPU

_NDVI_PMTILES_KEY = "rwanda/vector_tiles/rwanda_ndvi.pmtiles"


@rwanda_router.get(
    "/rwanda/tiles/ndvi.pmtiles",
    operation_id="rwanda_ndvi_vector_tiles",
)
async def ndvi_vector_tiles(
    request: Request,
    session: UserContext = Depends(verify_session_required),
):
    """Serve pre-computed NDVI H3 vector tiles as PMTiles.

    The PMTiles file contains H3-gridded NDVI data at two resolutions:
    - Resolution 7 (~5.16 km²) for district-level view (zoom 4-10)
    - Resolution 9 (~0.1 km²) for cell-level detail (zoom 10-14)

    Each hexagon has properties: h3, district, cell, ndvi, ndvi_std, date, level.
    MapLibre GL can filter by `district` or `cell` and color by `ndvi` value.

    Supports HTTP Range requests (required for PMTiles protocol).
    """
    from src.utils import get_async_s3_client, get_bucket_name, s3_op

    bucket = get_bucket_name()
    s3 = await get_async_s3_client()

    # Check file exists and get size
    try:
        head = await s3_op(
            s3.head_object(Bucket=bucket, Key=_NDVI_PMTILES_KEY),
            "head_object", "NDVI PMTiles",
        )
        file_size = head["ContentLength"]
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="NDVI vector tiles not yet generated. Run the nightly_ndvi_vector_tiles Dagster asset.",
        )

    range_header = request.headers.get("range")
    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Range, Content-Type",
        "Access-Control-Expose-Headers": "Content-Range, Accept-Ranges, Content-Length",
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
    }

    if range_header:
        range_match = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if range_match:
            start = int(range_match.group(1))
            end_str = range_match.group(2)
            end = min(int(end_str), file_size - 1) if end_str else file_size - 1
        else:
            start, end = 0, file_size - 1

        content_length = end - start + 1

        s3_resp = await s3_op(
            s3.get_object(Bucket=bucket, Key=_NDVI_PMTILES_KEY, Range=f"bytes={start}-{end}"),
            "get_object (range)", "NDVI PMTiles",
        )

        async def stream_range():
            body = s3_resp["Body"]
            while True:
                chunk = await body.read(8192)
                if not chunk:
                    break
                yield chunk
            body.close()

        return StreamingResponse(
            stream_range(),
            status_code=206,
            media_type="application/octet-stream",
            headers={
                **cors_headers,
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(content_length),
            },
        )

    # Full file request
    s3_resp = await s3_op(
        s3.get_object(Bucket=bucket, Key=_NDVI_PMTILES_KEY),
        "get_object", "NDVI PMTiles",
    )

    async def stream_full():
        body = s3_resp["Body"]
        while True:
            chunk = await body.read(8192)
            if not chunk:
                break
            yield chunk
        body.close()

    return StreamingResponse(
        stream_full(),
        status_code=200,
        media_type="application/octet-stream",
        headers={
            **cors_headers,
            "Content-Length": str(file_size),
        },
    )


@rwanda_router.get(
    "/rwanda/tiles/status",
    operation_id="rwanda_vector_tiles_status",
)
async def vector_tiles_status(
    session: UserContext = Depends(verify_session_required),
):
    """Check availability and metadata of vector tile layers.

    Returns which PMTiles layers are available and their sizes.
    The frontend uses this to decide between raster and vector tile rendering.
    """
    from src.utils import get_async_s3_client, get_bucket_name, s3_op

    bucket = get_bucket_name()
    s3 = await get_async_s3_client()

    layers = {}

    # Check NDVI vector tiles
    try:
        head = await s3_op(
            s3.head_object(Bucket=bucket, Key=_NDVI_PMTILES_KEY),
            "head_object", "NDVI PMTiles status",
        )
        layers["ndvi"] = {
            "available": True,
            "url": "/api/rwanda/tiles/ndvi.pmtiles",
            "size_bytes": head["ContentLength"],
            "last_modified": str(head.get("LastModified", "")),
            "format": "pmtiles",
            "layer_name": "ndvi",
            "properties": ["h3", "district", "cell", "ndvi", "ndvi_std", "date", "level", "pixels"],
            "zoom_range": {"min": 4, "max": 14},
        }
    except Exception:
        layers["ndvi"] = {
            "available": False,
            "url": None,
            "message": "Run nightly_ndvi_vector_tiles Dagster asset to generate",
        }

    return {
        "vector_tiles_enabled": any(v.get("available") for v in layers.values()),
        "layers": layers,
    }


# ── Admin: one-shot cache backfill ──────────────────────────────────────
# Triggers Sentinel Hub + ML pipeline to populate empty cache tables
# after a DuckDB → PostgreSQL migration.  Requires auth.

@rwanda_router.post(
    "/rwanda/admin/backfill-caches",
    operation_id="rwanda_admin_backfill_caches",
)
async def admin_backfill_caches(
    session: UserContext = Depends(verify_session_required),
):
    """One-shot backfill of all Rwanda analytics cache tables.

    Runs nightly_field_ndvi (NDVI + agri indices for 30 districts),
    then drought, anomaly, yield-risk, phenology, and weather.
    Designed to be called once after cache migration.
    """
    import asyncio

    results: dict = {}

    # Run the heavy Sentinel Hub job in a thread to avoid blocking
    def _run_field_ndvi():
        """Run nightly_field_ndvi asset logic synchronously."""
        import json as _json
        from datetime import datetime, timedelta

        import numpy as _np

        from src.services.sentinel_hub_service import (
            get_sentinel_hub_service,
            AGRI_INDEX_NAMES,
        )
        from src.structures import get_sync_db_connection

        sh = get_sentinel_hub_service()
        if sh is None or not sh.is_configured():
            return {"status": "skipped", "reason": "sentinel_hub_unavailable"}

        with get_sync_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT district, ST_AsGeoJSON(geom) FROM rwanda_district_boundaries ORDER BY district"
                )
                district_rows = cur.fetchall()

        if not district_rows:
            return {"status": "skipped", "reason": "no_district_boundaries"}

        now = datetime.utcnow()
        date_from = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")
        week_start = date_from
        written = 0
        errors = []

        for district, geom_geojson in district_rows:
            if not geom_geojson:
                continue
            try:
                geometry = _json.loads(geom_geojson)
                stats = sh.get_agri_stats(
                    geometry=geometry,
                    date_from=date_from,
                    date_to=date_to,
                )
                if "error" in stats:
                    errors.append(district)
                    continue

                intervals = stats.get("intervals", [])
                if not intervals:
                    continue

                index_stats: dict = {}
                total_pixels = 0
                for idx_name in AGRI_INDEX_NAMES:
                    means = [
                        iv[idx_name]["mean"]
                        for iv in intervals
                        if idx_name in iv and iv[idx_name].get("valid_pixels", 0) > 0
                    ]
                    if means:
                        index_stats[f"{idx_name}_mean"] = round(float(_np.mean(means)), 4)
                        index_stats[f"{idx_name}_std"] = round(float(_np.std(means)), 4)
                    else:
                        index_stats[f"{idx_name}_mean"] = None
                        index_stats[f"{idx_name}_std"] = None

                for iv in intervals:
                    if "ndvi" in iv:
                        total_pixels += iv["ndvi"].get("valid_pixels", 0)

                with get_sync_db_connection() as pg_conn:
                    with pg_conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO agri_indices_cache
                                (admin_level, admin_name, parent_name, week_start,
                                 ndvi_mean, ndvi_std, evi_mean, evi_std,
                                 ndwi_mean, ndwi_std, savi_mean, savi_std,
                                 ndre_mean, ndre_std, ndbi_mean, ndbi_std,
                                 valid_pixels)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                            (
                                "district", district, None, week_start,
                                index_stats.get("ndvi_mean"), index_stats.get("ndvi_std"),
                                index_stats.get("evi_mean"), index_stats.get("evi_std"),
                                index_stats.get("ndwi_mean"), index_stats.get("ndwi_std"),
                                index_stats.get("savi_mean"), index_stats.get("savi_std"),
                                index_stats.get("ndre_mean"), index_stats.get("ndre_std"),
                                index_stats.get("ndbi_mean"), index_stats.get("ndbi_std"),
                                total_pixels,
                            ),
                        )
                        cur.execute(
                            """INSERT INTO ndvi_field_cache
                                (district, week_start, mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                            (
                                district, week_start,
                                index_stats.get("ndvi_mean"),
                                index_stats.get("ndvi_std"),
                                index_stats.get("ndvi_mean"),  # min approx
                                index_stats.get("ndvi_mean"),  # max approx
                                total_pixels,
                            ),
                        )
                    pg_conn.commit()
                written += 1
                logger.info("Backfill NDVI: %s done (NDVI=%.3f)", district, index_stats.get("ndvi_mean", 0) or 0)
            except Exception as e:
                logger.warning("Backfill NDVI failed for %s: %s", district, e)
                errors.append(district)

        return {"status": "ok", "districts": written, "errors": errors}

    def _run_derived_caches():
        """Run drought, anomaly, yield-risk, phenology from ndvi_field_cache."""
        from src.services.ml_inference import get_ml_service
        from src.structures import get_sync_db_connection

        ml = get_ml_service()
        results = {}

        with get_sync_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT district, week_start, mean_ndvi
                    FROM ndvi_field_cache
                    WHERE week_start >= CURRENT_DATE - INTERVAL '90 days'
                    ORDER BY district, week_start
                """)
                rows = cur.fetchall()

        if not rows:
            return {"status": "no_ndvi_data"}

        # Group by district
        district_series: dict = {}
        for district, week_start, mean_ndvi in rows:
            if district not in district_series:
                district_series[district] = []
            district_series[district].append({
                "date": str(week_start),
                "mean_ndvi": float(mean_ndvi) if mean_ndvi else 0.0,
            })

        # Drought
        drought_count = 0
        for district, ts in district_series.items():
            if len(ts) < 3:
                continue
            try:
                drought = ml.detect_drought(ts)
                if "error" in drought:
                    continue
                with get_sync_db_connection() as pg_conn:
                    with pg_conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO drought_cache
                                (district, drought_status, current_vci, latest_ndvi,
                                 latest_ndwi, drought_period_count, description)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                            (district, drought.get("drought_status"), drought.get("current_vci"),
                             drought.get("latest_ndvi"), drought.get("latest_ndwi"),
                             drought.get("drought_period_count"), drought.get("description")),
                        )
                    pg_conn.commit()
                drought_count += 1
            except Exception as e:
                logger.warning("Drought backfill failed for %s: %s", district, e)
        results["drought"] = drought_count

        # Yield risk
        yield_count = 0
        for district, ts in district_series.items():
            if len(ts) < 3:
                continue
            try:
                risk = ml.predict_yield_risk(ts)
                if "error" in risk:
                    continue
                with get_sync_db_connection() as pg_conn:
                    with pg_conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO yield_risk_cache
                                (district, risk_level, risk_description, trend_slope,
                                 kendall_tau, latest_ndvi, mean_ndvi, seasonal_deviation, observations)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                            (district, risk.get("risk_level"), risk.get("risk_description"),
                             risk.get("trend_slope"), risk.get("kendall_tau"),
                             risk.get("latest_ndvi"), risk.get("mean_ndvi"),
                             risk.get("seasonal_deviation"), risk.get("observations")),
                        )
                    pg_conn.commit()
                yield_count += 1
            except Exception as e:
                logger.warning("Yield risk backfill failed for %s: %s", district, e)
        results["yield_risk"] = yield_count

        # Anomaly
        anomaly_count = 0
        for district, ts in district_series.items():
            if len(ts) < 5:
                continue
            try:
                anomalies = ml.detect_anomalies(ts)
                if "error" in anomalies or not anomalies.get("anomalies"):
                    continue
                with get_sync_db_connection() as pg_conn:
                    with pg_conn.cursor() as cur:
                        for a in anomalies["anomalies"]:
                            cur.execute(
                                """INSERT INTO anomaly_alerts_cache
                                    (district, anomaly_date, observed_ndvi, expected_ndvi,
                                     z_score, severity)
                                VALUES (%s, %s, %s, %s, %s, %s)""",
                                (district, a.get("date"), a.get("observed"), a.get("expected"),
                                 a.get("z_score"), a.get("severity")),
                            )
                    pg_conn.commit()
                anomaly_count += 1
            except Exception as e:
                logger.warning("Anomaly backfill failed for %s: %s", district, e)
        results["anomalies"] = anomaly_count

        # Phenology
        pheno_count = 0
        for district, ts in district_series.items():
            if len(ts) < 5:
                continue
            try:
                pheno = ml.analyze_crop_phenology(ts)
                if "error" in pheno:
                    continue
                with get_sync_db_connection() as pg_conn:
                    with pg_conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO phenology_cache
                                (district, current_stage, peak_ndvi, peak_date,
                                 green_up_start, senescence_start, harvest_date, observations)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                            (district, pheno.get("current_stage"), pheno.get("peak_ndvi"),
                             pheno.get("peak_date"), pheno.get("green_up_start"),
                             pheno.get("senescence_start"), pheno.get("harvest_date"),
                             pheno.get("observations")),
                        )
                    pg_conn.commit()
                pheno_count += 1
            except Exception as e:
                logger.warning("Phenology backfill failed for %s: %s", district, e)
        results["phenology"] = pheno_count

        return results

    def _run_weather():
        """Backfill weather from Open-Meteo (free, no API key needed)."""
        from src.services.weather_service import get_weather_service
        from src.structures import get_sync_db_connection

        ws = get_weather_service()
        if ws is None:
            return {"status": "skipped", "reason": "weather_service_unavailable"}

        # Get district centroids from PostGIS
        with get_sync_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT district, ST_Y(ST_Centroid(geom)), ST_X(ST_Centroid(geom)) "
                    "FROM rwanda_district_boundaries ORDER BY district"
                )
                districts = cur.fetchall()

        if not districts:
            return {"status": "skipped", "reason": "no_district_boundaries"}

        # Build centroid list for Open-Meteo bulk request
        centroids = [
            {"district": d, "lat": lat, "lon": lon}
            for d, lat, lon in districts
        ]

        try:
            records = ws.fetch_openmeteo_districts(centroids, past_days=10)
        except Exception as e:
            logger.warning("Open-Meteo fetch failed: %s", e)
            return {"status": "error", "reason": str(e)}

        if not records:
            return {"status": "no_data"}

        written = 0
        with get_sync_db_connection() as pg_conn:
            with pg_conn.cursor() as cur:
                for rec in records:
                    try:
                        cur.execute(
                            """INSERT INTO weather_daily_cache
                                (district, observation_date, temperature_mean, temperature_max,
                                 temperature_min, precipitation, solar_radiation)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING""",
                            (
                                rec["district"], rec["date"],
                                rec.get("temperature_mean"),
                                rec.get("temperature_max"),
                                rec.get("temperature_min"),
                                rec.get("precipitation"),
                                rec.get("solar_radiation"),
                            ),
                        )
                        written += 1
                    except Exception as e:
                        logger.warning("Weather insert failed for %s/%s: %s", rec["district"], rec["date"], e)
            pg_conn.commit()

        return {"records": written, "districts": len(districts)}

    # Run all backfills sequentially in a thread pool
    loop = asyncio.get_running_loop()

    logger.info("Starting cache backfill — NDVI + agri indices for 30 districts...")
    results["ndvi"] = await loop.run_in_executor(None, _run_field_ndvi)

    logger.info("Starting derived caches (drought, yield risk, anomaly, phenology)...")
    results["derived"] = await loop.run_in_executor(None, _run_derived_caches)

    logger.info("Starting weather backfill...")
    results["weather"] = await loop.run_in_executor(None, _run_weather)

    logger.info("Cache backfill complete: %s", results)
    return results
