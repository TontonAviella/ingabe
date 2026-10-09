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

"""REST API routes for Rwanda admin units and district NDVI.

Endpoints:
  GET /rwanda/h3/{h3_index}/admin            - Admin units a hexagon shares area with
  GET /rwanda/admin/{level}/{unit_id}/hexagons - Hexagons sharing area with one admin unit
  GET /rwanda/ndvi/districts                 - District outlines with latest NDVI
  GET /rwanda/admin/{level}/outlines         - Admin unit outlines in view
"""

import json
import logging
from typing import Any, Optional

import h3
from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.services import admin_boundaries, data_coverage, h3_admin_index, industry, ndvi_classes
from src.dependencies.session import UserContext, verify_session_required

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


@rwanda_router.get("/rwanda/h3/{h3_index}/admin")
async def get_hexagon_admin_units(
    h3_index: str,
    session: UserContext = Depends(verify_session_required),
):
    """Every province, district, sector, cell and village a hexagon shares area with.

    From the precomputed H3 admin index (resolution 9); coarser hexagons
    (resolution 5-8) are answered through their resolution-9 children.
    """
    from src.structures import get_async_db_connection

    if not h3.is_valid_cell(h3_index):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="not a valid H3 cell")
    try:
        async with get_async_db_connection() as conn:
            return await h3_admin_index.admin_units_for_hexagon(conn, h3_index)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@rwanda_router.get("/rwanda/admin/{level}/{unit_id}/hexagons")
async def get_admin_unit_hexagons(
    level: str,
    unit_id: str,
    geojson: bool = Query(False, description="Return hexagon outlines as a GeoJSON FeatureCollection"),
    session: UserContext = Depends(verify_session_required),
):
    """The resolution-9 hexagons sharing area with one admin unit, with the shares.

    unit_id: province or district name; sector_id, cell_id or village_id.
    """
    from src.structures import get_async_db_connection

    try:
        async with get_async_db_connection() as conn:
            hexagons = await h3_admin_index.hexagons_for_unit(conn, level, unit_id)
            if not geojson:
                return {"level": level, "unit_id": unit_id, "count": len(hexagons), "hexagons": hexagons}
            outlines = {
                r["h3_index"]: json.loads(r["geometry"])
                for r in await conn.fetch(
                    "SELECT h3_index, ST_AsGeoJSON(geom, 6) AS geometry FROM h3_admin_cells "
                    "WHERE h3_index = ANY($1::text[])",
                    [h["h3_index"] for h in hexagons],
                )
            }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {
        "type": "FeatureCollection",
        "level": level,
        "unit_id": unit_id,
        "features": [
            {"type": "Feature", "geometry": outlines[h["h3_index"]], "properties": h}
            for h in hexagons if h["h3_index"] in outlines
        ],
    }


@rwanda_router.get("/rwanda/ndvi/districts")
async def get_district_ndvi_map(
    project_id: Optional[str] = Query(None, description="the project being viewed: its industry decides"),
    session: UserContext = Depends(verify_session_required),
):
    """District outlines with each district's latest NDVI, for the dashboard map.

    NDVI is computed per district (Sentinel-2 10 m pixels averaged over the
    district), so the map colours whole districts rather than hexagons that
    would all repeat their district's value. Vegetation is a farm capability:
    other industries' projects get the outlines only (audit R2-13).
    """
    from src.structures import get_async_db_connection

    async with get_async_db_connection() as pg_conn:
        viewer_industry = (await industry.industry_of_project(pg_conn, project_id) if project_id else
                           await industry.industry_for_new_project(pg_conn, session.get_user_id(), session.get_org_id()))
        vegetation = industry.serves("district_ndvi_map", viewer_industry)
        rows = await pg_conn.fetch(
            """
            SELECT b.district,
                   ST_AsGeoJSON(ST_SimplifyPreserveTopology(b.geom, 0.002), 5) AS geometry,
                   n.ndvi_mean, n.week_start, n.computed_at
            FROM rwanda_district_boundaries b
            LEFT JOIN LATERAL (
                SELECT ndvi_mean, week_start, computed_at
                FROM agri_indices_cache a
                WHERE a.admin_level = 'district' AND lower(a.admin_name) = lower(b.district)
                ORDER BY a.week_start DESC, a.computed_at DESC
                LIMIT 1
            ) n ON true
            ORDER BY b.district
            """
        )
        counts = await admin_boundaries.units_per_district(pg_conn)
    features = [
        {
            "type": "Feature",
            "geometry": json.loads(r["geometry"]),
            "properties": {
                **(_district_ndvi_properties(r) if vegetation else {"district": r["district"]}),
                **{
                    f"shared_note_{level}": data_coverage.shared_value_note(
                        "district", r["district"], level, n)
                    for level, n in counts.get(r["district"].lower(), {}).items()
                    if vegetation  # the note explains a shared NDVI value; no value, no note
                },
            },
        }
        for r in rows
    ]
    return {
        "type": "FeatureCollection",
        "features": features,
        "legend": ndvi_classes.legend() if vegetation else None,
        "levels": data_coverage.map_levels(("district",)),
        "data_coverage": data_coverage.describe("sentinel2", "district") if vegetation else None,
        "vegetation": vegetation,
    }


@rwanda_router.get("/rwanda/admin/{level}/outlines")
async def get_admin_outlines(
    level: str,
    bbox: Optional[str] = Query(None, description="west,south,east,north; required below district"),
    session: UserContext = Depends(verify_session_required),
):
    """Outlines of the district, sector, cell or village units in view, for the map."""
    from src.structures import get_async_db_connection

    box = None
    if bbox:
        try:
            west, south, east, north = (float(v) for v in bbox.split(","))
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bbox is west,south,east,north")
        box = (west, south, east, north)
    try:
        async with get_async_db_connection() as conn:
            return await admin_boundaries.admin_outlines(conn, level, box)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


def _district_ndvi_properties(r: Any) -> dict[str, Any]:
    ndvi = None if r["ndvi_mean"] is None else round(float(r["ndvi_mean"]), 3)
    cls = ndvi_classes.ndvi_class(ndvi)
    return {
        "district": r["district"],
        "mean_ndvi": ndvi,
        "ndvi_class": cls.key if cls else None,
        "ndvi_label": cls.label if cls else None,
        "color": cls.color if cls else None,
        "week_start": str(r["week_start"]) if r["week_start"] else None,
        "computed_at": r["computed_at"].isoformat() if r["computed_at"] else None,
    }
