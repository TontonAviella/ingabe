"""A health index for each field, from a photo that has a near-infrared band.

What each index answers to, from how a leaf treats light:
- NDVI = (NIR - Red) / (NIR + Red). Chlorophyll takes in red light and the leaf's inner structure throws back
  near-infrared, so it rises with the amount of living leaf over the ground. It stops rising in a dense crop.
- NDRE = (NIR - RedEdge) / (NIR + RedEdge). Red-edge light goes deeper into the leaves, so it keeps answering to
  how much chlorophyll (and so nitrogen) a dense crop holds after NDVI has stopped.
- GNDVI = (NIR - Green) / (NIR + Green). Answers to chlorophyll too, from the green band.

A thermal band gives each field's surface temperature. A crop with enough water cools itself by letting water
out of its leaves, so a field warmer than its neighbours can be short of water; it can as well be bare soil or a
thinner crop, and sun, wind and the hour change every field at once. So a field's temperature is said beside the
middle of the fields shown with it, from the same flight, and never as a verdict.

Zones inside a field: where the field is large enough and uneven enough, it is split into three zones, the lower,
middle and upper third of its own index (NDRE when the camera has a red-edge band, since that answers to the
nitrogen in the leaves; NDVI otherwise). A zone says where the leaves hold less; it does not say why, and it gives
no rate: a rate needs the soil, the crop and its stage, which is the agronomist's part.

Nothing here is a verdict: no cut-off between healthy and stressed has been tested, and a lower index can be a
younger crop, another crop, or bare ground. So a field gets its index and its place among the photo's fields, and
on a photo of raw camera values (not reflectance) only its place: that order does not depend on calibration, the
number does (photo_bands.looks_like_reflectance).

Measured once per outline and kept beside the photo; an outline that changes is a new outline.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask, shapes, sieve
from rasterio.windows import Window, from_bounds
from shapely.geometry import mapping, shape
from scipy import ndimage
from shapely.ops import transform as reproject, unary_union
from shapely.validation import make_valid

from src.services import drone_plots, ground_area, photo_bands, stored_files
from src.services.gdal_http import GDAL_HTTP_TIMEOUTS

logger = logging.getLogger(__name__)

METHOD = "field-health-1"  # change when what is measured changes: kept records of another method are not read
STORE_PREFIX = "field_health/v1"
STATS_M_PER_PX = 0.5  # the photo is read at about this ground size: a field's mean does not need each leaf
MAX_READ_PIXELS = 16_000_000
MIN_PIXELS = 25  # fewer readable pixels than this inside an outline is no measurement
MIN_MEASURED_SHARE = 0.5  # ... and so is an outline less than half on the photo
MIN_ZONED_M2 = 1000.0  # a smaller field is treated as one piece: a spreader cannot follow zones inside it
ZONE_SMOOTH_M = 5.0  # the index is averaged over about this width first, so single plants do not make zones
MIN_ZONE_PATCH_M2 = 100.0  # a patch smaller than this joins the zone around it
EVEN_SPREAD = 0.05  # a field whose index varies less than this (10th to 90th percentile) is even: one zone.
# These four are choices made from how a spreader works, not measured against yield.
ZONES = ("low", "middle", "high")
MIN_RANKED = 3  # a field's place among fewer fields than this is not said


@dataclass(frozen=True)
class FieldHealth:
    said: Optional[str]  # as every surface says it; None when there is nothing honest to say
    ndvi: Optional[float] = None  # the numbers only on a photo of reflectance
    ndre: Optional[float] = None
    gndvi: Optional[float] = None
    temperature_said: Optional[str] = None  # from a thermal band whose values are a temperature
    temp_c: Optional[float] = None
    # zones inside the field (None: too small to zone, or not measured; (): even, one rate fits it)
    zones: Optional[tuple[dict[str, Any], ...]] = None
    zone_index: Optional[str] = None  # "ndre" or "ndvi": what the zones are thirds of


KEY_DECIMALS = 6  # about 0.1 m: an outline that went to the photo's coordinates and back is still the same outline


def _rounded(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, KEY_DECIMALS)
    if isinstance(value, (list, tuple)):
        return [_rounded(v) for v in value]
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    return value


def key_of(geometry: dict[str, Any]) -> str:
    """A field's outline as a key: the same outline always measures the same, on every surface."""
    text = json.dumps(_rounded(geometry), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def _store_key(photo_key: str) -> str:
    return f"{STORE_PREFIX}/{hashlib.sha256(photo_key.encode()).hexdigest()[:32]}.json"


OVERVIEW_LONG_SIDE = 512  # the whole photo is looked at once, this small, to tell reflectance from raw values


def _mean(index: Any, inside: np.ndarray, whole: float) -> Optional[float]:
    """The index's mean inside an outline; `whole` is how many pixels the whole outline would cover, so a field
    mostly off the photo, or mostly empty photo, is not measured from the part that happens to be readable."""
    values = index[inside]
    values = values[~np.isnan(values)]
    if values.size < MIN_PIXELS or values.size < MIN_MEASURED_SHARE * whole:
        return None
    return round(float(values.mean()), 4)


def _zones(index: Any, inside: np.ndarray, transform: Affine, to_wgs84: Any) -> Optional[list[dict[str, Any]]]:
    """The field's zones as [{"zone", "area_m2", "geometry" (WGS84)}], lowest first; [] for an even field (one
    rate fits it); None for a field too small to zone. Thirds of the field's own index after smoothing."""
    pixel_m = float(np.sqrt(abs(transform.a * transform.e)))
    valid = inside & ~np.isnan(index)
    if valid.sum() * pixel_m ** 2 < MIN_ZONED_M2:
        return None
    values = index[valid]
    p10, p90 = np.percentile(values, [10, 90])
    if p90 - p10 < EVEN_SPREAD:
        return []
    width = max(1, int(round(ZONE_SMOOTH_M / pixel_m)))
    filled = np.where(valid, index, float(values.mean())).astype("float32")
    smooth = ndimage.uniform_filter(filled, size=width, mode="nearest")
    low, high = np.percentile(smooth[valid], [100 / 3, 200 / 3])
    classes = np.where(smooth < low, 1, np.where(smooth > high, 3, 2)).astype("uint8")
    classes = sieve(classes, size=max(2, int(MIN_ZONE_PATCH_M2 / pixel_m ** 2)), mask=valid.astype("uint8"))
    out = []
    for number, zone in enumerate(ZONES, start=1):
        pieces = [shape(geom) for geom, value in shapes(classes, mask=valid & (classes == number), transform=transform)
                  if value == number]
        if not pieces:
            continue
        merged = reproject(to_wgs84, unary_union(pieces).simplify(pixel_m))
        area = ground_area.area_ha(merged) * 10_000
        if area > 0:
            out.append({"zone": zone, "area_m2": round(area), "geometry": mapping(merged)})
    return out if len(out) > 1 else []


def _one(ds: Any, bands: photo_bands.Bands, outline: Any, to_wgs84: Any) -> dict[str, Any]:
    """One outline (in the photo's CRS), read on its own window at its own size: what a field measures does not
    depend on which other fields were asked for with it."""
    nothing: dict[str, Any] = {"ndvi": None, "ndre": None, "gndvi": None, "temp_c": None}
    left, bottom = max(outline.bounds[0], ds.bounds.left), max(outline.bounds[1], ds.bounds.bottom)
    right, top = min(outline.bounds[2], ds.bounds.right), min(outline.bounds[3], ds.bounds.top)
    if right <= left or top <= bottom:
        return nothing
    w = from_bounds(left, bottom, right, top, ds.transform)
    col0, row0 = max(0, int(np.floor(w.col_off))), max(0, int(np.floor(w.row_off)))
    col1 = min(ds.width, int(np.ceil(w.col_off + w.width)))
    row1 = min(ds.height, int(np.ceil(w.row_off + w.height)))
    if col1 <= col0 or row1 <= row0:
        return nothing
    window = Window(col0, row0, col1 - col0, row1 - row0)
    factor = max(1.0, STATS_M_PER_PX / drone_plots._photo_m_per_px(ds))
    factor = max(factor, float(np.sqrt(window.width * window.height / MAX_READ_PIXELS)))
    rows, cols = max(1, int(window.height / factor)), max(1, int(window.width / factor))

    def band(number: int) -> Any:
        return ds.read(number, window=window, out_shape=(rows, cols), resampling=Resampling.average, masked=True)

    transform = ds.window_transform(window) * Affine.scale(window.width / cols, window.height / rows)
    inside = geometry_mask([mapping(outline)], out_shape=(rows, cols), transform=transform, invert=True)
    if not inside.any():
        return nothing
    whole = outline.area / abs(transform.a * transform.e)
    out = dict(nothing)
    if bands.nir is not None and bands.red is not None:
        nir = band(bands.nir)
        out["ndvi"] = _mean(photo_bands.ndvi(nir, band(bands.red)), inside, whole)
        out["ndre"] = _mean(photo_bands.ndre(nir, band(bands.red_edge)), inside, whole) if bands.red_edge else None
        out["gndvi"] = _mean(photo_bands.index(nir, band(bands.green)), inside, whole) if bands.green else None
        by = "ndre" if bands.red_edge else "ndvi"
        index = photo_bands.ndre(nir, band(bands.red_edge)) if bands.red_edge else photo_bands.ndvi(nir, band(bands.red))
        if out[by] is not None:
            out["zone_index"], out["zones"] = by, _zones(index, inside, transform, to_wgs84)
    if bands.thermal is not None:
        degrees, _scale = photo_bands.celsius(band(bands.thermal))
        mean = _mean(degrees, inside, whole) if degrees is not None else None
        out["temp_c"] = round(mean, 1) if mean is not None else None
    return out


def measure(cog_url: str, outlines: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """{"nir": whether the photo has a band to measure from (a near-infrared band it names, or a thermal one), "reflectance": whether the whole photo holds
    reflectance, "fields": {key: {"ndvi", "ndre", "gndvi"}}} for outlines in WGS84 (blocking). A field that
    cannot be measured has None for each index."""
    with rasterio.Env(**GDAL_HTTP_TIMEOUTS), rasterio.open(cog_url) as ds:
        bands = photo_bands.of(ds)
        has_index = bands.nir is not None and bands.red is not None
        if not has_index and bands.thermal is None:
            return {"nir": False, "reflectance": False, "fields": {}}
        scale = max(1, max(ds.width, ds.height) // OVERVIEW_LONG_SIDE)
        small = (max(1, ds.height // scale), max(1, ds.width // scale))
        reflectance = has_index and photo_bands.looks_like_reflectance(
            ds.read(bands.nir, out_shape=small, resampling=Resampling.average, masked=True),
            ds.read(bands.red, out_shape=small, resampling=Resampling.average, masked=True))
        to_photo = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        to_wgs84 = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True).transform
        fields: dict[str, dict[str, Any]] = {}
        for key, geometry in outlines.items():
            fields[key] = {"ndvi": None, "ndre": None, "gndvi": None, "temp_c": None}
            try:
                outline = reproject(to_photo, make_valid(shape(geometry)))
                if not outline.is_empty and outline.area > 0:
                    fields[key] = _one(ds, bands, outline, to_wgs84)
            except Exception:  # this outline, not the photo: it is kept as not measured
                logger.warning("field health: an outline could not be measured", exc_info=True)
        return {"nir": True, "reflectance": reflectance, "fields": fields}


def _place(below: int, above: int, tied: bool, total: int) -> str:
    """A field's place among the fields shown with it, counted from the lowest index."""
    words = (f"lowest of {total} fields" if below == 0 else f"highest of {total} fields" if above == 0 else None)
    if words is None:
        rank = below + 1
        ordinal = f"{rank}{'th' if 10 <= rank % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(rank % 10, 'th')}"
        words = f"{ordinal} lowest of {total} fields"
    return f"{words}, level with another" if tied else words


def said(fields: dict[str, dict[str, Any]], reflectance: bool) -> dict[str, FieldHealth]:
    """Each measured field as it is said everywhere. Pure. A place is among the fields given (the fields the
    reader is looking at), on the index as it is shown (two decimals), so fields that show the same share a place."""
    shown = {key: round(values["ndvi"], 2) for key, values in fields.items() if values.get("ndvi") is not None}
    out: dict[str, FieldHealth] = {}
    for key, value in shown.items():
        place = None
        if len(shown) >= MIN_RANKED:
            below = sum(1 for other in shown.values() if other < value)
            above = sum(1 for other in shown.values() if other > value)
            place = None if below == 0 and above == 0 else _place(
                below, above, len(shown) - below - above > 1, len(shown))
        values = fields[key]
        if reflectance:
            words = f"health index (NDVI) {value:.2f}" + (f", {place}" if place else "")
            out[key] = FieldHealth(said=words, ndvi=values["ndvi"], ndre=values.get("ndre"), gndvi=values.get("gndvi"))
        else:
            out[key] = FieldHealth(said=f"health index (NDVI): {place}" if place else None)
    warm = {key: values["temp_c"] for key, values in fields.items() if values.get("temp_c") is not None}
    middle = float(np.median(list(warm.values()))) if len(warm) >= MIN_RANKED else None
    for key, degrees in warm.items():
        words = f"surface {degrees:.1f} °C"
        if middle is not None:
            gap = round(degrees - middle, 1)
            words += (f", level with the middle of {len(warm)} fields" if gap == 0 else
                      f", {abs(gap):.1f} °C {'warmer' if gap > 0 else 'cooler'} than the middle of {len(warm)} fields")
        out[key] = dataclasses.replace(out.get(key, FieldHealth(said=None)), temperature_said=words, temp_c=degrees)
    for key, values in fields.items():
        if key in out and isinstance(values.get("zones"), list):
            out[key] = dataclasses.replace(out[key], zones=tuple(values["zones"]), zone_index=values.get("zone_index"))
    return out


RETRY_SECONDS = 600  # a photo that could not be read is not opened again sooner than this
_tasks: dict[str, "asyncio.Task[None]"] = {}  # one measurement at a time per photo, in this process
_failed_at: dict[str, float] = {}


async def _record(s3: Any, bucket: str, photo_key: str) -> dict[str, Any]:
    record = await stored_files.load_json(s3, bucket, _store_key(photo_key))
    if not isinstance(record, dict) or record.get("method") != METHOD or not isinstance(record.get("fields"), dict):
        return {"method": METHOD, "fields": {}}
    return record


async def _measure_missing(s3: Any, bucket: str, photo_key: str, cog_url: str, outlines: dict[str, Any]) -> None:
    """Measure outlines not kept yet and keep them. Runs beside the request: nobody waits for a photo to be read."""
    try:
        record = await _record(s3, bucket, photo_key)
        missing = {key: geometry for key, geometry in outlines.items() if key not in record["fields"]}
        if record.get("nir") is False or not missing:
            return
        measured = await asyncio.to_thread(measure, cog_url, missing)
        record = await _record(s3, bucket, photo_key)  # another worker may have kept more meanwhile
        kept = {"method": METHOD, "nir": measured["nir"],
                # a fact of the whole photo, judged on the whole photo: set once, the same for every reader of it
                "reflectance": record["reflectance"] if "reflectance" in record else measured["reflectance"],
                "fields": {**record["fields"], **measured["fields"]}}
        await s3.put_object(Bucket=bucket, Key=_store_key(photo_key), Body=json.dumps(kept).encode(),
                            ContentType="application/json")
        _failed_at.pop(photo_key, None)
    except Exception:  # said nowhere: the fields are shown without a health index, and it is tried again later
        _failed_at[photo_key] = asyncio.get_running_loop().time()
        logger.exception("field health: the photo %s could not be measured", photo_key)
    finally:
        _tasks.pop(photo_key, None)


async def of_features(s3: Any, bucket: str, photo_key: str, cog_url: str,
                      features: list[dict[str, Any]]) -> dict[str, FieldHealth]:
    """The health of each feature's outline, by key_of(geometry), from what is kept: {} for a photo with no
    near-infrared band. Never reads the photo itself: outlines not measured yet are measured beside the request
    (seconds) and are there the next time this is asked, so no screen waits for it."""
    outlines = {key_of(f["geometry"]): f["geometry"] for f in features if isinstance(f.get("geometry"), dict)}
    if not outlines:
        return {}
    record = await _record(s3, bucket, photo_key)
    if record.get("nir") is False:
        return {}
    if any(key not in record["fields"] for key in outlines) and photo_key not in _tasks:
        loop = asyncio.get_running_loop()
        if loop.time() - _failed_at.get(photo_key, -RETRY_SECONDS) >= RETRY_SECONDS:
            _tasks[photo_key] = loop.create_task(_measure_missing(s3, bucket, photo_key, cog_url, outlines))
    return said({key: record["fields"][key] for key in outlines if key in record["fields"]},
                bool(record.get("reflectance")))


async def settled(photo_key: str) -> None:
    """Wait for this photo's measurement beside the request, if one is running (tests; a job that wants it now)."""
    task = _tasks.get(photo_key)
    if task is not None:
        await task


TEMPERATURE_NOTE = ("A warmer field can be bare soil, a thinner crop, or a crop short of water. Sun, wind and the "
                    "hour change every field, so compare fields from one flight only.")
SHORT_NOTE = "A lower index can be a younger crop, another crop, or bare ground. No cut-off has been tested."
NOTE = ("The health index (NDVI) comes from the photo's own red and near-infrared bands. It is given with no verdict: "
        "no cut-off between healthy and stressed has been tested, and a lower index can be a younger crop, another "
        "crop, or bare ground.")


ZONE_GROUP = {"low": "least_green", "middle": "mostly_soil", "high": "other"}  # the overlay's three looks
ZONE_SAID = {"low": "lower third", "middle": "middle third", "high": "upper third"}


def zone_features(named: list[tuple[int, str, FieldHealth]]) -> dict[str, Any]:
    """The zones of the given fields ((number, name, health)) as one GeoJSON file for the map and the agronomist:
    one feature per zone, with the field it is in, its size, and an empty rate to fill in."""
    features = []
    for number, name, health in named:
        for zone in health.zones or ():
            features.append({"type": "Feature", "geometry": zone["geometry"], "properties": {
                "number": number, "field": name, "zone": zone["zone"], "zone_said": ZONE_SAID[zone["zone"]],
                "area_m2": zone["area_m2"], "index": (health.zone_index or "").upper(), "rate": None,
                "group": ZONE_GROUP[zone["zone"]],
                "label": f"{name} · {ZONE_SAID[zone['zone']]} · {zone['area_m2']:,} m²"}})
    return {"type": "FeatureCollection", "features": features}
