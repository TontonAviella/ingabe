"""The satellite view of a drone photo as two layers of the map, each with its own switch.

satellite_view measures; this module draws what it measured: the plants picture (NDVI squares in ndvi_classes'
colours, older squares faded) and FAO's soil moisture (100 m squares in blues, drawn over the plants as
outlined squares with their reading, so both can be read at once and either can be hidden). Each layer's
legend carries its dates: a reader must not have to open anything to learn how old the picture is.

A photo keeps one layer of each kind on a map. Asked again while the satellites have nothing newer, the
layers already there are used; when there is something newer, the new layer takes the old one's place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from src.postgis_tiles import MVT_LAYER_NAME
from src.services import ndvi_classes, satellite_view
from src.services.h3_layer_persistence import save_analysis_layer
from src.structures import get_async_read_connection

logger = logging.getLogger(__name__)

PLANTS_KIND = "satellite_plants"
SOIL_KIND = "satellite_soil_moisture"
FADED_COLOUR = "#d4d4d4"  # the legend's swatch for "older look"
PLANTS_MAXZOOM = 16  # a 10 m square is about 13 screen pixels wide at zoom 16; deeper zooms stretch these tiles
SOIL_MAXZOOM = 14
# Seen on the Cyampirita photo, 2026-11-23: at zoom 14 a 100 m square is about 20 screen pixels and its "68%"
# ran into its neighbours'; at 15 it is about 40. A fill above 0.3 tinted the plants' greens underneath.
SOIL_LABELS_FROM_ZOOM = 15
SOIL_FILL = 0.3
LABEL_FONT = ["Open Sans Semibold"]  # the font the app's own map labels use


@dataclass(frozen=True)
class OnMap:
    layer_id: str
    name: str
    kind: str
    reused: bool  # the layer was already on the map with this same data
    bounds: Optional[list[float]]


def plants_stamp(plants: satellite_view.Plants) -> str:
    """Changes when a newer pass changes any square's day."""
    days = sorted({int(day) for day in plants.seen[plants.seen > 0].ravel()})
    return ",".join(date.fromordinal(day).isoformat() for day in days)


def soil_stamp(soil: satellite_view.Soil) -> str:
    """Changes with the ten-day period, and when FAO's first estimate has been replaced."""
    return f"{soil.period}:{'first' if soil.first_estimate else 'final'}"


def plants_legend(plants: satellite_view.Plants, square_m: int) -> dict[str, Any]:
    main = plants.main_day
    title = f"Plants from the satellite (NDVI), {square_m} m squares, seen {main.day} {main:%b %Y}"
    legend = ndvi_classes.ground_legend(title)
    if ((plants.seen > 0) & (plants.seen <= main.toordinal() - satellite_view.FADED_AFTER_DAYS)).any():
        legend["items"].append({"label": "Faded: an older look (tap a square for its day)", "color": FADED_COLOUR})
    return legend


def plants_style(layer_id: str, legend: dict[str, Any]) -> list[dict[str, Any]]:
    colour: list[Any] = ["step", ["coalesce", ["get", "ndvi"], -1], ndvi_classes.NDVI_CLASSES[0].color]
    for cls in ndvi_classes.NDVI_CLASSES[1:]:
        colour += [cls.min_ndvi, cls.color]
    return [{
        "id": f"satellite-plants-{layer_id}",
        "type": "fill",
        "source": layer_id,
        "source-layer": MVT_LAYER_NAME,
        "metadata": {"mundi:legend": legend},
        "paint": {
            "fill-color": colour,
            "fill-opacity": ["case", ["==", ["get", "faded"], True], 0.3, 0.72],
            "fill-antialias": False,
        },
    }]


def soil_legend(soil: satellite_view.Soil) -> dict[str, Any]:
    title = f"Soil moisture, {satellite_view.period_words(soil.period)} (FAO estimate, {satellite_view.SOIL_SQUARE_M} m squares)"
    if soil.first_estimate:
        title += ", first estimate"
    items = []
    for low, _ in satellite_view.SOIL_BANDS:
        label, colour = satellite_view.soil_band(low)
        items.append({"label": label, "color": colour})
    return {"title": title, "items": items}


