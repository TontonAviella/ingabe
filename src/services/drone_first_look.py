"""Drone first look: when an RGB orthophoto finishes processing, Sage posts a short summary in the chat.

Nobody has to ask. The message says where the photo is, how big it is, how
green the field looks, and which part looks weakest, in the words of the
reader (farmer, insurer, agronomist or scientist; see insurance_engine
resolve_audience). Everything here is measured, not generated: one read of
the processed image at about 1024 pixels on the long side, GRVI per pixel
(src.services.grvi), and a 3 x 3 grid of the image (north-west ... south-east)
to say where the low-green pixels are. No LLM call, so uploads do not spend
the free model's daily requests.

Honest limits, said in the message: RGB has no near-infrared band, so this is
greenness, not crop health; roads, paths and roofs are "not green" too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional

import h3
import numpy as np
import rasterio
from rasterio.enums import Resampling

from src.services.grvi import LOW_GREEN, grvi, grvi_verdict
from src.services.h3_admin_index import admin_units_for_hexagon
from src.services.insurance_engine import resolve_audience
from src.structures import get_async_db_connection
from src.utils import get_async_s3_client, get_bucket_name

logger = logging.getLogger(__name__)

ZONES = (
    ("north-west", "north", "north-east"),
    ("west", "centre", "east"),
    ("south-west", "south", "south-east"),
)
SAMPLE_LONG_SIDE = 1024
MIN_ZONE_VALID = 0.05  # a zone with fewer valid pixels than this share is not judged
HOTSPOT_MARGIN = 0.15  # a zone stands out when its low-green share exceeds the field's by this much
HOTSPOT_MIN = 0.30  # ... and at least this share of it is low green


@dataclass(frozen=True)
class Zone:
    name: str
    low_share: float  # share of the zone's valid pixels below LOW_GREEN
    valid_share: float  # share of the zone that is image (not nodata)


@dataclass(frozen=True)
class Greenness:
    mean: float
    p10: float
    p90: float
    low_share: float
    valid_pct: float
    zones: list[Zone]
    sample_shape: tuple[int, int]

    @property
    def hotspots(self) -> list[Zone]:
        """Up to two zones clearly less green than the field as a whole, worst first."""
        cut = max(self.low_share + HOTSPOT_MARGIN, HOTSPOT_MIN)
        weak = [z for z in self.zones if z.valid_share >= MIN_ZONE_VALID and z.low_share >= cut]
        return sorted(weak, key=lambda z: z.low_share, reverse=True)[:2]

    @property
    def greenest(self) -> Optional[Zone]:
        """The zone clearly greener than the field as a whole, if one is."""
        good = [z for z in self.zones
                if z.valid_share >= MIN_ZONE_VALID and z.low_share <= self.low_share - HOTSPOT_MARGIN]
        return min(good, key=lambda z: z.low_share) if good else None

    @property
    def uniform(self) -> bool:
        return self.p90 - self.p10 <= 0.15


@dataclass(frozen=True)
class FirstLook:
    layer_name: str
    place: Optional[str]
    area_ha: Optional[float]
    resolution_cm: Optional[float]
    green: Greenness
    verdict: str = field(init=False)
    verdict_message: str = field(init=False)

    def __post_init__(self) -> None:
        level, message = grvi_verdict(self.green.mean)
        object.__setattr__(self, "verdict", level)
        object.__setattr__(self, "verdict_message", message)


def measure_greenness(red: Any, green: Any) -> Optional[Greenness]:
    """GRVI statistics for the whole image and per 3 x 3 zone; None when no pixel is valid."""
    g = grvi(red, green)
    valid = ~np.isnan(g)
    if not valid.any():
        return None
    values = g[valid]
    rows, cols = g.shape
    zones = []
    for i, names in enumerate(ZONES):
        for j, name in enumerate(names):
            block = g[i * rows // 3:(i + 1) * rows // 3, j * cols // 3:(j + 1) * cols // 3]
            block_valid = ~np.isnan(block)
            n = int(block_valid.sum())
            zones.append(Zone(
                name=name,
                low_share=float((block[block_valid] < LOW_GREEN).sum() / n) if n else 0.0,
                valid_share=n / block.size if block.size else 0.0,
            ))
    return Greenness(
        mean=float(values.mean()),
        p10=float(np.percentile(values, 10)),
        p90=float(np.percentile(values, 90)),
        low_share=float((values < LOW_GREEN).sum() / values.size),
        valid_pct=100.0 * values.size / g.size,
        zones=zones,
        sample_shape=(rows, cols),
    )


def place_name(units: dict[str, list[dict[str, Any]]]) -> Optional[str]:
    """'Kabarama cell, Ruhango sector, Ruhango district' from h3_admin_index units (largest overlap first)."""
    parts = [f"{units[level][0]['name']} {level}" for level in ("cell", "sector", "district") if units.get(level)]
    return ", ".join(parts) or None


# ---------------------------------------------------------------------------
# The message, per reader
# ---------------------------------------------------------------------------

_PLAIN_VERDICT = {
    "lush_canopy": "very green and dense",
    "healthy_canopy": "green and healthy",
    "moderate_canopy": "only moderately green (a young crop, or some stress)",
    "sparse_or_stressed": "thin or stressed",
    "bare_or_dry": "mostly bare or dry",
}


def _zones_text(zones: list[Zone]) -> str:
    return " and ".join(z.name for z in zones)


FIELD_MAX_HA = 10.0  # above this the photo shows an area with several fields, not one field


def _subject(look: FirstLook) -> str:
    return "field" if look.area_ha is not None and look.area_ha <= FIELD_MAX_HA else "area"


def _where(look: FirstLook) -> str:
    bits = [look.place or "your area"]
    if look.area_ha is not None:
        bits.append(f"about {look.area_ha:.1f} ha")
    return ", ".join(bits)


def compose(look: FirstLook, audience: str) -> str:
    """The chat message for one reader. Pure: everything comes from `look`."""
    g = look.green
    hot = g.hotspots
    pct_low = round(100 * g.low_share)
    res = f"{look.resolution_cm:.0f} cm per pixel" if look.resolution_cm else None

    subject = _subject(look)
    best = g.greenest
    if audience == "farmer":
        lines = [f"**Your drone photo is ready**: {_where(look)}.",
                 f"Most of the {subject} looks {_PLAIN_VERDICT[look.verdict]}."]
        if hot:
            part = "part looks" if len(hot) == 1 else "parts look"
            lines.append(f"The **{_zones_text(hot)}** {part} less green than the rest. "
                         "Walk there first: check for pests, disease, missing plants or dry soil.")
        elif g.uniform:
            lines.append(f"The {subject} looks even; no part stands out.")
        else:
            lines.append(f"Green and less-green patches are mixed all over the {subject}; no single part is worse "
                         "than the others.")
        if best and best not in hot:
            lines.append(f"The **{best.name}** part is the greenest.")
        lines.append("_This comes from the colours in the photo. Roads, paths and roofs also count as not green._")
        first = '"Show me the weak part on the map"' if hot else '"Show me the greenest and the weakest patches"'
        lines.append(f'You can ask me: {first} · "Has it rained enough here?" · '
                     '"How does it compare with my last photo?"')
        return "\n\n".join(lines)

    if audience == "insurance":
        head = " · ".join(b for b in [look.place, f"{look.area_ha:.1f} ha" if look.area_ha else None, res] if b)
        where = f", concentrated in the {_zones_text(hot)}" if hot else f", spread across the {subject}"
        return "\n\n".join([
            f"**Drone orthophoto received**: {head}.",
            f"Visible greenness: {_PLAIN_VERDICT[look.verdict]}. "
            f"**{pct_low}%** of the photographed area is low-green (possible damage, bare ground or non-crop){where}.",
            "_Not a loss assessment: colour only (RGB, no infrared). Confirm on the ground or with a "
            "multispectral flight before a claim decision._",
            'You can ask me: "Show the low-green area on the map" · "Compare with the previous flight" · '
            '"Rain in this area since planting"',
        ])

    zone_detail = ", ".join(f"{z.name} {round(100 * z.low_share)}%" for z in hot) if hot else "no zone stands out"
    if best:
        zone_detail += f"; greenest: {best.name} {round(100 * best.low_share)}%"
    lines = [
        f"**Drone orthophoto ready**: {_where(look)}" + (f" at {res}." if res else "."),
        f"- Canopy greenness (GRVI, RGB only): mean {g.mean:.3f}, p10 to p90 {g.p10:.3f} to {g.p90:.3f}. "
        f"{look.verdict_message}",
        f"- Low-green share (GRVI < {LOW_GREEN}): {pct_low}% overall; highest: {zone_detail}.",
        f"- Uniformity: {'fairly uniform' if g.uniform else 'patchy (some areas clearly greener than others)'}.",
        "_GRVI is about 70% as informative as NDVI for canopy stress; a multispectral (NIR) flight gives a "
        "firmer answer. Roads, paths and roofs read as low-green._",
    ]
    if audience == "scientist":
        rows, cols = g.sample_shape
        lines.append(
            f"Method: GRVI = (G - R) / (G + R) on bands 2 and 1 of the processed COG, read at {cols} x {rows} px "
            f"(average resampling, nodata masked; {g.valid_pct:.0f}% valid). Zones: 3 x 3 grid of the image; a zone "
            f"is flagged when its low-green share is at least {round(100 * HOTSPOT_MIN)}% and "
            f"{round(100 * HOTSPOT_MARGIN)} points above the field's."
        )
    lines.append('You can ask me: "Map the stress zones" · "Compare with satellite NDVI for this field" · '
                 '"What growth stage should the crop be at?"')
    return "\n".join(lines[:1]) + "\n\n" + "\n".join(lines[1:])


# ---------------------------------------------------------------------------
# Reading the layer and posting the message
# ---------------------------------------------------------------------------

def _resolution_cm(ds: Any, lat: float) -> Optional[float]:
    size = abs(ds.res[0])
    crs = ds.crs
    if crs is None:
        return None
    if crs.is_geographic:
        return size * 111_320 * math.cos(math.radians(lat)) * 100
    if crs.to_epsg() == 3857:  # web mercator metres shrink by cos(latitude) on the ground
        return size * math.cos(math.radians(lat)) * 100
    return size * 100


def _bbox_area_ha(bounds: list[float]) -> float:
    west, south, east, north = bounds
    lat = (south + north) / 2
    return (north - south) * 111.32 * (east - west) * 111.32 * math.cos(math.radians(lat)) * 100


async def look_at_layer(conn: Any, layer: dict[str, Any], cog_url: str) -> Optional[FirstLook]:
    """FirstLook for an RGB raster layer row (layer_id, name, bounds, metadata); None if not RGB or empty."""
    metadata = layer["metadata"]
    if (metadata.get("band_count") or 0) < 3:
        return None
    bounds = layer["bounds"]
    lat = (bounds[1] + bounds[3]) / 2 if bounds else 0.0

    def read() -> tuple[Optional[Greenness], Optional[float]]:
        with rasterio.open(cog_url) as ds:
            factor = max(1, max(ds.width, ds.height) // SAMPLE_LONG_SIDE)
            shape = (max(1, ds.height // factor), max(1, ds.width // factor))
            red = ds.read(1, out_shape=shape, resampling=Resampling.average, masked=True)
            green = ds.read(2, out_shape=shape, resampling=Resampling.average, masked=True)
            return measure_greenness(red, green), _resolution_cm(ds, lat)

    greenness, resolution_cm = await asyncio.wait_for(asyncio.to_thread(read), timeout=120)
    if greenness is None:
        return None
    place = None
    if bounds:
        try:
            cell = h3.latlng_to_cell(lat, (bounds[0] + bounds[2]) / 2, 9)
            place = place_name((await admin_units_for_hexagon(conn, cell))["units"])
        except Exception:  # noqa: BLE001 - outside Rwanda or index not built: the message just says "your area"
            logger.info("first look: no admin names for layer %s", layer["layer_id"], exc_info=True)
    area_ha = round(_bbox_area_ha(bounds) * greenness.valid_pct / 100, 1) if bounds else None
    return FirstLook(layer_name=layer["name"], place=place, area_ha=area_ha,
                     resolution_cm=resolution_cm, green=greenness)


async def conversation_for_upload(conn: Any, project_id: str, user_id: str, requested: Optional[int],
                                  title: str) -> int:
    """The chat the first look goes to: the one open at upload, else the latest, else a new one."""
    if requested is not None:
        ok = await conn.fetchval(
            "SELECT id FROM conversations WHERE id = $1 AND project_id = $2 AND owner_uuid = $3 "
            "AND soft_deleted_at IS NULL", requested, project_id, user_id)
        if ok is not None:
            return int(ok)
    latest = await conn.fetchval(
        "SELECT id FROM conversations WHERE project_id = $1 AND owner_uuid = $2 AND soft_deleted_at IS NULL "
        "ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT 1", project_id, user_id)
    if latest is not None:
        return int(latest)
    return int(await conn.fetchval(
        "INSERT INTO conversations (project_id, owner_uuid, title) VALUES ($1, $2, $3) RETURNING id",
        project_id, user_id, title))


async def post_first_look(layer_id: str, map_id: str, user_id: str, partner_id: Optional[str],
                          conversation_id: int, wait_s: int = 600) -> Optional[str]:
    """Background task after the COG step: post the first look into the chat. Returns the text, or None.

    Never raises: a failed first look must not disturb the upload.
    """
    try:
        layer = None
        for _ in range(max(1, wait_s // 15)):
            async with get_async_db_connection() as conn:
                row = await conn.fetchrow(
                    "SELECT layer_id, name, type, bounds, metadata FROM map_layers WHERE layer_id = $1", layer_id)
            if row is None or row["type"] != "raster":
                return None
            metadata = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"] or "{}")
            status = metadata.get("cog_status")
            if status == "ready" and metadata.get("cog_key"):
                layer = {**dict(row), "metadata": metadata}
                break
            if status == "failed":
                return None
            await asyncio.sleep(15)
        if layer is None:
            logger.info("first look: COG for %s not ready in time", layer_id)
            return None

        s3 = await get_async_s3_client()
        cog_url = await s3.generate_presigned_url(
            "get_object", Params={"Bucket": get_bucket_name(), "Key": layer["metadata"]["cog_key"]}, ExpiresIn=900)
        async with get_async_db_connection() as conn:
            look = await look_at_layer(conn, layer, cog_url)
            if look is None:
                return None
            audience = await resolve_audience(conn, None, user_id, partner_id)
            text = compose(look, audience)
            await conn.execute(
                "INSERT INTO chat_completion_messages (map_id, sender_id, message_json, conversation_id) "
                "VALUES ($1, $2, $3, $4)",
                map_id, user_id, json.dumps({"role": "assistant", "content": text}), conversation_id)
            await conn.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = $1",
                               conversation_id)
        logger.info("first look posted for %s (%s, %s)", layer_id, audience, look.verdict)
        return text
    except Exception:  # noqa: BLE001 - logged; the upload itself already succeeded
        logger.exception("first look failed for layer %s", layer_id)
        return None
