"""Plots on a drone photo: found by a field-outlining model, numbered, measured, ranked and exported.

A plot is one field as a farmer works it. Ingabe finds the plots once per photo
(a few minutes on the CPU), keeps the result in object storage, and every card
can then answer plot by plot. The words for readers live in drone_cards.py;
this module owns the outlines, the numbers and the files.

Method, measured on Cyampirita (3.2 cm photo, 2026-10-06):
- Delineate Anything v2 (field instance segmentation, AGPL-3.0 like the
  Ultralytics runtime it needs) read at 0.5 m per pixel found 176 plots in a
  600 m square, 123 at 1 m and 16 at 0.25 m; FastSAM at 0.25 m found about 45.
  Plots it finds follow the plot edges well; it misses some, mostly where
  neighbouring plots look alike. Checked by eye, not against surveyed edges.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import rasterio
from affine import Affine
from pyproj import Geod, Transformer
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.warp import transform_bounds
from shapely import STRtree
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.ops import transform as reproject, unary_union

from src.services.grvi import BARE_PIXEL, grvi

logger = logging.getLogger(__name__)

# --- The field-outlining model ------------------------------------------------------

FIELD_MODEL_URL = "https://huggingface.co/MykolaL/DelineateAnything/resolve/main/DelineateAnythingv2.pt"
FIELD_MODEL_SHA256 = "46700b8a279b07922953a11adaeb5e658d9a2384b6334c8e0a3090886218915a"
FIELD_MODEL_NAME = "Delineate Anything v2"
_DEFAULT_MODEL_PATH = "/tmp/ingabe_cache/models/DelineateAnythingv2.pt"

READ_M_PER_PX = 0.5  # detail the model sees; see the module docstring for why
TILE_PX = 1024
TILE_OVERLAP_PX = 256  # a plot narrower than 128 m always lies whole inside one tile
EDGE_PX = 3  # an outline this close to an inner tile edge is cut off; the next tile has it whole
MODEL_CONF = 0.15  # the model card's starting value
MODEL_IOU = 0.5

MIN_PLOT_M2 = 100.0  # smaller shapes are paths, trees and noise
MAX_PLOT_HA = 20.0  # larger shapes are whole hillsides, not one plot
MIN_PHOTO_COVER = 0.9  # a plot must lie at least this much on the photo, not on its empty edges
MAX_SHARED = 0.3  # a plot sharing more than this with plots already kept is a duplicate
BLOCK_PLOTS = 2  # an outline around this many kept plots is a block of plots (a tree inside a plot is fine)

# --- Plot measurements --------------------------------------------------------------

STATS_M_PER_PX = 0.25  # greenness and bare ground per plot are read at this detail
MIN_MEASURED_SHARE = 0.5  # below this share of readable pixels a plot's greenness is unknown
TAIL_SHARE = 0.2  # the least green and the greenest fifth of the plots with a crop in a photo
# A plot with at least this share of bare soil is just prepared, just planted or harvested: comparing its
# greenness with plots in full crop says nothing about the crop (on Cyampirita 81 of 445 plots).
MOSTLY_BARE = 0.5

MOSTLY_SOIL = "mostly_soil"
LEAST_GREEN = "least_green"
BETWEEN = "between"
GREENEST = "greenest"
UNKNOWN = "unknown"

_GEOD = Geod(ellps="WGS84")
_STORE_PREFIX = "drone_plots/v1"


@dataclass(frozen=True)
class PlotSet:
    """The plots of one photo, numbered from the north-west, with their measurements."""

    geojson: dict[str, Any]  # WGS84 polygons; properties: number, area_ha, greenness, bare_share, group, confidence
    found_at: str
    seconds: float
    source: str  # "found" by the model, or the name of the reader's own plot layer
    read_m_per_px: float

    @property
    def count(self) -> int:
        return len(self.geojson["features"])

    @property
    def total_ha(self) -> float:
        return sum(f["properties"]["area_ha"] for f in self.geojson["features"])

    def plots(self) -> list[dict[str, Any]]:
        return [f["properties"] for f in self.geojson["features"]]


@dataclass(frozen=True)
class PlotJob:
    state: str  # "running" or "failed"
    parts_done: int
    parts: int
    started: float
    error: Optional[str] = None

    @property
    def minutes_left(self) -> Optional[int]:
        if self.state != "running" or self.parts_done == 0:
            return None
        per_part = (time.time() - self.started) / self.parts_done
        return max(1, round(per_part * (self.parts - self.parts_done) / 60))


# --- Model ---------------------------------------------------------------------------------

def _model_path() -> Path:
    return Path(os.environ.get("INGABE_FIELD_MODEL_PATH", _DEFAULT_MODEL_PATH))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_model() -> Path:
    """The model weights on disk, downloaded and checked on first use."""
    path = _model_path()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    logger.info("downloading %s to %s", FIELD_MODEL_NAME, path)
    urllib.request.urlretrieve(FIELD_MODEL_URL, partial)
    if _sha256(partial) != FIELD_MODEL_SHA256:
        partial.unlink()
        raise RuntimeError(f"{FIELD_MODEL_NAME} download does not match its checksum")
    partial.rename(path)
    return path


@lru_cache(maxsize=1)
def _load_model(path: str) -> Any:
    from ultralytics import YOLO

    return YOLO(path)


# --- Finding the plots ----------------------------------------------------------------------

def _tile_starts(size: int) -> list[int]:
    if size <= TILE_PX:
        return [0]
    step = TILE_PX - TILE_OVERLAP_PX
    starts = list(range(0, size - TILE_PX, step))
    return starts + [size - TILE_PX]


def _mask_outline(mask: np.ndarray) -> Optional[Polygon]:
    """The largest outline of one model mask, in tile pixels."""
    import cv2

    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    contour = cv2.approxPolyDP(contour, 1.0, True)
    if len(contour) < 3:
        return None
    polygon = Polygon(contour[:, 0, :]).buffer(0)
    if isinstance(polygon, MultiPolygon):
        polygon = max(polygon.geoms, key=lambda g: g.area)
    return polygon if isinstance(polygon, Polygon) and not polygon.is_empty else None


def _touches_inner_edge(bounds: tuple[float, float, float, float], x0: int, y0: int, w: int, h: int) -> bool:
    minx, miny, maxx, maxy = bounds
    return ((x0 > 0 and minx <= EDGE_PX) or (y0 > 0 and miny <= EDGE_PX)
            or (x0 + TILE_PX < w and maxx >= TILE_PX - EDGE_PX)
            or (y0 + TILE_PX < h and maxy >= TILE_PX - EDGE_PX))


def _keep_distinct(candidates: list[tuple[float, Polygon]]) -> list[tuple[float, Polygon]]:
    """Most confident first; a candidate mostly covered by kept plots is dropped, a small overlap is cut away,
    and one that surrounds kept plots is a block of plots, not a plot (Cyampirita: a 6.4 ha outline around 9)."""
    kept: list[tuple[float, Polygon]] = []
    kept_shapes: list[Polygon] = []
    for conf, polygon in sorted(candidates, key=lambda c: c[0], reverse=True):
        if kept_shapes:
            tree = STRtree(kept_shapes)
            near = [kept_shapes[i] for i in tree.query(polygon)]
            if sum(polygon.contains(p) for p in near) >= BLOCK_PLOTS:
                continue
            overlap = unary_union([p.intersection(polygon) for p in near]) if near else None
            if overlap is not None and not overlap.is_empty:
                if overlap.area > MAX_SHARED * polygon.area:
                    continue
                polygon = polygon.difference(overlap)
                if isinstance(polygon, MultiPolygon):
                    polygon = max(polygon.geoms, key=lambda g: g.area)
                if not isinstance(polygon, Polygon) or polygon.is_empty:
                    continue
        kept.append((conf, polygon))
        kept_shapes.append(polygon)
    return kept


def _area_ha(geom_wgs84: Any) -> float:
    return abs(_GEOD.geometry_area_perimeter(geom_wgs84)[0]) / 10_000


def _north_west_order(outlines: list[Polygon]) -> list[int]:
    """Indexes in plot-number order: rows from north to south, west to east inside a row (rows about one plot tall)."""
    if not outlines:
        return []
    row_height = float(np.median([np.sqrt(p.area) for p in outlines]))
    centres = [p.centroid for p in outlines]
    order = sorted(range(len(outlines)), key=lambda i: (-round(centres[i].y / row_height), centres[i].x))
    return order


def _measure(ds: Any, outlines: list[Polygon]) -> list[tuple[Optional[float], Optional[float]]]:
    """Mean greenness and bare share of each outline (in the photo's CRS); None where too little is readable."""
    m_per_px = _photo_m_per_px(ds)
    factor = max(1.0, STATS_M_PER_PX / m_per_px)
    rows, cols = max(1, int(ds.height / factor)), max(1, int(ds.width / factor))
    bands = ds.read([1, 2], out_shape=(2, rows, cols), resampling=Resampling.average, masked=True)
    green = grvi(bands[0], bands[1])  # NaN where the photo is empty
    readable = ~np.isnan(green)
    green = np.where(readable, green, 0).astype("float32")
    transform = ds.transform * Affine.scale(ds.width / cols, ds.height / rows)
    labels = rasterize(((p, i + 1) for i, p in enumerate(outlines)), out_shape=(rows, cols),
                       transform=transform, fill=0, dtype="int32")
    n = len(outlines) + 1
    inside = np.bincount(labels.ravel(), minlength=n)
    seen = np.bincount(labels.ravel(), weights=readable.ravel(), minlength=n)
    green_sum = np.bincount(labels.ravel(), weights=(green * readable).ravel(), minlength=n)
    bare_sum = np.bincount(labels.ravel(), weights=((green < BARE_PIXEL) & readable).ravel(), minlength=n)
    results = []
    for i in range(1, n):
        if inside[i] == 0 or seen[i] < MIN_MEASURED_SHARE * inside[i]:
            results.append((None, None))
        else:
            results.append((float(green_sum[i] / seen[i]), float(bare_sum[i] / seen[i])))
    return results


def _photo_m_per_px(ds: Any) -> float:
    west, south, east, north = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds)
    lat = (south + north) / 2
    return _GEOD.inv(west, lat, east, lat)[2] / ds.width


