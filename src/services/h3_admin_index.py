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
"""One table linking every H3 hexagon in Rwanda to every admin level.

Each resolution-9 hexagon (~0.1 km2) is matched to each level (province,
district, sector, cell, village) independently, by the area it actually
shares with each unit, using the rwanda_*_boundaries polygons and their ids:

- h3_admin_overlap: one row per (level, unit, hexagon) that overlap, with
  the shared area, the share of the hexagon and the share of the unit.
  Area-weighted roll-ups from hexagons to any level are exact, and a village
  smaller than one hexagon still has the hexagons that touch it.
- h3_admin_cells: one row per hexagon with its main unit at every level
  (largest shared area) and its outline, for lookups and maps.

Levels are matched independently rather than inferred from village names:
villages do not cover national parks and lakes (~4% of Rwanda), and some
village cell/sector names do not match the cell table.

Build with `python -m src.services.h3_admin_index` or the Dagster job
`h3_admin_index_job` after boundaries change.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import h3
import numpy as np
import shapely
from shapely.geometry import Polygon, shape
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

RESOLUTION = 9
LEVELS = ("province", "district", "sector", "cell", "village")

# Each level's source table: SQL selecting (unit_id, unit_name, GeoJSON geometry).
_LEVEL_SQL: dict[str, str] = {
    "province": "SELECT province, province, ST_AsGeoJSON(geom) FROM rwanda_province_boundaries",
    "district": "SELECT district, district, ST_AsGeoJSON(geom) FROM rwanda_district_boundaries",
    "sector": "SELECT sector_id::text, sector_name, ST_AsGeoJSON(geom) FROM rwanda_sector_boundaries",
    "cell": "SELECT cell_id::text, cell_name, ST_AsGeoJSON(geom) FROM rwanda_cell_boundaries",
    "village": "SELECT village_id::text, village_name, ST_AsGeoJSON(geom) FROM rwanda_village_boundaries",
}


@dataclass(frozen=True)
class Overlap:
    h3_index: str
    overlap_km2: float
    hex_fraction: float  # share of the hexagon inside the unit


def hexagon(h3_index: str) -> Polygon:
    """The hexagon as a lon/lat polygon."""
    return Polygon([(lng, lat) for lat, lng in h3.cell_to_boundary(h3_index)])


def unit_overlaps(geom: BaseGeometry, resolution: int = RESOLUTION) -> list[Overlap]:
    """Every hexagon sharing area with `geom`, and how much.

    h3 4.1 fills a polygon by hexagon centre only, so the polygon is padded by
    more than one hexagon width first and hexagons without real overlap are
    dropped; small units (a village smaller than one hexagon) keep the
    hexagons that touch them. Shares are planar ratios in lon/lat, accurate at
    the size of one hexagon; areas use h3's exact cell areas.
    """
    if geom.is_empty:
        return []
    pad_deg = 2.5 * h3.average_hexagon_edge_length(resolution, unit="km") / 111.0
    candidates = h3.geo_to_cells(geom.buffer(pad_deg), resolution)
    if not candidates:
        return []
    cells = sorted(candidates)
    hexes = np.array([hexagon(c) for c in cells], dtype=object)
    shapely.prepare(geom)
    inside = shapely.contains_properly(geom, hexes)
    hex_area = shapely.area(hexes)
    shared = np.where(inside, hex_area, 0.0)
    edge = ~inside & shapely.intersects(geom, hexes)
    if edge.any():
        shared[edge] = shapely.area(shapely.intersection(hexes[edge], geom))
    out = []
    for cell, part, whole in zip(cells, shared, hex_area):
        if part <= 0 or whole <= 0:
            continue
        fraction = min(1.0, float(part / whole))
        out.append(Overlap(cell, fraction * h3.cell_area(cell, unit="km^2"), fraction))
    return out


@dataclass(frozen=True)
class OverlapRow:
    h3_index: str
    admin_level: str
    unit_id: str
    unit_name: str
    overlap_km2: float
    hex_fraction: float
    unit_fraction: float


def level_rows(level: str, units: Iterable[tuple[str, str, BaseGeometry]],
               resolution: int = RESOLUTION) -> list[OverlapRow]:
    """Overlap rows for one level; unit_fraction = share of the unit's area."""
    rows: list[OverlapRow] = []
    for unit_id, unit_name, geom in units:
        overlaps = unit_overlaps(geom, resolution)
        total = sum(o.overlap_km2 for o in overlaps)
        for o in overlaps:
            rows.append(OverlapRow(o.h3_index, level, unit_id, unit_name, o.overlap_km2,
                                   o.hex_fraction, o.overlap_km2 / total if total else 0.0))
    return rows


