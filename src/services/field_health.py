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

A thermal photo is often its own file: one band, flown with another camera than the colour photo the fields were
outlined on. Such a file of the same place in the same project gives the fields their temperature (`with_thermal`).
It is taken as thermal when the file says so (a band described as thermal, or a temperature unit), or when the
reader named the layer so ("thermal", "LWIR", "TIR", "temperature"); then, as always, degrees are said only if the
file states its unit.

The bands of one flight often come as one file each ("result_Red.tif", "result_NIR.tif": how DJI Terra and
others deliver). Uploaded into the project of the colour photo the fields were outlined on, such files of the same
place are read together as one photo (`with_bands`): each file's band is the word its name ends with, the files
are laid over each other as they are, and everything above is then measured as from one file.

Measured once per outline and kept beside the photo; an outline that changes is a new outline.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import numpy as np
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask, shapes, sieve
from rasterio.windows import Window, from_bounds
from shapely.errors import ShapelyError
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
MAX_READ_PIXELS = 4_000_000
MIN_PIXELS = 25  # fewer readable pixels than this inside an outline is no measurement
MIN_MEASURED_SHARE = 0.5  # ... and so is an outline less than half on the photo
MIN_ZONED_M2 = 1000.0  # a smaller field is treated as one piece: a spreader cannot follow zones inside it
ZONE_SMOOTH_M = 5.0  # the index is averaged over about this width first, so single plants do not make zones
MIN_ZONE_PATCH_M2 = 100.0  # a patch smaller than this joins the zone around it
EVEN_SPREAD = 0.05  # a field whose index varies less than this (10th to 90th percentile) is too even to split.
# These four are choices made from how a spreader works, not measured against yield.
ZONES = ("low", "middle", "high")
LEVEL_INDEX = 0.05  # fields whose index lies within this of each other are said to be level: on the first real
# photo (sunflower at full cover, 2026-11-16) nine squares of one even field differed by 0.01. A choice, not tested.
MIN_RANKED = 3  # a field's place among fewer fields than this is not said


@dataclass(frozen=True)
class FieldHealth:
    said: Optional[str]  # as every surface says it; None when there is nothing honest to say
    ndvi: Optional[float] = None  # the numbers only on a photo of reflectance
    ndre: Optional[float] = None
    gndvi: Optional[float] = None
    temperature_said: Optional[str] = None  # from a thermal band whose values are a temperature
    temp_c: Optional[float] = None
    temperature_from: Optional[str] = None  # the thermal photo it was measured on, when not this photo itself
    index_from: Optional[str] = None  # the band files the index was measured on, when not this photo itself
    # zones inside the field (None: too small to zone, or not measured; (): even, one rate fits it)
    bare_m2: Optional[int] = None  # ground inside the field that reads as bare, left out of its zones
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


def _store_key(photo_key: str, named_thermal: bool = False) -> str:
    """Where a photo's measurements are kept; a one-band file read as thermal on the reader's naming is kept apart
    from the same file read on its own statements."""
    return f"{STORE_PREFIX}/{hashlib.sha256(photo_key.encode()).hexdigest()[:32]}{'-named' if named_thermal else ''}.json"


OVERVIEW_LONG_SIDE = 512  # the whole photo is looked at once, this small, to tell reflectance from raw values
SOIL_NDVI = 0.2  # on reflectance, ground under this is bare soil, shadow or road, not crop: the usual soil line,
# taken from how soil and leaves reflect and not yet checked on these photos. It is left out of a field's zones.
BATCH = 20  # outlines measured between two saves: a stop loses at most this many
MAX_KEPT = 1500  # outlines kept per photo; the oldest are dropped past this
_EMPTY: dict[str, Any] = {"ndvi": None, "ndre": None, "gndvi": None, "temp": None, "zones": None, "bare_m2": None,
                          "zone_index": None}


def _mean(index: Any, inside: np.ndarray, whole: float) -> Optional[float]:
    """The index's mean inside an outline; `whole` is how many pixels the whole outline would cover, so a field
    mostly off the photo, or mostly empty photo, is not measured from the part that happens to be readable."""
    values = index[inside]
    values = values[~np.isnan(values)]
    if values.size < MIN_PIXELS or values.size < MIN_MEASURED_SHARE * whole:
        return None
    return round(float(values.mean()), 4)