def soil_style(layer_id: str, legend: dict[str, Any]) -> list[dict[str, Any]]:
    colour: list[Any] = ["step", ["coalesce", ["get", "moisture"], 0], satellite_view.SOIL_BANDS[0][1]]
    for low, band_colour in satellite_view.SOIL_BANDS[1:]:
        colour += [low, band_colour]
    base = {"source": layer_id, "source-layer": MVT_LAYER_NAME}
    return [
        {**base, "id": f"satellite-soil-{layer_id}", "type": "fill", "metadata": {"mundi:legend": legend},
         "paint": {"fill-color": colour, "fill-opacity": SOIL_FILL}},
        {**base, "id": f"satellite-soil-outline-{layer_id}", "type": "line",
         "paint": {"line-color": "#08306b", "line-width": 1.2, "line-opacity": 0.85}},
        {**base, "id": f"satellite-soil-label-{layer_id}", "type": "symbol", "minzoom": SOIL_LABELS_FROM_ZOOM,
         "layout": {"text-field": ["concat", ["to-string", ["round", ["get", "moisture"]]], "%"],
                    "text-font": LABEL_FONT, "text-size": 12},
         "paint": {"text-color": "#08306b", "text-halo-color": "#ffffff", "text-halo-width": 1.4}},
    ]


async def _already_on_map(map_id: str, photo_layer_id: str) -> dict[str, tuple[str, str, str, Optional[list[float]]]]:
    """kind -> (layer id, name, data stamp, bounds) for this photo's satellite layers on the map now."""
    async with get_async_read_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT l.layer_id, l.name, l.bounds, l.metadata->>'analysis_kind' AS kind, l.metadata->>'data_stamp' AS stamp
            FROM map_layers l
            JOIN user_mundiai_maps m ON l.layer_id = ANY(m.layers)
            WHERE m.id = $1
              AND l.metadata->>'analysis_kind' = ANY($2::text[])
              AND l.metadata->>'source_layer_id' = $3
            ORDER BY l.created_on
            """,
            map_id, [PLANTS_KIND, SOIL_KIND], photo_layer_id,
        )
    return {row["kind"]: (row["layer_id"], row["name"], row["stamp"] or "", list(row["bounds"]) if row["bounds"] else None)
            for row in rows}


async def put_on_map(seen: satellite_view.View, *, photo_layer_id: str, photo_name: str, user_uuid: str, map_id: str,
                     project_id: str) -> list[OnMap]:
    """The view's layers on the map: soil moisture first, plants after, so that the list reads plants on top.
    A part the view does not hold gets no layer. A layer that cannot be saved is logged and left out: the
    facts are still told."""
    there = await _already_on_map(map_id, photo_layer_id)
    made: list[OnMap] = []
    plans: list[tuple[str, str, str, Any]] = []
    if seen.soil is not None:
        plans.append((SOIL_KIND, f"Soil moisture - {photo_name}", soil_stamp(seen.soil), seen.soil))
    if seen.plants is not None:
        plans.append((PLANTS_KIND, f"Plants (NDVI) - {photo_name}", plants_stamp(seen.plants), seen.plants))
    for kind, name, stamp, part in plans:
        old = there.get(kind)
        if old and old[2] == stamp:
            made.append(OnMap(old[0], old[1], kind, True, old[3]))
            continue
        if kind == PLANTS_KIND:
            squares = satellite_view.plants_squares(part)
            legend = plants_legend(part, squares["square_m"])
            geojson, maxzoom, extra = squares["geojson"], PLANTS_MAXZOOM, {"square_m": squares["square_m"]}

            def style(layer_id: str, legend: dict[str, Any] = legend) -> list[dict[str, Any]]:
                return plants_style(layer_id, legend)
        else:
            legend = soil_legend(part)
            geojson, maxzoom, extra = satellite_view.soil_squares(part)["geojson"], SOIL_MAXZOOM, {
                "square_m": satellite_view.SOIL_SQUARE_M, "period": part.period}

            def style(layer_id: str, legend: dict[str, Any] = legend) -> list[dict[str, Any]]:
                return soil_style(layer_id, legend)
        try:
            saved = await save_analysis_layer(
                feature_collection=geojson, user_uuid=user_uuid, map_id=map_id, project_id=project_id, layer_name=name,
                maxzoom=maxzoom, style_for=style, replaces=(old[0],) if old else (),
                metadata={"source": "satellite_view", "analysis_kind": kind, "source_layer_id": photo_layer_id,
                          "data_stamp": stamp, **extra},
            )
        except Exception:  # noqa: BLE001 - the facts are still told; the reader is told the layer is missing
            logger.exception("satellite layers: %s for photo %s could not be saved", kind, photo_layer_id)
            continue
        made.append(OnMap(saved.layer_id, name, kind, False, saved.bounds))
    return made
