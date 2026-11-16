"""A health index for each field, from a photo that has a near-infrared band.

What each index answers to, from how a leaf treats light:
- NDVI = (NIR - Red) / (NIR + Red). Chlorophyll takes in red light and the leaf's inner structure throws back
  near-infrared, so it rises with the amount of living leaf over the ground. It stops rising in a dense crop.
- NDRE = (NIR - RedEdge) / (NIR + RedEdge). Red-edge light goes deeper into the leaves, so it keeps answering to
  how much chlorophyll (and so nitrogen) a dense crop holds after NDVI has stopped.
- GNDVI = (NIR - Green) / (NIR + Green). Answers to chlorophyll too, from the green band.

Nothing here is a verdict: no cut-off between healthy and stressed has been tested, and a lower index can be a
younger crop, another crop, or bare ground. So a field gets its index and its place among the photo's fields, and
on a photo of raw camera values (not reflectance) only its place: that order does not depend on calibration, the
number does (photo_bands.looks_like_reflectance).

Measured once per outline and kept beside the photo; an outline that changes is a new outline.
"""

from __future__ import annotations

import asyncio
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
from rasterio.features import geometry_mask
from rasterio.windows import Window, from_bounds
from shapely.geometry import mapping, shape
from shapely.ops import transform as reproject
from shapely.validation import make_valid

from src.services import drone_plots, photo_bands, stored_files
from src.services.gdal_http import GDAL_HTTP_TIMEOUTS

logger = logging.getLogger(__name__)

METHOD = "field-health-1"  # change when what is measured changes: kept records of another method are not read
STORE_PREFIX = "field_health/v1"
STATS_M_PER_PX = 0.5  # the photo is read at about this ground size: a field's mean does not need each leaf
MAX_READ_PIXELS = 16_000_000
MIN_PIXELS = 25  # fewer readable pixels than this inside an outline is no measurement
MIN_MEASURED_SHARE = 0.5  # ... and so is an outline less than half on the photo
MIN_RANKED = 3  # a field's place among fewer fields than this is not said
MEASURE_SECONDS = 120

_locks: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class FieldHealth:
    said: Optional[str]  # as every surface says it; None when there is nothing honest to say
    ndvi: Optional[float] = None  # the numbers only on a photo of reflectance
    ndre: Optional[float] = None
    gndvi: Optional[float] = None