def _zones(index: Any, crop: np.ndarray, pixel_m: float, transform: Affine,
           to_wgs84: Any) -> Optional[list[dict[str, Any]]]:
    """The zones of the part of a field under crop (`crop`: inside the outline, readable, not bare) as
    [{"zone", "area_m2", "geometry" (WGS84)}], lowest first; [] for a part too even to split; None when it is too
    small to zone or does not split into three. `pixel_m`: a pixel's side on the ground, in metres."""
    valid = crop & ~np.isnan(index)
    if valid.sum() * pixel_m ** 2 < MIN_ZONED_M2:
        return None
    values = index[valid]
    p10, p90 = np.percentile(values, [10, 90])
    if p90 - p10 < EVEN_SPREAD:
        return []
    width = max(1, int(round(ZONE_SMOOTH_M / pixel_m)))
    weight = ndimage.uniform_filter(valid.astype("float32"), size=width, mode="constant")
    total = ndimage.uniform_filter(np.where(valid, index, 0).astype("float32"), size=width, mode="constant")
    with np.errstate(divide="ignore", invalid="ignore"):
        smooth = np.where(weight > 0, total / weight, np.nan)  # the mean of the crop pixels around, and only those
    low, high = np.percentile(smooth[valid], [100 / 3, 200 / 3])
    classes = np.where(smooth < low, 1, np.where(smooth > high, 3, 2)).astype("uint8")
    classes = sieve(classes, size=max(2, int(MIN_ZONE_PATCH_M2 / pixel_m ** 2)), mask=valid.astype("uint8"))
    out = []
    for number, zone in enumerate(ZONES, start=1):
        pieces = [shape(geom) for geom, value in shapes(classes, mask=valid & (classes == number), transform=transform)
                  if value == number]
        if not pieces:
            return None  # not three zones: nothing is drawn
        merged = reproject(to_wgs84, unary_union(pieces).simplify(abs(transform.a)))
        area = ground_area.area_ha(merged) * 10_000
        if area <= 0:
            return None
        out.append({"zone": zone, "area_m2": round(area), "geometry": mapping(merged)})
    return out


def _one(ds: Any, bands: photo_bands.Bands, outline: Any, to_wgs84: Any, photo: dict[str, Any]) -> dict[str, Any]:
    """One outline (in the photo's CRS), read on its own window at its own size: what a field measures does not
    depend on which other fields were asked for with it. `photo`: the facts of the whole photo (`_photo_facts`)."""
    left, bottom = max(outline.bounds[0], ds.bounds.left), max(outline.bounds[1], ds.bounds.bottom)
    right, top = min(outline.bounds[2], ds.bounds.right), min(outline.bounds[3], ds.bounds.top)
    if right <= left or top <= bottom:
        return dict(_EMPTY)
    w = from_bounds(left, bottom, right, top, ds.transform)
    col0, row0 = max(0, int(np.floor(w.col_off))), max(0, int(np.floor(w.row_off)))
    col1 = min(ds.width, int(np.ceil(w.col_off + w.width)))
    row1 = min(ds.height, int(np.ceil(w.row_off + w.height)))
    if col1 <= col0 or row1 <= row0:
        return dict(_EMPTY)
    window = Window(col0, row0, col1 - col0, row1 - row0)
    photo_m = drone_plots._photo_m_per_px(ds)
    factor = max(1.0, STATS_M_PER_PX / photo_m)
    factor = max(factor, float(np.sqrt(window.width * window.height / MAX_READ_PIXELS)))
    rows, cols = max(1, int(window.height / factor)), max(1, int(window.width / factor))
    pixel_m = photo_m * window.width / cols  # a read pixel's side on the ground

    def raw(number: int) -> Any:
        return ds.read(number, window=window, out_shape=(rows, cols), resampling=Resampling.average, masked=True)

    def band(number: int) -> Any:  # a band of light, on the scale the file states for it
        return _on_its_scale(ds, number, raw(number))

    transform = ds.window_transform(window) * Affine.scale(window.width / cols, window.height / rows)
    inside = geometry_mask([mapping(outline)], out_shape=(rows, cols), transform=transform, invert=True)
    out = dict(_EMPTY)
    if not inside.any():
        return out
    whole = outline.area / abs(transform.a * transform.e)
    if photo["index"]:
        nir, red = band(bands.nir), band(bands.red)  # type: ignore[arg-type]
        red_edge = band(bands.red_edge) if bands.red_edge else None
        ndvi = photo_bands.ndvi(nir, red)
        ndre = photo_bands.ndre(nir, red_edge) if red_edge is not None else None
        out["ndvi"] = _mean(ndvi, inside, whole)
        out["ndre"] = _mean(ndre, inside, whole) if ndre is not None else None
        out["gndvi"] = _mean(photo_bands.index(nir, band(bands.green)), inside, whole) if bands.green else None
        if photo["reflectance"] and out["ndvi"] is not None:  # zones mean something on reflectance only
            try:
                seen = inside & ~np.isnan(ndvi)
                bare = seen & (ndvi < SOIL_NDVI)
                out["zone_index"] = "ndre" if ndre is not None else "ndvi"
                out["zones"] = _zones(ndre if ndre is not None else ndvi, seen & ~bare, pixel_m, transform, to_wgs84)
                out["bare_m2"] = round(float(bare.sum()) * pixel_m ** 2)
            except Exception:  # the zones of this field only: its index is still kept
                logger.warning("field health: the zones of a field could not be drawn", exc_info=True)
                out["zones"] = out["bare_m2"] = None
    if photo["thermal"]:
        try:
            degrees = photo_bands.temperature(raw(bands.thermal), photo["thermal_unit"],  # type: ignore[arg-type]
                                              photo["thermal_scale"], photo["thermal_offset"], photo["thermal_whole"])
            mean = _mean(degrees, inside, whole)
            out["temp"] = round(mean, 2) if mean is not None else None
        except Exception:  # the temperature of this field only
            logger.warning("field health: the temperature of a field could not be read", exc_info=True)
    return out