def main_units(rows: Iterable[OverlapRow]) -> dict[str, tuple[str, str]]:
    """{hexagon: (unit_id, unit_name)} of the unit sharing the most area (ties: lowest id)."""
    best: dict[str, OverlapRow] = {}
    for r in rows:
        cur = best.get(r.h3_index)
        if cur is None or (r.overlap_km2, cur.unit_id) > (cur.overlap_km2, r.unit_id):
            best[r.h3_index] = r
    return {h: (r.unit_id, r.unit_name) for h, r in best.items()}


async def build(conn: Any, resolution: int = RESOLUTION) -> dict[str, Any]:
    """Rebuild both tables from the boundary tables, in one transaction."""
    started = datetime.now(timezone.utc)
    mains: dict[str, dict[str, tuple[str, str]]] = {}
    counts: dict[str, int] = {}
    async with conn.transaction():
        await conn.execute("DELETE FROM h3_admin_overlap")
        await conn.execute("DELETE FROM h3_admin_cells")
        for level in LEVELS:
            units = [(r[0], r[1], shape(json.loads(r[2]))) for r in await conn.fetch(_LEVEL_SQL[level])]
            rows = await asyncio.to_thread(level_rows, level, units, resolution)
            await conn.copy_records_to_table(
                "h3_admin_overlap",
                records=[(r.h3_index, r.admin_level, r.unit_id, r.unit_name, r.overlap_km2,
                          r.hex_fraction, r.unit_fraction) for r in rows],
                columns=["h3_index", "admin_level", "unit_id", "unit_name", "overlap_km2",
                         "hex_fraction", "unit_fraction"],
            )
            mains[level] = main_units(rows)
            counts[level] = len(rows)
            logger.info("h3_admin_index: %s %d units, %d overlap rows", level, len(units), len(rows))
        hexes = sorted(set().union(*(m.keys() for m in mains.values())))

        def main(level: str, h: str) -> tuple[Optional[str], Optional[str]]:
            return mains[level].get(h, (None, None))

        records = []
        for h in hexes:
            sector, cell, village = main("sector", h), main("cell", h), main("village", h)
            records.append((
                h, resolution, main("province", h)[0], main("district", h)[0],
                int(sector[0]) if sector[0] else None, sector[1],
                int(cell[0]) if cell[0] else None, cell[1],
                int(village[0]) if village[0] else None, village[1],
                hexagon(h).wkt,
            ))
        await conn.execute("""
            CREATE TEMP TABLE _h3_cells_load (
                h3_index text, resolution smallint, province text, district text,
                sector_id int, sector_name text, cell_id int, cell_name text,
                village_id int, village_name text, wkt text
            ) ON COMMIT DROP
        """)
        await conn.copy_records_to_table("_h3_cells_load", records=records)
        await conn.execute("""
            INSERT INTO h3_admin_cells (h3_index, resolution, province, district, sector_id,
                sector_name, cell_id, cell_name, village_id, village_name, geom)
            SELECT h3_index, resolution, province, district, sector_id, sector_name,
                   cell_id, cell_name, village_id, village_name, ST_GeomFromText(wkt, 4326)
            FROM _h3_cells_load
        """)
        summary = {"resolution": resolution, "hexagons": len(hexes), "overlap_rows": counts,
                   "built_at": started.isoformat()}
        await conn.execute(
            "INSERT INTO h3_admin_index_meta (built_at, resolution, summary) VALUES ($1, $2, $3)",
            started, resolution, json.dumps(summary),
        )
    return summary