def plot_groups(measures: list[tuple[Optional[float], Optional[float]]]) -> list[str]:
    """Each plot's group from its (greenness, bare share): mostly soil apart, then the least green and the
    greenest fifth of the plots with a crop, the rest in between; unknown when not measured."""
    with_crop = sorted(g for g, bare in measures if g is not None and bare is not None and bare < MOSTLY_BARE)
    tail = max(1, int(len(with_crop) * TAIL_SHARE))
    low_cut, high_cut = (with_crop[tail - 1], with_crop[-tail]) if len(with_crop) >= 5 else (None, None)

    def group(greenness: Optional[float], bare: Optional[float]) -> str:
        if greenness is None or bare is None:
            return UNKNOWN
        if bare >= MOSTLY_BARE:
            return MOSTLY_SOIL
        if low_cut is None or high_cut is None:
            return BETWEEN
        return LEAST_GREEN if greenness <= low_cut else GREENEST if greenness >= high_cut else BETWEEN

    return [group(g, bare) for g, bare in measures]


def _plot_set(ds: Any, found: list[tuple[float, Polygon]], source: str, seconds: float) -> PlotSet:
    """Measure, number and describe outlines given in the photo's CRS."""
    to_wgs84 = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True).transform
    outlines = [p for _, p in found]
    measures = _measure(ds, outlines)
    groups = plot_groups(measures)
    features = []
    for number, i in enumerate(_north_west_order(outlines), start=1):
        conf, outline = found[i]
        wgs84 = reproject(to_wgs84, outline)
        greenness, bare = measures[i]
        centre = wgs84.representative_point()
        features.append({"type": "Feature", "geometry": mapping(wgs84), "properties": {
            "number": number,
            "area_ha": round(_area_ha(wgs84), 3),
            "greenness": None if greenness is None else round(greenness, 4),
            "bare_share": None if bare is None else round(bare, 3),
            "group": groups[i],
            "confidence": None if conf is None else round(conf, 2),
            "lon": round(centre.x, 6),
            "lat": round(centre.y, 6),
        }})
    return PlotSet(geojson={"type": "FeatureCollection", "features": features},
                   found_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   seconds=round(seconds, 1), source=source, read_m_per_px=READ_M_PER_PX)


