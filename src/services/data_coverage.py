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
"""What a data value covers, in plain words.

Each data source measures on squares of a fixed size, and each tool returns
one value per some area (a district, a cell, a forecast grid square). Users
read those values at whatever level they asked about, so a district average
reads like a village fact. ``annotate`` adds a ``data_coverage`` block to a
tool result: the source, how coarse it is, what one value covers, and a
one-sentence ``note`` Sage repeats to the user (system prompt rule 6).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

# Average area of each administrative level (km2), measured from the
# rwanda_*_boundaries tables on 2026-10-04: 5 provinces, 30 districts,
# 416 sectors, 2,148 cells, 14,815 villages.
ADMIN_LEVEL_AREA_KM2: dict[str, float] = {
    "province": 5063.0,
    "district": 844.0,
    "sector": 58.4,
    "cell": 11.3,
    "village": 1.64,
}
_LEVELS_COARSE_TO_FINE = ("province", "district", "sector", "cell", "village")


@dataclass(frozen=True)
class DataSource:
    name: str
    measured_every: str  # size of one measurement, in words
    square_km2: float  # area of one measurement


SOURCES: dict[str, DataSource] = {
    "chirps": DataSource("CHIRPS satellite rainfall estimates", "about 5 km squares", 30.0),
    "agera5": DataSource("AgERA5 weather data (Copernicus)", "about 11 km squares", 120.0),
    "forecast": DataSource("weather forecast models (ECMWF, GFS, ICON, GraphCast)",
                           "squares of 9 to 28 km", 80.0),
    "sentinel2": DataSource("Sentinel-2 satellite imagery", "10 m pixels", 0.0001),
}


@dataclass(frozen=True)
class ToolCoverage:
    source: str
    value_covers: Callable[[dict[str, Any]], str]  # args -> admin level or "point"


def _fixed(level: str) -> Callable[[dict[str, Any]], str]:
    return lambda _args: level


def _finest_named(args: dict[str, Any]) -> str:
    for level in reversed(_LEVELS_COARSE_TO_FINE):
        if args.get(level) or args.get(f"{level}_name"):
            return level
    return "district"


def _admin_level_arg(args: dict[str, Any]) -> str:
    level = str(args.get("admin_level") or "").lower()
    return level if level in ADMIN_LEVEL_AREA_KM2 else "district"


TOOLS: dict[str, ToolCoverage] = {
    "get_weather_stats": ToolCoverage("agera5", _fixed("district")),
    "detect_dry_spells": ToolCoverage("agera5", _fixed("district")),
    "get_forecast": ToolCoverage("forecast", _fixed("point")),
    "get_drought_status": ToolCoverage("sentinel2", _fixed("district")),
    "get_ndvi_stats": ToolCoverage("sentinel2", _fixed("district")),
    "get_cell_ndvi_stats": ToolCoverage("sentinel2", _finest_named),
    "get_agri_indices": ToolCoverage("sentinel2", _admin_level_arg),
}


def _and_list(words: tuple[str, ...]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def describe(source_key: str, value_covers: str) -> dict[str, Any]:
    """The coverage block for one value of ``source_key`` covering ``value_covers``."""
    src = SOURCES[source_key]
    if value_covers == "point":
        note = (f"Forecast for the model grid square containing the place ({src.measured_every}), "
                f"from {src.name}; nearby villages, cells and sectors share it.")
    else:
        area = ADMIN_LEVEL_AREA_KM2[value_covers]
        finer = _LEVELS_COARSE_TO_FINE[_LEVELS_COARSE_TO_FINE.index(value_covers) + 1:]
        note = (f"One value for the whole {value_covers} (about {area:g} km2 on average), "
                f"from {src.name} measured on {src.measured_every}.")
        if finer:
            note += f" Every {_and_list(finer)} in the {value_covers} gets this same value."
        if src.square_km2 > area:
            shared = round(src.square_km2 / area)
            note += (f" One measurement is larger than a {value_covers}, so about {shared} "
                     f"neighbouring {value_covers}s can share it.")
    return {"source": src.name, "measured_every": src.measured_every,
            "value_covers": value_covers, "note": note}


def point_sample_note(source_key: str, place: str, level: str) -> str:
    """Plain note for a value read from the one square at the centre of ``place``."""
    src = SOURCES[source_key]
    note = f"From {src.name}: the {src.measured_every.removeprefix('about ').removesuffix('s')} at the centre of {place}"
    area = ADMIN_LEVEL_AREA_KM2.get(level)
    if area is not None and src.square_km2 > area:
        return (note + f". One square is larger than a {level}, so about "
                f"{round(src.square_km2 / area)} neighbouring {level}s share these figures.")
    if area is not None:
        return note + f", not an average over the whole {level}."
    return note + "."


def annotate(tool_name: str, args: Any, result: Any) -> Optional[Any]:
    """``result`` with a ``data_coverage`` block, or None to leave it as it is.

    Accepts the result as a dict or as its JSON text and returns the same
    form. Unknown tools, non-object results and failures are left alone.
    """
    spec = TOOLS.get(tool_name)
    if spec is None:
        return None
    as_text = isinstance(result, str)
    try:
        payload = json.loads(result) if as_text else result
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or "data_coverage" in payload:
        return None
    if str(payload.get("status", "")).lower() in {"error", "failed", "failure"} or payload.get("error"):
        return None
    covers = spec.value_covers(args if isinstance(args, dict) else {})
    annotated = {**payload, "data_coverage": describe(spec.source, covers)}
    return json.dumps(annotated) if as_text else annotated
