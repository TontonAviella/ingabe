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
"""Seasonal evapotranspiration normals: what ET is usual for each dekad, where.

ET has a strong season: at the end of Rwanda's dry season (September) it is
about half its April value. Insurance compared every dekad with one yearly
constant (3.5 mm/day), so every early Season A read as a severe deficit.

``build`` reads WaPOR v3 L2-AETI-D (100 m, dekadal, 2018 onwards) over
Rwanda for every dekad of the year and every complete year, averages the
years per pixel, then averages pixels per H3 resolution-8 hexagon (~0.7 km2)
into ``et_dekad_normals``. ``normals_by_cell`` looks them up; reports compare
each observed dekad with its own normal (``attach``).

    python -m src.services.et_normals          # rebuild (about 20 min)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

import h3
import numpy as np

logger = logging.getLogger(__name__)

RESOLUTION = 8
FIRST_YEAR = 2018  # WaPOR v3 dekadal archive starts in 2018
MIN_YEARS = 3
RWANDA_BOUNDS = (28.85, -2.85, 30.9, -1.04)  # west, south, east, north


def dekad_of_year(code: str) -> int:
    """1-36 for a WaPOR dekad code "YYYY-MM-Dk"."""
    return (int(code[5:7]) - 1) * 3 + int(code[-1])


def dekads_between(start: date, end: date) -> list[int]:
    """Dekads of the year (1-36) touched by the days start..end."""
    out: list[int] = []
    d = start
    while d <= end:
        k = (d.month - 1) * 3 + min(3, (d.day - 1) // 10 + 1)
        if k not in out:
            out.append(k)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def cell_for(lat: float, lon: float) -> str:
    return h3.latlng_to_cell(lat, lon, RESOLUTION)


async def normals_by_cell(
    conn: Any, cells: Iterable[str], dekads: Iterable[int],
) -> dict[str, dict[int, float]]:
    """{cell: {dekad of year: normal AETI mm/day}} for the given cells and dekads."""
    rows = await conn.fetch(
        "SELECT h3_index, dekad, mean_mm_day FROM et_dekad_normals "
        "WHERE h3_index = ANY($1::text[]) AND dekad = ANY($2::smallint[])",
        sorted(set(cells)), sorted(set(dekads)),
    )
    out: dict[str, dict[int, float]] = {}
    for r in rows:
        out.setdefault(r["h3_index"], {})[r["dekad"]] = float(r["mean_mm_day"])
    return out


def attach(et_result: Optional[dict], normals: dict[int, float]) -> Optional[dict]:
    """``et_result`` (wapor_service.query_et) with normal_et_mm_per_day per dekad."""
    if not et_result or et_result.get("status") != "success":
        return et_result
    for entry in et_result.get("time_series", []):
        if not entry.get("dekad"):
            continue
        normal = normals.get(dekad_of_year(entry["dekad"]))
        entry["normal_et_mm_per_day"] = round(normal, 2) if normal is not None else None
    return et_result


def _read_rwanda(code: str) -> Optional[tuple[np.ndarray, Any]]:
    """AETI mm/day over Rwanda for one dekad (NaN = no data), or None if unpublished."""
    import rasterio  # lazy: GDAL stack, only for the build
    from rasterio.windows import from_bounds

    from src.services.wapor_service import GDAL_COG_ENV, LAYERS, NODATA, raster_url  # lazy: imports rasterio; only the build reads rasters

    scale, offset, _, _ = LAYERS["L2-AETI-D"]
    try:
        with rasterio.Env(**GDAL_COG_ENV), rasterio.open(raster_url("L2-AETI-D", code)) as ds:
            window = from_bounds(*RWANDA_BOUNDS, transform=ds.transform)
            raw = ds.read(1, window=window)
            transform = ds.window_transform(window)
    except Exception as e:  # noqa: BLE001 - an unpublished dekad is skipped, and logged
        logger.warning("WaPOR %s unavailable: %s", code, e)
        return None
    data = raw.astype("float32") * scale + offset
    data[raw == NODATA] = np.nan
    return data, transform


def _pixel_cells(shape: tuple[int, int], transform: Any, keep: set[str]) -> tuple[np.ndarray, list[str]]:
    """Per pixel: index into the returned cell list, or -1 outside ``keep``."""
    rows, cols = shape
    lons = transform.c + (np.arange(cols) + 0.5) * transform.a
    lats = transform.f + (np.arange(rows) + 0.5) * transform.e
    index: dict[str, int] = {}
    idx = np.full(shape, -1, dtype=np.int32)
    for r, lat in enumerate(lats):
        for c, lon in enumerate(lons):
            cell = h3.latlng_to_cell(float(lat), float(lon), RESOLUTION)
            if cell in keep:
                idx[r, c] = index.setdefault(cell, len(index))
    return idx, list(index)


async def build(conn: Any, last_year: Optional[int] = None) -> dict[str, Any]:
    """Rebuild et_dekad_normals for Rwanda from WaPOR FIRST_YEAR..last_year on one connection.

    For long runs prefer build_from_env: computing takes about an hour and an idle
    connection held that long gets closed before the write (2026-10-05).
    """
    last_year = last_year or date.today().year - 1
    records = await compute(await rwanda_cells(conn), last_year)
    return await write(conn, records, last_year)


async def rwanda_cells(conn: Any) -> set[str]:
    """The H3 cells (at RESOLUTION) that cover Rwanda, from the H3 admin index."""
    keep = {h3.cell_to_parent(r["h3_index"], RESOLUTION)
            for r in await conn.fetch("SELECT h3_index FROM h3_admin_cells")}
    if not keep:
        raise RuntimeError("h3_admin_cells is empty: build the H3 admin index first")
    return keep


async def compute(keep: set[str], last_year: int) -> list[tuple[str, int, float, int]]:
    """(h3_index, dekad, mean_mm_day, years) rows from WaPOR; needs no database connection."""
    years = list(range(FIRST_YEAR, last_year + 1))
    pixel_cell: Optional[np.ndarray] = None
    cells: list[str] = []
    records: list[tuple[str, int, float, int]] = []
    for dekad in range(1, 37):
        month, k = (dekad - 1) // 3 + 1, (dekad - 1) % 3 + 1
        stack = []
        for year in years:
            read = await asyncio.to_thread(_read_rwanda, f"{year}-{month:02d}-D{k}")
            if read is None:
                continue
            data, transform = read
            if pixel_cell is None:
                pixel_cell, cells = await asyncio.to_thread(_pixel_cells, data.shape, transform, keep)
            stack.append(data)
        if not stack or pixel_cell is None:
            continue
        arr = np.stack(stack)
        n_years = np.sum(~np.isnan(arr), axis=0)
        with np.errstate(invalid="ignore"):
            pixel_mean = np.nanmean(arr, axis=0)
        ok = (pixel_cell >= 0) & (n_years >= MIN_YEARS)
        sums = np.bincount(pixel_cell[ok], weights=pixel_mean[ok], minlength=len(cells))
        counts = np.bincount(pixel_cell[ok], minlength=len(cells))
        min_years = np.full(len(cells), len(years))
        np.minimum.at(min_years, pixel_cell[ok], n_years[ok])
        for i, cell in enumerate(cells):
            if counts[i]:
                records.append((cell, dekad, float(sums[i] / counts[i]), int(min_years[i])))
        logger.info("et_normals: dekad %d/36, %d years", dekad, len(stack))
    return records


async def write(conn: Any, records: list[tuple[str, int, float, int]], last_year: int) -> dict[str, Any]:
    """Replace et_dekad_normals with records in one transaction and log the build."""
    if not records:
        raise RuntimeError("no ET normals computed: refusing to empty et_dekad_normals")
    async with conn.transaction():
        await conn.execute("DELETE FROM et_dekad_normals")
        await conn.copy_records_to_table(
            "et_dekad_normals", records=records,
            columns=["h3_index", "dekad", "mean_mm_day", "years"],
        )
        summary = {"cells": len({r[0] for r in records}), "rows": len(records),
                   "years": f"{FIRST_YEAR}-{last_year}", "resolution": RESOLUTION}
        await conn.execute(
            "INSERT INTO et_dekad_normals_meta (built_at, summary) VALUES ($1, $2::jsonb)",
            datetime.now(timezone.utc), json.dumps(summary),
        )
    return summary


async def build_from_env(last_year: Optional[int] = None) -> dict[str, Any]:
    """CLI / Dagster entry: short connections before and after the hour of computing."""
    import asyncpg  # lazy: only the CLI/Dagster entry point opens its own connection

    async def connect() -> Any:
        return await asyncpg.connect(
            host=os.environ.get("POSTGRES_HOST", "postgresdb"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
            database=os.environ.get("POSTGRES_DB", "mundidb"),
        )

    last_year = last_year or date.today().year - 1
    conn = await connect()
    try:
        keep = await rwanda_cells(conn)
    finally:
        await conn.close()
    records = await compute(keep, last_year)
    conn = await connect()
    try:
        return await write(conn, records, last_year)
    finally:
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(asyncio.run(build_from_env()))