def find_plots(cog_url: str, progress: Callable[[int, int], None] = lambda done, parts: None) -> PlotSet:
    """Outline, number and measure the plots in an RGB drone photo (blocking, minutes for a large photo)."""
    started = time.perf_counter()
    model = _load_model(str(ensure_model()))
    with rasterio.open(cog_url) as ds:
        factor = max(1.0, READ_M_PER_PX / _photo_m_per_px(ds))
        rows, cols = max(1, int(ds.height / factor)), max(1, int(ds.width / factor))
        bands = ds.read([1, 2, 3], out_shape=(3, rows, cols), resampling=Resampling.average, masked=True)
        readable = ~np.ma.getmaskarray(bands).any(axis=0)
        image = np.ascontiguousarray(np.transpose(bands.filled(0), (1, 2, 0))[:, :, ::-1])  # BGR for the model
        to_photo = ds.transform * Affine.scale(ds.width / cols, ds.height / rows)
        m_per_px = _photo_m_per_px(ds) * factor
        tiles = [(x0, y0) for y0 in _tile_starts(rows) for x0 in _tile_starts(cols)]
        candidates: list[tuple[float, Polygon]] = []
        for done, (x0, y0) in enumerate(tiles):
            tile = image[y0:y0 + TILE_PX, x0:x0 + TILE_PX]
            if readable[y0:y0 + TILE_PX, x0:x0 + TILE_PX].mean() < 0.05:
                progress(done + 1, len(tiles))
                continue
            result = model.predict(tile, imgsz=TILE_PX, conf=MODEL_CONF, iou=MODEL_IOU, retina_masks=True,
                                   verbose=False, device="cpu", max_det=1000)[0]
            if result.masks is not None:
                h, w = tile.shape[:2]
                for mask, conf in zip(result.masks.data.cpu().numpy(), result.boxes.conf.cpu().numpy()):
                    outline = _mask_outline(mask[:h, :w])
                    if outline is None or _touches_inner_edge(outline.bounds, x0, y0, cols, rows):
                        continue
                    area_m2 = outline.area * m_per_px ** 2
                    if area_m2 < MIN_PLOT_M2 or area_m2 > MAX_PLOT_HA * 10_000:
                        continue
                    candidates.append((float(conf), _shift(outline, x0, y0)))
            progress(done + 1, len(tiles))
        on_photo = [(conf, p) for conf, p in candidates if _photo_cover(p, readable) >= MIN_PHOTO_COVER]
        kept = _keep_distinct(on_photo)
        found = [(conf, reproject(lambda x, y, z=None: to_photo * (x, y), p)) for conf, p in kept]
        return _plot_set(ds, found, "found", time.perf_counter() - started)


