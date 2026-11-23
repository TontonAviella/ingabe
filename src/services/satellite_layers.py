"""The satellite view of a drone photo as two layers of the map, each with its own switch.

satellite_view measures; this module draws what it measured, as hexagons like the app's other findings. Each
layer holds its picture in up to three hexagon sizes, and the map shows one size at a time: wide ones from afar,
smaller ones as it is zoomed in, so a weak patch can be followed down to where it sits.

- Plants: filled hexagons in ndvi_classes' shades, nearly solid so a colour on the map is the colour in the
  legend. A hexagon whose look is older than the photo's main one is drawn in grey, darker where it was
  greener: in colour it read as a sick patch beside the fresh part.
- Soil moisture: a rim in its band's blue and the reading written at the middle, with no fill. It is always
  drawn wider than the plants under it, so both can be read at once and either can be hidden. The map shows
  the facts of the topmost thing tapped: with nothing filled, a tap inside a soil hexagon reaches the plants
  hexagon under it, and a tap on the number or the rim gives the soil's facts.

Each legend carries its dates: a reader must not have to open anything to learn how old the picture is.

A photo keeps one layer of each kind on a map. Asked again while the satellites have nothing newer, the layers
already there are used; when there is something newer, the new layer takes the old one's place.

Roger, 2026-11-23, on the first drawing (10 m squares at 0.72 and 0.3 opacity under navy-outlined 100 m squares
with a number on each): the colours were ugly and could not be traced. Fading had made colours the legend did
not hold, and 135 outlined squares hid the plants.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional, Sequence

import h3

from src.postgis_tiles import MVT_LAYER_NAME
from src.services import ndvi_classes, satellite_view
from src.services.h3_layer_persistence import h3_resolution_filter, save_analysis_layer
from src.structures import get_async_read_connection

logger = logging.getLogger(__name__)

PLANTS_KIND = "satellite_plants"
SOIL_KIND = "satellite_soil_moisture"
DRAWING = "hexagons"  # part of each layer's stamp: a layer drawn another way is made again, not reused
# Tiles are cut to this zoom and stretched beyond it. A tile's grid is 4,096 steps wide: 0.3 m at zoom 15, against
# a smallest plants hexagon 9 m on a side; 1.2 m at zoom 13, against a smallest soil hexagon 66 m on a side.
PLANTS_MAXZOOM = 15
SOIL_MAXZOOM = 13
# A smaller size takes over once its hexagons are this wide on the screen. Soil moisture switches much later, so
# its hexagons stay several times wider than the plants' and hold their number. Seen on the Cyampirita photo,
# 2026-11-23: at 90 pixels the smaller soil hexagons came in over mid-sized plants ones, and a hundred rims and
# numbers hid the plants.
PLANTS_PIXELS_ACROSS = 20
SOIL_PIXELS_ACROSS = 160
ZOOM_0_METRES_A_PIXEL = 78271.517  # at the equator, on the 512-pixel tiles the map counts its zoom in
OLDER_LIGHT, OLDER_DARK = "#f3f4f6", "#4b5563"  # an older look: grey, from NDVI 0 to 0.8
OLDER_IN_LEGEND = "#9ca3af"
SOIL_INK = "#0b1f3a"
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
    return DRAWING + ":" + ",".join(date.fromordinal(day).isoformat() for day in days)


def soil_stamp(soil: satellite_view.Soil) -> str:
    """Changes with the ten-day period, and when FAO's first estimate has been replaced."""
    return f"{DRAWING}:{soil.period}:{'first' if soil.first_estimate else 'final'}"


def zoom_bands(sizes: Sequence[int], latitude: float, pixels_across: float) -> list[dict[str, Any]]:
    """Which hexagon size the map draws at which zoom, widest size first: [{"h3_resolution", "minzoom",
    "maxzoom"}], the shape the app's other hexagon layers record. The widest size is drawn from zoom 0; each
    smaller one takes over at the half zoom where its hexagons become pixels_across wide."""
    starts = [0.0]
    for size in sizes[1:]:
        across_m = 2 * float(h3.average_hexagon_edge_length(size, unit="m"))
        zoom = math.log2(ZOOM_0_METRES_A_PIXEL * math.cos(math.radians(latitude)) * pixels_across / across_m)
        starts.append(max(starts[-1] + 0.5, round(zoom * 2) / 2))
    ends = starts[1:] + [24.0]
    return [{"h3_resolution": size, "minzoom": start, "maxzoom": end} for size, start, end in zip(sizes, starts, ends)]