async def admin_units_for_hexagon(conn: Any, h3_index: str) -> dict[str, Any]:
    """Every admin unit sharing area with a hexagon of resolution 5 or finer.

    A coarser hexagon is answered through its resolution-9 children.
    """
    resolution = h3.get_resolution(h3_index)
    if resolution < 5:
        raise ValueError("use a hexagon of resolution 5 or finer (~250 km2)")
    if resolution > RESOLUTION:
        h3_index = h3.cell_to_parent(h3_index, RESOLUTION)
        children = [h3_index]
    else:
        children = list(h3.cell_to_children(h3_index, RESOLUTION))
    rows = await conn.fetch(
        """
        SELECT admin_level, unit_id, unit_name, sum(overlap_km2) AS km2
        FROM h3_admin_overlap WHERE h3_index = ANY($1::text[])
        GROUP BY admin_level, unit_id, unit_name ORDER BY admin_level, km2 DESC
        """,
        children,
    )
    out: dict[str, list[dict[str, Any]]] = {level: [] for level in LEVELS}
    for r in rows:
        out[r["admin_level"]].append({"id": r["unit_id"], "name": r["unit_name"],
                                      "shared_km2": round(float(r["km2"]), 4)})
    return {"h3_index": h3_index, "resolution": h3.get_resolution(h3_index), "units": out}


async def hexagons_for_unit(conn: Any, level: str, unit_id: str) -> list[dict[str, Any]]:
    """The resolution-9 hexagons sharing area with one admin unit."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    rows = await conn.fetch(
        """
        SELECT h3_index, overlap_km2, hex_fraction, unit_fraction
        FROM h3_admin_overlap WHERE admin_level = $1 AND unit_id = $2 ORDER BY h3_index
        """,
        level, unit_id,
    )
    return [{"h3_index": r["h3_index"], "shared_km2": round(float(r["overlap_km2"]), 5),
             "hex_fraction": round(float(r["hex_fraction"]), 4),
             "unit_fraction": round(float(r["unit_fraction"]), 6)} for r in rows]


async def roll_up(conn: Any, values: dict[str, float], level: str) -> list[dict[str, Any]]:
    """Area-weighted mean of resolution-9 hexagon values for every unit of `level`.

    `covered_fraction` is the share of the unit's area that had a value, so a
    unit with values for only part of its area says so.
    """
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    if not values:
        return []
    keys, vals = list(values), [float(values[k]) for k in values]
    rows = await conn.fetch(
        """
        WITH v AS (SELECT * FROM unnest($2::text[], $3::float8[]) AS t(h3_index, value))
        SELECT o.unit_id, o.unit_name,
               sum(o.overlap_km2 * v.value) / sum(o.overlap_km2) AS mean,
               sum(o.unit_fraction) AS covered
        FROM h3_admin_overlap o JOIN v USING (h3_index)
        WHERE o.admin_level = $1
        GROUP BY o.unit_id, o.unit_name ORDER BY o.unit_name
        """,
        level, keys, vals,
    )
    return [{"id": r["unit_id"], "name": r["unit_name"], "mean": float(r["mean"]),
             "covered_fraction": round(min(1.0, float(r["covered"])), 4)} for r in rows]


async def build_from_env(resolution: int = RESOLUTION) -> dict[str, Any]:
    """`build()` on a connection from the POSTGRES_* environment variables."""
    import os

    import asyncpg

    conn = await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "postgresdb"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ.get("POSTGRES_DB", "mundidb"))
    try:
        return await build(conn, resolution)
    finally:
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(asyncio.run(build_from_env()), indent=1))