def _shift(polygon: Polygon, dx: int, dy: int) -> Polygon:
    return Polygon([(x + dx, y + dy) for x, y in polygon.exterior.coords])


def _photo_cover(polygon: Polygon, readable: np.ndarray) -> float:
    minx, miny, maxx, maxy = (int(v) for v in polygon.bounds)
    window = readable[max(0, miny):maxy + 1, max(0, minx):maxx + 1]
    if window.size == 0:
        return 0.0
    inside = rasterize([(polygon, 1)], out_shape=window.shape,
                       transform=Affine.translation(max(0, minx), max(0, miny)), fill=0, dtype="uint8").astype(bool)
    return float(window[inside].mean()) if inside.any() else 0.0


def measure_own_plots(cog_url: str, plots_wgs84: list[Any], source: str) -> PlotSet:
    """Number and measure the reader's own plot outlines (WGS84 shapes) on this photo."""
    started = time.perf_counter()
    with rasterio.open(cog_url) as ds:
        to_photo = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        found = []
        for geom in plots_wgs84:
            polygon = reproject(to_photo, geom)
            for part in getattr(polygon, "geoms", [polygon]):
                if isinstance(part, Polygon) and not part.is_empty:
                    found.append((None, part))
        return _plot_set(ds, found, source, time.perf_counter() - started)