def thermal_band(ds: Any, bands: photo_bands.Bands, named_thermal: bool = False) -> Optional[int]:
    """The photo's thermal band: one it describes as thermal; or, in a file of one band, that band when the file
    states a temperature unit for it or the reader named the layer as thermal."""
    if bands.thermal is not None:
        return bands.thermal
    if int(ds.count) != 1:
        return None
    if photo_bands.thermal_unit(ds.units[0], ds.descriptions[0]):
        return 1
    if named_thermal:  # the reader's name is believed only when the values could be temperatures
        scale = max(1, max(ds.width, ds.height) // OVERVIEW_LONG_SIDE)
        small = (max(1, ds.height // scale), max(1, ds.width // scale))
        if photo_bands.could_be_temperature(ds.read(1, out_shape=small, resampling=Resampling.average, masked=True)):
            return 1
    return None


def _on_its_scale(ds: Any, number: int, values: Any) -> Any:
    """A band's values with the scale and offset the file states for it applied (reflectance kept as whole numbers
    with "scale 0.0001" becomes 0 to 1). A file that states neither is returned as it is."""
    scale, offset = float(ds.scales[number - 1] or 1.0), float(ds.offsets[number - 1] or 0.0)
    if scale == 1.0 and offset == 0.0:
        return values
    return np.ma.asarray(values, dtype="float32") * scale + offset


def _photo_facts(ds: Any, bands: photo_bands.Bands) -> dict[str, Any]:
    """What is true of the whole photo, judged on the whole photo once: whether an index can be measured, whether
    its values are reflectance, and what its thermal band is in."""
    index = bands.nir is not None and bands.red is not None
    scale = max(1, max(ds.width, ds.height) // OVERVIEW_LONG_SIDE)
    small = (max(1, ds.height // scale), max(1, ds.width // scale))
    reflectance = index and photo_bands.looks_like_reflectance(
        _on_its_scale(ds, bands.nir, ds.read(bands.nir, out_shape=small, resampling=Resampling.average, masked=True)),
        _on_its_scale(ds, bands.red, ds.read(bands.red, out_shape=small, resampling=Resampling.average, masked=True)))
    t = bands.thermal
    # an 8-bit thermal band is a picture of colours or a stretched preview: it holds no temperature
    thermal = t is not None and str(ds.dtypes[t - 1]) != "uint8"
    unit = photo_bands.thermal_unit(ds.units[t - 1], ds.descriptions[t - 1]) if thermal else None
    t_scale = float(ds.scales[t - 1]) if thermal else 1.0
    t_offset = float(ds.offsets[t - 1]) if thermal else 0.0
    whole = bool(thermal and np.dtype(ds.dtypes[t - 1]).kind in "iu")
    if unit is not None and not photo_bands.unit_fits(
            ds.read(t, out_shape=small, resampling=Resampling.average, masked=True), unit, t_scale, t_offset, whole):
        unit = None  # the values contradict the stated unit: no degrees, only which field is warmer
    return {"index": index, "reflectance": bool(reflectance), "thermal": thermal, "thermal_unit": unit,
            "thermal_scale": t_scale, "thermal_offset": t_offset, "thermal_whole": whole}


BAND_DESCRIPTIONS = {"blue": "Blue", "green": "Green", "red": "Red", "red_edge": "RedEdge", "nir": "NIR"}


class NotOnePhoto(ValueError):
    """The band files are not the bands of one photo: different grids or different kinds of numbers."""


def _stack(band_urls: dict[str, str], folder: str) -> str:
    """One virtual photo of several one-band files (role -> link or path), each band described by its role, so the
    file says which band is which like any multispectral photo. Nothing is copied: the bands are read where they
    are. The files must cover exactly the same pixels and hold the same kind of number, or they are not one photo
    (NotOnePhoto): laid over each other by position, a band missing over part of the ground would read as zero
    there, and an index of 1 or -1 would look like a result."""
    roles = [role for role in BAND_DESCRIPTIONS if role in band_urls]
    path = os.path.join(folder, "bands.vrt")
    sources = [f"/vsicurl/{band_urls[role]}" if band_urls[role].startswith("http") else band_urls[role] for role in roles]
    try:
        subprocess.run(["gdalbuildvrt", "-q", "-separate", path, *sources], check=True, capture_output=True,
                       timeout=180, env={**os.environ, **GDAL_HTTP_TIMEOUTS})
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # said without the command: it holds the links to the files, which must not reach the logs
        raise OSError("the band files could not be opened to lay them over each other") from None
    tree = ET.parse(path)
    root = tree.getroot()
    made = root.findall("VRTRasterBand")
    if len(made) != len(roles):
        raise OSError("the band files could not be laid over each other")
    whole = (root.get("rasterXSize"), root.get("rasterYSize"))
    kinds = set()
    for band, role in zip(made, roles):
        kinds.add(band.get("dataType"))
        for source in band:
            placed, read = source.find("DstRect"), source.find("SrcRect")
            if placed is None or read is None:
                continue
            same = lambda r: (r.get("xOff"), r.get("yOff"), r.get("xSize"), r.get("ySize")) == ("0", "0", *whole)  # noqa: E731
            if not same(placed) or not same(read):
                raise NotOnePhoto("the band files do not cover the same pixels")
        described = band.find("Description")
        (described if described is not None else ET.SubElement(band, "Description")).text = BAND_DESCRIPTIONS[role]
    if len(kinds) != 1:
        raise NotOnePhoto("the band files hold different kinds of numbers")
    tree.write(path)
    return path


def measure(cog_url: str, outlines: dict[str, dict[str, Any]], named_thermal: bool = False,
            band_urls: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """`_measure` on one photo, or on several one-band files read together as one (`band_urls`: role -> link)."""
    if not band_urls:
        return _measure(cog_url, outlines, named_thermal)
    with tempfile.TemporaryDirectory() as folder:
        try:
            stacked = _stack(band_urls, folder)
        except NotOnePhoto:  # a fact of these files: kept as nothing to measure, not tried again
            logger.info("field health: band files that are not one photo were not joined", exc_info=True)
            return {"nir": False, "reflectance": False, "thermal_unit": None, "fields": {}}
        return _measure(stacked, outlines, named_thermal)


def _measure(cog_url: str, outlines: dict[str, dict[str, Any]], named_thermal: bool = False) -> dict[str, Any]:
    """{"nir": whether the photo has a band to measure from (a near-infrared band it names, or a thermal one),
    "reflectance", "thermal_unit", "fields": {key: {"ndvi", "ndre", "gndvi", "temp", "zones", "bare_m2",
    "zone_index"}}} for outlines in WGS84 (blocking). An outline that is no shape is kept as not measured; a photo
    that cannot be read raises, so nothing wrong is kept and it is tried again."""
    with rasterio.Env(**GDAL_HTTP_TIMEOUTS), rasterio.open(cog_url) as ds:
        bands = photo_bands.of(ds)
        bands = dataclasses.replace(bands, thermal=thermal_band(ds, bands, named_thermal))
        photo = _photo_facts(ds, bands)
        if not photo["index"] and not photo["thermal"]:
            return {"nir": False, "reflectance": False, "thermal_unit": None, "fields": {}}
        to_photo = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        to_wgs84 = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True).transform
        fields: dict[str, dict[str, Any]] = {}
        for key, geometry in outlines.items():
            try:
                outline = reproject(to_photo, make_valid(shape(geometry)))
            except (ValueError, TypeError, KeyError, AttributeError, ShapelyError):  # this outline, not the photo
                logger.warning("field health: an outline is no shape", exc_info=True)
                fields[key] = dict(_EMPTY)
                continue
            fields[key] = (_one(ds, bands, outline, to_wgs84, photo)
                           if not outline.is_empty and outline.area > 0 else dict(_EMPTY))
        return {"nir": True, "reflectance": photo["reflectance"], "thermal_unit": photo["thermal_unit"],
                "program": photo_bands.program(ds.tags()), "fields": fields}


def _place(below: int, above: int, tied: bool, total: int, low: str = "lowest", high: str = "highest") -> str:
    """A field's place among the fields shown with it, counted from the lowest."""
    words = (f"{low} of {total} fields" if below == 0 else f"{high} of {total} fields" if above == 0 else None)
    if words is None:
        rank = below + 1
        ordinal = f"{rank}{'th' if 10 <= rank % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(rank % 10, 'th')}"
        words = f"{ordinal} {low} of {total} fields"
    return f"{words}, level with another" if tied else words


def _places(shown: dict[str, float], low: str = "lowest", high: str = "highest",
            level_within: Optional[float] = None) -> dict[str, str]:
    """Each field's place among the given values; {} for fewer than MIN_RANKED fields; no place where all are level.
    `level_within`: when the highest and lowest are closer than this, no field is called highest or lowest: each
    is said to be within that of the others, which is a fact and not a judgement that they are the same."""
    if len(shown) < MIN_RANKED:
        return {}
    if level_within is not None and round(max(shown.values()) - min(shown.values()), 4) < level_within:
        return {key: f"within {level_within:g} of the other {len(shown) - 1} fields" for key in shown}
    out = {}
    for key, value in shown.items():
        below = sum(1 for other in shown.values() if other < value)
        above = sum(1 for other in shown.values() if other > value)
        if below or above:
            out[key] = _place(below, above, len(shown) - below - above > 1, len(shown), low, high)
    return out


def said(fields: dict[str, dict[str, Any]], reflectance: bool,
         thermal_unit: Optional[str] = None) -> dict[str, FieldHealth]:
    """Each measured field as it is said everywhere. Pure. A place is among the fields given (the fields the
    reader is looking at), on the value as it is shown, so fields that show the same share a place."""
    shown = {key: round(values["ndvi"], 2) for key, values in fields.items() if values.get("ndvi") is not None}
    places = _places(shown, level_within=LEVEL_INDEX)
    out: dict[str, FieldHealth] = {}
    for key, value in shown.items():
        values, place = fields[key], places.get(key)
        if reflectance:
            words = f"health index (NDVI) {value:.2f}" + (f", {place}" if place else "")
            out[key] = FieldHealth(said=words, ndvi=values["ndvi"], ndre=values.get("ndre"), gndvi=values.get("gndvi"))
        else:
            out[key] = FieldHealth(said=f"health index (NDVI): {place}" if place else None)
    warm = {key: round(values["temp"], 1 if thermal_unit else 6) for key, values in fields.items()
            if values.get("temp") is not None}
    middle = float(np.median(list(warm.values()))) if len(warm) >= MIN_RANKED else None
    warm_places = _places(warm, "coolest", "warmest") if thermal_unit is None else {}
    for key, degrees in warm.items():
        if thermal_unit is None:  # the file does not state its unit: only which field is warmer can be said
            words = f"surface temperature: {warm_places[key]}" if key in warm_places else None
            out[key] = dataclasses.replace(out.get(key, FieldHealth(said=None)), temperature_said=words)
            continue
        words = f"surface {degrees:.1f} °C"
        if middle is not None:
            gap = round(degrees - middle, 1)
            words += (f", level with the middle of {len(warm)} fields" if gap == 0 else
                      f", {abs(gap):.1f} °C {'warmer' if gap > 0 else 'cooler'} than the middle of {len(warm)} fields")
        out[key] = dataclasses.replace(out.get(key, FieldHealth(said=None)), temperature_said=words, temp_c=degrees)
    for key, values in fields.items():
        if key in out and (isinstance(values.get("zones"), list) or values.get("bare_m2")):
            zones = values.get("zones")
            out[key] = dataclasses.replace(out[key], zones=tuple(zones) if isinstance(zones, list) else None,
                                           zone_index=values.get("zone_index"), bare_m2=values.get("bare_m2"))
    return out


RETRY_SECONDS = 600  # a photo that could not be read is not opened again sooner than this
_tasks: dict[str, "asyncio.Task[None]"] = {}  # one measurement at a time per photo, in this process
_pending: dict[str, dict[str, Any]] = {}  # outlines waiting for that measurement, from every caller
_failed_at: dict[str, float] = {}


async def _record(s3: Any, bucket: str, store_key: str) -> dict[str, Any]:
    record = await stored_files.load_json(s3, bucket, store_key)
    if not isinstance(record, dict) or record.get("method") != METHOD or not isinstance(record.get("fields"), dict):
        return {"method": METHOD, "fields": {}}
    return record


async def _measure_missing(s3: Any, bucket: str, store_key: str, cog_url: str, named_thermal: bool,
                           band_urls: Optional[dict[str, str]] = None) -> None:
    """Measure the outlines waiting for this photo and keep them, a batch at a time. Runs beside the request:
    nobody waits for a photo to be read. A photo that cannot be read keeps what was measured before the failure."""
    try:
        while _pending.get(store_key):
            record = await _record(s3, bucket, store_key)
            if record.get("nir") is False:
                break
            waiting = _pending[store_key]
            batch = {key: waiting[key] for key in list(waiting)[:BATCH]}
            todo = {key: geometry for key, geometry in batch.items() if key not in record["fields"]}
            measured = (await asyncio.to_thread(measure, cog_url, todo, named_thermal, band_urls)
                        if todo else None)
            if measured is not None:
                record = await _record(s3, bucket, store_key)  # another worker may have kept more meanwhile
                fields = {**record["fields"], **measured["fields"]}
                kept = {"method": METHOD, "nir": measured["nir"],
                        # facts of the whole photo, judged on the whole photo: set once, the same for every reader
                        "reflectance": record.get("reflectance", measured["reflectance"]),
                        "thermal_unit": record.get("thermal_unit", measured["thermal_unit"]),
                        "fields": dict(list(fields.items())[-MAX_KEPT:])}
                await s3.put_object(Bucket=bucket, Key=store_key, Body=json.dumps(kept).encode(),
                                    ContentType="application/json")
            for key in batch:
                waiting.pop(key, None)
        _failed_at.pop(store_key, None)
    except Exception:  # said nowhere: the fields are shown without a health index, and it is tried again later
        _failed_at[store_key] = asyncio.get_running_loop().time()
        logger.exception("field health: the photo kept at %s could not be measured", store_key)
    finally:
        _pending.pop(store_key, None)
        _tasks.pop(store_key, None)


async def of_features(s3: Any, bucket: str, photo_key: str, cog_url: str, features: list[dict[str, Any]],
                      named_thermal: bool = False) -> dict[str, FieldHealth]:
    return (await _asked(s3, bucket, photo_key, cog_url, features, named_thermal))[0]


async def _asked(s3: Any, bucket: str, photo_key: str, cog_url: str, features: list[dict[str, Any]],
                 named_thermal: bool = False,
                 band_urls: Optional[dict[str, str]] = None) -> tuple[dict[str, FieldHealth], bool]:
    """(The health of each feature's outline, by key_of(geometry), from what is kept; whether every outline asked
    is kept, so the answer will not change): {} for a photo with nothing to measure from. Never reads the photo
    itself: outlines not measured yet are measured beside the request and are there the next time this is asked,
    so no screen waits for it. `named_thermal`: the reader named this one-band layer as a thermal photo."""
    outlines = {key_of(f["geometry"]): f["geometry"] for f in features if isinstance(f.get("geometry"), dict)}
    if not outlines:
        return {}, True
    store_key = _store_key(photo_key, named_thermal)
    record = await _record(s3, bucket, store_key)
    if record.get("nir") is False:
        return {}, True
    missing = {key: geometry for key, geometry in outlines.items() if key not in record["fields"]}
    loop = asyncio.get_running_loop()
    if missing and loop.time() - _failed_at.get(store_key, -RETRY_SECONDS) >= RETRY_SECONDS:
        task = _tasks.get(store_key)
        if task is not None and not task.done() and task.get_loop() is loop:
            _pending.setdefault(store_key, {}).update(missing)  # the running measurement takes these too
        else:
            _pending[store_key] = dict(missing)
            _tasks[store_key] = loop.create_task(
                _measure_missing(s3, bucket, store_key, cog_url, named_thermal, band_urls))
    return (said({key: record["fields"][key] for key in outlines if key in record["fields"]},
                 bool(record.get("reflectance")), record.get("thermal_unit")), not missing)


async def settled(photo_key: str, named_thermal: bool = False) -> None:
    """Wait for this photo's measurement beside the request, if one is running (tests; a job that wants it now)."""
    task = _tasks.get(_store_key(photo_key, named_thermal))
    if task is not None and not task.done() and task.get_loop() is asyncio.get_running_loop():
        await task


_THERMAL_NAME = re.compile(r"(?<![a-z])(thermal|thermique|lwir|temperature)(?![a-z])", re.IGNORECASE)
def _one_band(companions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in companions if int(c.get("bands") or 1) == 1]


async def with_spectral(s3: Any, bucket: str, health: dict[str, FieldHealth], features: list[dict[str, Any]],
                        companions: list[dict[str, Any]],
                        url_of: Callable[[dict[str, Any]], Awaitable[str]]) -> dict[str, FieldHealth]:
    """`health` with each field's index and zones from a multispectral photo of the same place that was uploaded
    beside the colour photo (most programs deliver a colour picture and a file of bands as two files). The newest
    such file that names a near-infrared band is used, by the same rules as a thermal photo of its own: never
    while it is still being measured, never when it reaches fewer than MIN_COMPANION_COVER of the fields, and an
    older one only when every newer one is whole and gave nothing. Not used when the photo itself, or band
    files, already gave any field an index. Nothing is read here."""
    asked = len({key_of(f["geometry"]) for f in features if isinstance(f.get("geometry"), dict)})
    if not asked or any(h.said or h.zones is not None for h in health.values()):
        return health
    for companion in [c for c in companions if int(c.get("bands") or 1) >= 4][:MAX_COMPANIONS]:
        metadata = companion["metadata"]
        if not metadata.get("cog_key"):
            continue
        try:
            found, complete = await _asked(s3, bucket, metadata["cog_key"], await url_of(metadata), features)
        except Exception:  # this file cannot be asked now: wait, do not fall to an older one meanwhile
            logger.exception("field health: a multispectral photo of the place could not be asked")
            return health
        if not complete:
            return health
        measured = {key: h for key, h in found.items() if h.said or h.zones is not None or h.ndvi is not None}
        if measured and len(measured) >= MIN_COMPANION_COVER * asked:
            uploaded = companion.get("uploaded")
            source = plain_name(companion.get("name")) + (f", added {uploaded:%d %b %Y}" if uploaded else "")
            out = dict(health)
            for key, h in measured.items():
                kept = out.get(key, FieldHealth(said=None))
                out[key] = dataclasses.replace(h, temperature_said=h.temperature_said or kept.temperature_said,
                                               temp_c=h.temp_c if h.temperature_said else kept.temp_c,
                                               temperature_from=(source if h.temperature_said else kept.temperature_from),
                                               index_from=source)
            return out
    return health


MAX_COMPANIONS = 3  # other one-band photos of the place that are looked at for a temperature, newest first
MIN_COMPANION_COVER = 0.8  # a thermal photo is used when it gives this share of the fields a temperature: a place
# "among 4 fields" beside 10 fields on the screen would read as if it were among all of them


def named_thermal(name: Optional[str], metadata: dict[str, Any]) -> bool:
    """Whether the reader named this layer as a thermal photo: the word stands on its own in the layer's name or
    its file's name ("Kabarama_thermal.tif", "LWIR ortho"), not inside another word. The name is believed only
    when the file's values could be temperatures (`thermal_band`)."""
    return any(_THERMAL_NAME.search(str(text)) for text in (name, metadata.get("original_filename")) if text)


async def with_thermal(s3: Any, bucket: str, health: dict[str, FieldHealth], features: list[dict[str, Any]],
                       companions: list[dict[str, Any]],
                       url_of: Callable[[dict[str, Any]], Awaitable[str]]) -> dict[str, FieldHealth]:
    """`health` with each field's temperature from a thermal photo of the same place that is its own file.
    `companions`: the project's other one-band photos overlapping this one ({"name", "metadata"}), newest first.
    The newest one that gives the fields a temperature is used, and every field says which photo that was. An
    older one is used only once every newer one is fully measured and gave nothing, and none is used while its own
    measuring is unfinished: what a field says does not change from one load to the next. Fields that already have
    a temperature from their own photo keep it. Nothing is read here: see `of_features`."""
    asked = len({key_of(f["geometry"]) for f in features if isinstance(f.get("geometry"), dict)})
    if not asked or any(h.temperature_said for h in health.values()):
        return health
    for companion in _one_band(companions)[:MAX_COMPANIONS]:
        metadata = companion["metadata"]
        if not metadata.get("cog_key"):
            continue
        try:
            found, complete = await _asked(s3, bucket, metadata["cog_key"], await url_of(metadata), features,
                                           named_thermal(companion.get("name"), metadata))
        except Exception:  # this companion cannot be asked now: wait, do not fall to an older photo meanwhile
            logger.exception("field health: a thermal photo of the place could not be asked")
            return health
        if not complete:
            return health  # still being measured: nothing is said until it is whole
        warm = {key: h for key, h in found.items() if h.temperature_said}
        if warm and len(warm) >= MIN_COMPANION_COVER * asked:
            out = dict(health)
            source = plain_name(companion.get("name"))
            for key, h in warm.items():
                out[key] = dataclasses.replace(out.get(key, FieldHealth(said=None)), temperature_said=h.temperature_said,
                                               temp_c=h.temp_c, temperature_from=source)
            return out
    return health


# A file's band is the word its name ends with, after a break ("result_RedEdge", "flight 3 NIR", "field-red").
_BAND_AT_END = re.compile(r"^(?:(?P<pre>.*)[^A-Za-z])?(?P<band>red[ _-]?edge|nir|green|blue|red)$", re.IGNORECASE)
_BAND_ROLE = {"rededge": "red_edge", "nir": "nir", "green": "green", "blue": "blue", "red": "red"}


def band_named(name: Optional[str], metadata: dict[str, Any]) -> Optional[tuple[str, str]]:
    """(the band a one-band layer is, the rest of its name) from the file's name, else the layer's: "result_NIR.tif"
    is ("nir", "result"). None when the name does not end with a band's word standing on its own."""
    for text in (metadata.get("original_filename"), name):
        if not text:
            continue
        stem = re.sub(r"\.(tiff?|vrt)$", "", str(text).strip(), flags=re.IGNORECASE)
        match = _BAND_AT_END.match(stem)
        if match:
            role = _BAND_ROLE[re.sub(r"[^a-z]", "", match.group("band").lower())]
            return role, (match.group("pre") or "").strip(" _-").lower()
    return None


SAME_FLIGHT_HOURS = 24  # the bands of one flight are uploaded together: files further apart are not joined


def band_set(companions: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The newest set of one-band layers that are the bands of one flight: the same name but for the band's word,
    the same outline on the map, uploaded within SAME_FLIGHT_HOURS of each other, with at least red and
    near-infrared. {"key", "name", "members": {role: metadata}}; None when there is none. Two flights that are
    both called "result" lie on slightly different outlines, so they are never mixed.
    `companions`: one-band photos of the place ({"name", "metadata", "bounds", "uploaded"}), newest first."""
    groups: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
    for companion in _one_band(companions):
        metadata = companion.get("metadata") or {}
        named = band_named(companion.get("name"), metadata)
        if named and metadata.get("cog_key"):
            outline = tuple(round(float(v), 7) for v in companion.get("bounds") or ())
            groups.setdefault((named[1], outline), {}).setdefault(named[0], companion)  # the newest file of each band
    for (prefix, _outline), members in groups.items():
        if "red" not in members or "nir" not in members:
            continue
        times = [m["uploaded"] for m in members.values() if m.get("uploaded") is not None]
        if times and (max(times) - min(times)).total_seconds() > SAME_FLIGHT_HOURS * 3600:
            continue
        joined = "|".join(f"{role}={members[role]['metadata']['cog_key']}" for role in sorted(members))
        added = f", added {max(times):%d %b %Y}" if times else ""
        return {"key": "bands:" + hashlib.sha256(joined.encode()).hexdigest()[:32],
                "name": plain_name(prefix or "band files") + added,
                "members": {role: member["metadata"] for role, member in members.items()}}
    return None


async def with_bands(s3: Any, bucket: str, health: dict[str, FieldHealth], features: list[dict[str, Any]],
                     companions: list[dict[str, Any]],
                     url_of: Callable[[dict[str, Any]], Awaitable[str]]) -> dict[str, FieldHealth]:
    """`health` with each field's index and zones from the band files of the same place that were uploaded one band
    a file. When the photo itself gives any field an index, the band files are not used at all: a field's place
    among fields measured on two different sources would compare nothing. Nothing is said while the set is still
    being measured, or when it reaches fewer than MIN_COMPANION_COVER of the fields. Nothing is read here."""
    asked = len({key_of(f["geometry"]) for f in features if isinstance(f.get("geometry"), dict)})
    if not asked or any(h.said or h.zones is not None for h in health.values()):
        return health
    found_set = band_set(companions)
    if found_set is None:
        return health
    try:
        urls = {role: await url_of(metadata) for role, metadata in found_set["members"].items()}
        found, complete = await _asked(s3, bucket, found_set["key"], "", features, band_urls=urls)
    except Exception:  # the fields are still said, without an index from the band files
        logger.exception("field health: the band files of the place could not be asked")
        return health
    measured = {key: h for key, h in found.items() if h.said or h.zones is not None or h.ndvi is not None}
    if not complete or len(measured) < MIN_COMPANION_COVER * asked:
        return health
    out = dict(health)
    for key, h in measured.items():
        kept = out.get(key, FieldHealth(said=None))
        out[key] = dataclasses.replace(h, temperature_said=kept.temperature_said, temp_c=kept.temp_c,
                                       temperature_from=kept.temperature_from, index_from=found_set["name"])
    return out


def health_note(source: Optional[str]) -> str:
    """The caveat every surface gives with a health index, saying when it comes from separate band files."""
    return SHORT_NOTE + (f" Measured on the band files {source}, which may be from another day than this photo."
                         if source else "")


TEMPERATURE_NOTE = ("A warmer field can be bare soil, a thinner crop, or a crop short of water. Sun, wind and the "
                    "hour change every field, so compare fields from one flight only. The temperature is the "
                    "camera's reading of the surface, soil and leaves together, not checked with a thermometer.")
def plain_name(name: Optional[str]) -> str:
    """A layer's name as it may be repeated to a reader or to Sage: letters, digits, spaces and . _ - only, 60 at
    most. A name is written by whoever can edit the project, and must stay a name."""
    kept = re.sub(r"[^\w .\-]", " ", str(name or ""), flags=re.UNICODE)
    return re.sub(r"\s+", " ", kept).strip()[:60] or "a thermal photo"


def temperature_note(source: Optional[str]) -> str:
    """The caveat every surface gives with a temperature, saying when it comes from another photo."""
    return TEMPERATURE_NOTE + (f" Measured on the thermal photo {source}, which may be from another day or hour "
                               "than this photo." if source else "")


SHORT_NOTE = "A lower index can be a younger crop, another crop, or bare ground. No cut-off has been tested."
NOTE = ("The health index (NDVI) comes from the photo's own red and near-infrared bands. It is given with no verdict: "
        "no cut-off between healthy and stressed has been tested, and a lower index can be a younger crop, another "
        "crop, or bare ground.")


ZONE_GROUP = {"low": "least_green", "middle": "mostly_soil", "high": "other"}  # the overlay's three looks
ZONE_SAID = {"low": "lower third", "middle": "middle third", "high": "upper third"}


def zone_features(named: list[tuple[int, str, FieldHealth]]) -> dict[str, Any]:
    """The zones of the given fields ((number, name, health)) as one GeoJSON file for the map and the agronomist:
    one feature per zone, with the field it is in and its size. No rate: that is the agronomist's to add."""
    features = []
    for number, name, health in named:
        for zone in health.zones or ():
            features.append({"type": "Feature", "geometry": zone["geometry"], "properties": {
                "number": number, "field": name, "zone": zone["zone"], "zone_said": ZONE_SAID[zone["zone"]],
                "area_m2": zone["area_m2"], "index": (health.zone_index or "").upper(),
                "group": ZONE_GROUP[zone["zone"]],
                "label": f"{name} · {ZONE_SAID[zone['zone']]} · {zone['area_m2']:,} m²"}})
    return {"type": "FeatureCollection", "features": features}