def _in_band(layer_id: str, band: dict[str, Any], also: Optional[list[Any]] = None) -> dict[str, Any]:
    """What every style layer of one hexagon size shares: its source, its size's filter and its zooms."""
    by_size = h3_resolution_filter(band["h3_resolution"])
    base: dict[str, Any] = {"source": layer_id, "source-layer": MVT_LAYER_NAME, "filter": ["all", by_size, also] if also else by_size}
    if band["minzoom"] > 0:
        base["minzoom"] = band["minzoom"]
    if band["maxzoom"] < 24:
        base["maxzoom"] = band["maxzoom"]
    return base


def plants_legend(plants: satellite_view.Plants) -> dict[str, Any]:
    main = plants.main_day
    legend = ndvi_classes.shade_legend(f"Plants from the satellite (NDVI), seen {main.day} {main:%b %Y}")
    if ((plants.seen > 0) & (plants.seen <= main.toordinal() - satellite_view.OLDER_AFTER_DAYS)).any():
        legend["items"].append({"label": "Grey: an older look, darker where greener (tap it for its day)", "color": OLDER_IN_LEGEND})
    return legend


def plants_style(layer_id: str, legend: dict[str, Any], bands: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    fresh: list[Any] = ["step", ["coalesce", ["get", "ndvi"], -1], ndvi_classes.NDVI_SHADES[0][1]]
    for low, shade in ndvi_classes.NDVI_SHADES[1:]:
        fresh += [low, shade]
    older = ["interpolate", ["linear"], ["coalesce", ["get", "ndvi"], 0], 0, OLDER_LIGHT, 0.8, OLDER_DARK]
    layers: list[dict[str, Any]] = []
    for band in bands:
        size = band["h3_resolution"]
        layers += [
            {**_in_band(layer_id, band), "id": f"satellite-plants-{layer_id}-r{size}", "type": "fill",
             "metadata": {"mundi:legend": legend},
             # nearly solid from afar; closer in, the photo's own ground shows through the hexagon it explains
             "paint": {"fill-color": ["case", ["==", ["get", "older"], True], older, fresh],
                       "fill-opacity": ["interpolate", ["linear"], ["zoom"], 15, 0.88, 18, 0.62]}},
            {**_in_band(layer_id, band), "id": f"satellite-plants-edge-{layer_id}-r{size}", "type": "line",
             "paint": {"line-color": "#ffffff", "line-width": 0.7, "line-opacity": 0.75}},
        ]
    return layers


def soil_legend(soil: satellite_view.Soil) -> dict[str, Any]:
    title = f"Soil moisture, {satellite_view.period_words(soil.period)} (FAO estimate from {satellite_view.SOIL_SQUARE_M} m readings)"
    if soil.first_estimate:
        title += ", first estimate"
    items = []
    for low, _ in satellite_view.SOIL_BANDS:
        label, colour = satellite_view.soil_band(low)
        items.append({"label": label, "color": colour})
    return {"title": title, "items": items}


def soil_style(layer_id: str, legend: dict[str, Any], bands: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    colour: list[Any] = ["step", ["coalesce", ["get", "moisture"], 0], satellite_view.SOIL_BANDS[0][1]]
    for low, band_colour in satellite_view.SOIL_BANDS[1:]:
        colour += [low, band_colour]
    # The map gives any layer that has lines and no fill an unseen fill of its own, so that an outline can be tapped
    # anywhere inside it (MapLibreMap.tsx). Here that would put soil moisture in the way of every tap on the plants.
    # A fill that matches no hexagon keeps the map from adding one.
    layers: list[dict[str, Any]] = [{
        "id": f"satellite-soil-taps-pass-{layer_id}", "type": "fill", "source": layer_id, "source-layer": MVT_LAYER_NAME,
        "filter": ["==", ["get", "h3_resolution"], -1], "paint": {"fill-opacity": 0}}]
    for band in bands:
        size = band["h3_resolution"]
        layers += [
            # the rim: the band's blue between two dark lines, so the palest band reads on any ground
            {**_in_band(layer_id, band), "id": f"satellite-soil-rim-under-{layer_id}-r{size}", "type": "line",
             "layout": {"line-join": "round"}, "paint": {"line-color": SOIL_INK, "line-width": 4.5}},
            {**_in_band(layer_id, band), "id": f"satellite-soil-rim-{layer_id}-r{size}", "type": "line",
             "metadata": {"mundi:legend": legend},
             "layout": {"line-join": "round"}, "paint": {"line-color": colour, "line-width": 2.5}},
            {**_in_band(layer_id, band, ["has", "reading"]), "id": f"satellite-soil-reading-{layer_id}-r{size}", "type": "symbol",
             "layout": {"text-field": ["get", "reading"], "text-font": LABEL_FONT, "text-size": 15,
                        "text-allow-overlap": True, "text-ignore-placement": True},
             "paint": {"text-color": SOIL_INK, "text-halo-color": "#ffffff", "text-halo-width": 2}},
        ]
    return layers


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
    """The view's layers on the map: plants first, soil moisture after, so that soil moisture's rims and numbers
    are drawn over the plants. A part the view does not hold gets no layer. A layer that cannot be saved is logged
    and left out: the facts are still told."""
    there = await _already_on_map(map_id, photo_layer_id)
    made: list[OnMap] = []
    plans: list[tuple[str, str, str, Any]] = []
    if seen.plants is not None:
        plans.append((PLANTS_KIND, f"Plants (NDVI) - {photo_name}", plants_stamp(seen.plants), seen.plants))
    if seen.soil is not None:
        plans.append((SOIL_KIND, f"Soil moisture - {photo_name}", soil_stamp(seen.soil), seen.soil))
    for kind, name, stamp, part in plans:
        old = there.get(kind)
        if old and old[2] == stamp:
            made.append(OnMap(old[0], old[1], kind, True, old[3]))
            continue
        if kind == PLANTS_KIND:
            drawn = satellite_view.plants_hexagons(part, seen.bbox)
            bands = zoom_bands(drawn["sizes"], drawn["latitude"], PLANTS_PIXELS_ACROSS)
            legend, maxzoom, extra = plants_legend(part), PLANTS_MAXZOOM, {"square_m": drawn["square_m"]}

            def style(layer_id: str, legend: dict[str, Any] = legend, bands: list[dict[str, Any]] = bands) -> list[dict[str, Any]]:
                return plants_style(layer_id, legend, bands)
        else:
            drawn = satellite_view.soil_hexagons(part, seen.bbox)
            bands = zoom_bands(drawn["sizes"], drawn["latitude"], SOIL_PIXELS_ACROSS)
            legend, maxzoom, extra = soil_legend(part), SOIL_MAXZOOM, {"square_m": satellite_view.SOIL_SQUARE_M, "period": part.period}

            def style(layer_id: str, legend: dict[str, Any] = legend, bands: list[dict[str, Any]] = bands) -> list[dict[str, Any]]:
                return soil_style(layer_id, legend, bands)
        try:
            saved = await save_analysis_layer(
                feature_collection=drawn["geojson"], user_uuid=user_uuid, map_id=map_id, project_id=project_id,
                layer_name=name, maxzoom=maxzoom, style_for=style, replaces=(old[0],) if old else (),
                metadata={"source": "satellite_view", "analysis_kind": kind, "source_layer_id": photo_layer_id,
                          "data_stamp": stamp, "h3_resolutions": drawn["sizes"], "zoom_resolution_map": bands, **extra},
            )
        except Exception:  # noqa: BLE001 - the facts are still told; the reader is told the layer is missing
            logger.exception("satellite layers: %s for photo %s could not be saved", kind, photo_layer_id)
            continue
        made.append(OnMap(saved.layer_id, name, kind, False, saved.bounds))
    return made