# --- Kept results and running jobs --------------------------------------------------------

_jobs: dict[str, PlotJob] = {}
_results: dict[str, PlotSet] = {}
_job_lock = threading.Lock()  # one plot search at a time: it uses the CPU for minutes


def _store_key(photo_key: str) -> str:
    return f"{_STORE_PREFIX}/{hashlib.sha256(photo_key.encode()).hexdigest()[:32]}.json"


def _to_json(plots: PlotSet) -> bytes:
    return json.dumps({"geojson": plots.geojson, "found_at": plots.found_at, "seconds": plots.seconds,
                       "source": plots.source, "read_m_per_px": plots.read_m_per_px}).encode()


def _from_json(raw: bytes) -> PlotSet:
    data = json.loads(raw)
    return PlotSet(geojson=data["geojson"], found_at=data["found_at"], seconds=data["seconds"],
                   source=data["source"], read_m_per_px=data["read_m_per_px"])


async def load_plots(s3: Any, bucket: str, photo_key: str) -> Optional[PlotSet]:
    """The plots already found for this photo, from memory or object storage; None if not found yet."""
    if photo_key in _results:
        return _results[photo_key]
    try:
        response = await s3.get_object(Bucket=bucket, Key=_store_key(photo_key))
    except s3.exceptions.NoSuchKey:
        return None
    async with response["Body"] as body:
        plots = _from_json(await body.read())
    _results[photo_key] = plots
    return plots


def job(photo_key: str) -> Optional[PlotJob]:
    return _jobs.get(photo_key)


def start_finding(s3: Any, bucket: str, photo_key: str, cog_url: str) -> PlotJob:
    """Start the plot search for a photo in the background, once; the running or failed job is returned."""
    current = _jobs.get(photo_key)
    if current is not None and current.state == "running":
        return current
    _jobs[photo_key] = PlotJob(state="running", parts_done=0, parts=1, started=time.time())

    def progress(done: int, parts: int) -> None:
        _jobs[photo_key] = PlotJob(state="running", parts_done=done, parts=parts, started=_jobs[photo_key].started)

    def run() -> PlotSet:
        with _job_lock:
            return find_plots(cog_url, progress)

    async def work() -> None:
        try:
            plots = await asyncio.to_thread(run)
            await s3.put_object(Bucket=bucket, Key=_store_key(photo_key), Body=_to_json(plots),
                                ContentType="application/json")
            _results[photo_key] = plots
            _jobs.pop(photo_key, None)
            logger.info("found %d plots for %s in %.0f s", plots.count, photo_key, plots.seconds)
        except Exception as exc:  # the card shows the failure; the next request may try again
            logger.exception("plot search failed for %s", photo_key)
            previous = _jobs.get(photo_key)
            _jobs[photo_key] = PlotJob(state="failed", parts_done=previous.parts_done if previous else 0,
                                       parts=previous.parts if previous else 1, started=time.time(),
                                       error=str(exc)[:200])

    asyncio.get_running_loop().create_task(work())
    return _jobs[photo_key]


# --- Files to download ----------------------------------------------------------------------