def key_of(geometry: dict[str, Any]) -> str:
    """A field's outline as a key: the same outline always measures the same."""
    return hashlib.sha256(json.dumps(geometry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def _store_key(photo_key: str) -> str:
    return f"{STORE_PREFIX}/{hashlib.sha256(photo_key.encode()).hexdigest()[:32]}.json"


def _mean(index: np.ndarray, inside: np.ndarray, whole: float) -> Optional[float]:
    """The index's mean inside an outline; `whole` is how many pixels the whole outline would cover, so a field
    mostly off the photo, or mostly empty photo, is not measured from the part that happens to be readable."""
    values = index[inside]
    values = values[~np.isnan(values)]
    if values.size < MIN_PIXELS or values.size < MIN_MEASURED_SHARE * whole:
        return None
    return round(float(values.mean()), 4)


def measure(cog_url: str, outlines: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """{"nir": whether the photo has a near-infrared band it names, "reflectance": ..., "fields": {key: {"ndvi",
    "ndre", "gndvi"}}} for outlines in WGS84 (blocking). A field that cannot be measured has None for each index."""
    with rasterio.Env(**GDAL_HTTP_TIMEOUTS), rasterio.open(cog_url) as ds:
        bands = photo_bands.of(ds)
        if bands.nir is None or bands.red is None:
            return {"nir": False, "reflectance": False, "fields": {}}
        to_photo = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        shapes: dict[str, Any] = {}
        for key, geometry in outlines.items():
            try:
                outline = reproject(to_photo, make_valid(shape(geometry)))
            except Exception:  # this outline, not the photo: it is kept as not measured
                logger.warning("field health: an outline could not be read", exc_info=True)
                continue
            if not outline.is_empty and outline.area > 0:
                shapes[key] = outline
        empty: dict[str, dict[str, Optional[float]]] = {key: {"ndvi": None, "ndre": None, "gndvi": None}
                                                        for key in outlines}
        if not shapes:
            return {"nir": True, "reflectance": False, "fields": empty}
        left = max(min(s.bounds[0] for s in shapes.values()), ds.bounds.left)
        bottom = max(min(s.bounds[1] for s in shapes.values()), ds.bounds.bottom)
        right = min(max(s.bounds[2] for s in shapes.values()), ds.bounds.right)
        top = min(max(s.bounds[3] for s in shapes.values()), ds.bounds.top)
        if right <= left or top <= bottom:
            return {"nir": True, "reflectance": False, "fields": empty}
        w = from_bounds(left, bottom, right, top, ds.transform)
        col0, row0 = max(0, int(np.floor(w.col_off))), max(0, int(np.floor(w.row_off)))
        col1 = min(ds.width, int(np.ceil(w.col_off + w.width)))
        row1 = min(ds.height, int(np.ceil(w.row_off + w.height)))
        if col1 <= col0 or row1 <= row0:
            return {"nir": True, "reflectance": False, "fields": empty}
        window = Window(col0, row0, col1 - col0, row1 - row0)
        factor = max(1.0, STATS_M_PER_PX / drone_plots._photo_m_per_px(ds))
        factor = max(factor, float(np.sqrt(window.width * window.height / MAX_READ_PIXELS)))
        rows, cols = max(1, int(window.height / factor)), max(1, int(window.width / factor))

        def band(number: int) -> Any:
            return ds.read(number, window=window, out_shape=(rows, cols), resampling=Resampling.average, masked=True)

        nir, red = band(bands.nir), band(bands.red)
        indices = {"ndvi": photo_bands.ndvi(nir, red),
                   "ndre": photo_bands.ndre(nir, band(bands.red_edge)) if bands.red_edge else None,
                   "gndvi": photo_bands.index(nir, band(bands.green)) if bands.green else None}
        transform = ds.window_transform(window) * Affine.scale(window.width / cols, window.height / rows)
        fields = dict(empty)
        for key, outline in shapes.items():
            inside = geometry_mask([mapping(outline)], out_shape=(rows, cols), transform=transform, invert=True)
            if inside.any():
                whole = outline.area / abs(transform.a * transform.e)
                fields[key] = {name: (_mean(index, inside, whole) if index is not None else None)
                               for name, index in indices.items()}
        return {"nir": True, "reflectance": photo_bands.looks_like_reflectance(nir, red), "fields": fields}


def _place(rank: int, total: int) -> str:
    """A field's place among the photo's fields, counted from the lowest index."""
    if rank == 1:
        return f"lowest of {total} fields"
    if rank == total:
        return f"highest of {total} fields"
    ordinal = f"{rank}{'th' if 10 <= rank % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(rank % 10, 'th')}"
    return f"{ordinal} lowest of {total} fields"


def said(fields: dict[str, dict[str, Any]], reflectance: bool) -> dict[str, FieldHealth]:
    """Each measured field as it is said everywhere. Pure."""
    ranked = sorted((key for key, values in fields.items() if values.get("ndvi") is not None),
                    key=lambda key: fields[key]["ndvi"])
    places = {key: _place(i + 1, len(ranked)) for i, key in enumerate(ranked)} if len(ranked) >= MIN_RANKED else {}
    out: dict[str, FieldHealth] = {}
    for key in ranked:
        values, place = fields[key], places.get(key)
        if reflectance:
            words = f"health index (NDVI) {values['ndvi']:.2f}" + (f", {place}" if place else "")
            out[key] = FieldHealth(said=words, ndvi=values["ndvi"], ndre=values.get("ndre"), gndvi=values.get("gndvi"))
        else:
            out[key] = FieldHealth(said=f"health index (NDVI): {place}" if place else None)
    return out


async def of_features(s3: Any, bucket: str, photo_key: str, cog_url: str,
                      features: list[dict[str, Any]]) -> dict[str, FieldHealth]:
    """The health of each feature's outline, by key_of(geometry): {} for a photo with no near-infrared band.
    Outlines not measured yet are measured now (seconds) and kept; the rest are read."""
    outlines = {key_of(f["geometry"]): f["geometry"] for f in features if isinstance(f.get("geometry"), dict)}
    if not outlines:
        return {}
    async with _locks.setdefault(photo_key, asyncio.Lock()):
        record = await stored_files.load_json(s3, bucket, _store_key(photo_key))
        if not isinstance(record, dict) or record.get("method") != METHOD:
            record = {"method": METHOD, "fields": {}}
        if record.get("nir") is False:
            return {}
        missing = {key: geometry for key, geometry in outlines.items() if key not in record["fields"]}
        if missing:
            measured = await asyncio.wait_for(asyncio.to_thread(measure, cog_url, missing), timeout=MEASURE_SECONDS)
            any_measured = any(v.get("ndvi") is not None for v in measured["fields"].values())
            record = {"method": METHOD, "nir": measured["nir"],
                      # what the values are is a fact of the photo: learnt from any field that could be measured
                      "reflectance": measured["reflectance"] if any_measured else bool(record.get("reflectance")),
                      "fields": {**record["fields"], **measured["fields"]}}
            await s3.put_object(Bucket=bucket, Key=_store_key(photo_key), Body=json.dumps(record).encode(),
                                ContentType="application/json")
            if not measured["nir"]:
                return {}
    return said({key: record["fields"][key] for key in outlines if key in record["fields"]},
                bool(record.get("reflectance")))


NOTE = ("The health index (NDVI) comes from the photo's own red and near-infrared bands. It is given with no verdict: "
        "no cut-off between healthy and stressed has been tested, and a lower index can be a younger crop, another "
        "crop, or bare ground.")