GROUP_LABELS = {MOSTLY_SOIL: "Mostly soil showing", LEAST_GREEN: "Least green fifth", BETWEEN: "In between",
                GREENEST: "Greenest fifth", UNKNOWN: "Not measured"}

_COLUMNS = [  # (heading, property, Shapefile field of 10 characters at most)
    ("Plot", "number", "plot"),
    ("Area (ha)", "area_ha", "area_ha"),
    ("Greenness (GRVI)", "greenness", "grvi"),
    ("Bare ground (share)", "bare_share", "bare_share"),
    ("Group in this photo", "group", "group"),
    ("Outline confidence", "confidence", "confidence"),
    ("Latitude", "lat", "lat"),
    ("Longitude", "lon", "lon"),
]


def _row(props: dict[str, Any]) -> list[Any]:
    return [GROUP_LABELS[props["group"]] if key == "group" else props.get(key) for _, key, _ in _COLUMNS]


def to_xlsx(plots: PlotSet, photo_name: str) -> bytes:
    """One row per plot, and a sheet that says how the numbers were made."""
    import xlsxwriter

    out = io.BytesIO()
    book = xlsxwriter.Workbook(out, {"in_memory": True})
    bold = book.add_format({"bold": True})
    sheet = book.add_worksheet("Plots")
    for col, (heading, _, _) in enumerate(_COLUMNS):
        sheet.write(0, col, heading, bold)
    for r, props in enumerate(plots.plots(), start=1):
        for col, value in enumerate(_row(props)):
            if value is not None:
                sheet.write(r, col, value)
    sheet.set_column(0, len(_COLUMNS) - 1, 18)
    sheet.freeze_panes(1, 0)
    about = book.add_worksheet("How it was made")
    lines = [
        ("Photo", photo_name),
        ("Plots", "found in the photo by Ingabe" if plots.source == "found" else f"from your map: {plots.source}"),
        ("Found on", plots.found_at),
        ("Outlines", f"{FIELD_MODEL_NAME}, a field-outlining model, reading the photo at {plots.read_m_per_px} m "
                     "per pixel. Checked by eye, not against surveyed edges: some plots are missed or joined."
                     if plots.source == "found" else "your own outlines"),
        ("Area", "measured on the ellipsoid from each outline"),
        ("Greenness", f"mean GRVI = (green - red) / (green + red), read at {STATS_M_PER_PX} m per pixel. "
                      "Greenness, not health: a colour camera cannot see stress that still looks green."),
        ("Bare ground", f"share of the plot with GRVI below {BARE_PIXEL}"),
        ("Group", f"plots with {round(MOSTLY_BARE * 100)}% or more bare soil are 'mostly soil showing' (just "
                  "prepared, just planted or harvested). The others, with a crop, are split into the least green "
                  "fifth, the greenest fifth and those in between, compared within this photo."),
        ("Empty cells", "not measured: too little of the plot is on the photo"),
    ]
    for r, (label, text) in enumerate(lines):
        about.write(r, 0, label, bold)
        about.write(r, 1, text)
    about.set_column(0, 0, 14)
    about.set_column(1, 1, 110)
    book.close()
    return out.getvalue()


def to_shapefile_zip(plots: PlotSet, file_stem: str) -> bytes:
    """The plots as a zipped Shapefile (WGS84) with the same columns as the spreadsheet."""
    import geopandas as gpd

    rows = [{field: (GROUP_LABELS[p["group"]] if key == "group" else p.get(key)) for _, key, field in _COLUMNS}
            for p in plots.plots()]
    frame = gpd.GeoDataFrame(rows, geometry=[shape(f["geometry"]) for f in plots.geojson["features"]],
                             crs="EPSG:4326")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"{file_stem}.shp"
        frame.to_file(path, driver="ESRI Shapefile", engine="pyogrio")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for part in sorted(Path(folder).iterdir()):
                archive.write(part, part.name)
        return out.getvalue()


def to_geojson(plots: PlotSet) -> bytes:
    return json.dumps(plots.geojson).encode()
