"""Pictures the example finder looks at, and how the caller's boxes map to their pixels.

A picture source reads a Picture: its pixels (RGB, 8 bits a channel), a sha256 that identifies them, which way it
looks (answer_levels.OBLIQUE or NADIR: the finder runs only on pictures taken the way its target was measured), and
the frame the caller's boxes are given in. Two sources:

* `Photo`: a photo given as bytes, with the way it looks said by the caller (DJI's gimbal pitch, read by
  photo_inspections, tells it). Boxes are in the photo's own pixels.
* `InspectionPhoto`: an original photo of a photo inspection, read from that inspection's own storage prefix only
  (the prefix recorded on the layer and checked to be that layer's), then read as a `Photo`.
* `OrthophotoWindow`: a window of a drone orthophoto layer (NADIR), given as a box in lon/lat or in the layer's CRS,
  read through src/services/raster_process.py (remote COG reads must not run in threads: Lessons log 2026-10-07) at
  the photo's own resolution, at most MAX_WINDOW_PX a side. Boxes are in the window's coordinates; the finds come
  back in them too.

A new source (a photo-of-a-photo inspection) implements `PictureSource.read`.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence

import numpy as np
import rasterio
from affine import Affine
from PIL import Image, ImageOps
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

from src.services import answer_levels, photo_inspections, raster_process
from src.services.gdal_http import GDAL_HTTP_TIMEOUTS

Box = tuple[float, float, float, float]

MAX_WINDOW_PX = 2048  # a side, at the photo's own resolution
MAX_PHOTO_PIXELS = 40_000_000  # a 40 MP photo; a larger (or forged) header is refused before decoding
MAX_PHOTO_BYTES = 100 * 1024 ** 2  # an inspection photo larger than this is not read into memory
LONLAT = "lonlat"
LAYER = "layer"
FRAMES = (LONLAT, LAYER)
_LONLAT_CRS = "EPSG:4326"
_DENSIFY = 21  # points per edge when a box is reprojected, so its bounds follow the curved edges


class FinderInputError(ValueError):
    """The request cannot be answered as asked (a mark outside the picture, a window off the photo, ...). The
    message says what is wrong in the caller's terms."""


class PictureReadError(RuntimeError):
    """The picture could not be read (storage or network); asking again may work."""


@dataclass(frozen=True)
class MapFrame:
    """Where a window's pixels are: boxes in `crs` (lon/lat or the layer's CRS) <-> the window's pixels."""

    crs: str  # LONLAT or LAYER: the coordinates of the caller's boxes and of the finds given back
    layer_crs: str  # the photo's CRS, as WKT
    transform: tuple[float, ...]  # affine (a, b, c, d, e, f): window pixel (col, row) -> layer CRS

    def _to_layer(self, box: Box) -> Box:
        if self.crs == LONLAT:
            return tuple(transform_bounds(_LONLAT_CRS, self.layer_crs, *box, densify_pts=_DENSIFY))
        return box

    def to_pixels(self, box: Box) -> Box:
        """The box's bounds in the window's pixels (a lon/lat box is reprojected first)."""
        x0, y0, x1, y1 = self._to_layer(box)
        inverse = ~Affine(*self.transform)
        corners = [inverse * (x, y) for x in (x0, x1) for y in (y0, y1)]
        return _bounds(corners)

    def from_pixels(self, box: Box) -> Box:
        """A box in the window's pixels as bounds in the frame's coordinates."""
        px0, py0, px1, py1 = box
        forward = Affine(*self.transform)
        x0, y0, x1, y1 = _bounds([forward * (c, r) for c in (px0, px1) for r in (py0, py1)])
        if self.crs == LONLAT:
            return tuple(transform_bounds(self.layer_crs, _LONLAT_CRS, x0, y0, x1, y1, densify_pts=_DENSIFY))
        return (x0, y0, x1, y1)

    def as_dict(self) -> dict[str, Any]:
        return {"crs": self.crs, "layer_crs": self.layer_crs, "transform": list(self.transform)}


def _bounds(points: Sequence[tuple[float, float]]) -> Box:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def check_view(view: str) -> str:
    if view not in answer_levels.VIEWS:
        raise FinderInputError(f"a picture looks one of {list(answer_levels.VIEWS)}, not {view!r}")
    return view


@dataclass(frozen=True)
class Picture:
    pixels: np.ndarray  # height x width x 3, uint8, RGB
    sha256: str  # identifies the pixels and, for a window, where they are and the frame of its boxes
    view: str  # answer_levels.OBLIQUE or NADIR: which way the picture looks
    frame: Optional[MapFrame] = None  # None: boxes are in the picture's own pixels

    @property
    def width(self) -> int:
        return int(self.pixels.shape[1])

    @property
    def height(self) -> int:
        return int(self.pixels.shape[0])

    def to_pixels(self, box: Box) -> Box:
        return box if self.frame is None else self.frame.to_pixels(box)

    def from_pixels(self, box: Box) -> Box:
        return box if self.frame is None else self.frame.from_pixels(box)


class PictureSource(Protocol):
    async def read(self) -> Picture:
        """The picture; FinderInputError when it cannot be what was asked, PictureReadError when reading failed."""
        ...


def check_pixels(pixels: Any) -> np.ndarray:
    """A picture given as an array must be height x width x 3, 8 bits a channel (RGB)."""
    if not isinstance(pixels, np.ndarray) or pixels.ndim != 3 or pixels.shape[2] != 3 or pixels.dtype != np.uint8:
        raise FinderInputError("a picture must be an RGB image with 8 bits a channel")
    if pixels.shape[0] < 1 or pixels.shape[1] < 1:
        raise FinderInputError("the picture is empty")
    return pixels


def decode_photo(data: bytes) -> np.ndarray:
    """A photo file (JPEG, PNG, ...) as RGB pixels, turned upright as its EXIF orientation says (as the finder's
    measurement read its pictures, and as a browser shows them)."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_PHOTO_PIXELS:
                raise FinderInputError(f"the photo is {image.width} x {image.height} px; at most "
                                       f"{MAX_PHOTO_PIXELS:,} pixels are read")
            return np.asarray(ImageOps.exif_transpose(image).convert("RGB"))
    except FinderInputError:
        raise
    except Exception as e:  # PIL raises several kinds for a file that is not a picture
        raise FinderInputError(f"the photo could not be read as a picture ({type(e).__name__})") from e


@dataclass(frozen=True)
class Photo:
    """A photo given as bytes; boxes are in its own pixels (after its EXIF orientation is applied)."""

    data: bytes
    view: str  # answer_levels.OBLIQUE or NADIR, as the caller knows it (never guessed here)

    async def read(self) -> Picture:
        view = check_view(self.view)
        pixels = await asyncio.to_thread(decode_photo, self.data)
        return Picture(pixels=pixels, sha256=hashlib.sha256(self.data).hexdigest(), view=view)


@dataclass(frozen=True)
class InspectionPhoto:
    """An original photo of a photo inspection: only a photo stored under the prefix recorded on this layer
    (photo_inspections.stored_prefix checks the prefix is the layer's own), named by its sha256. Boxes are in its own
    pixels as shown (EXIF orientation applied); `view` is said by the caller (photo_inspections.view_of)."""

    s3: Any
    bucket: str
    layer_id: str
    layer_metadata: dict[str, Any]
    sha256: str
    view: str

    async def read(self) -> Picture:
        prefix = photo_inspections.stored_prefix(self.layer_metadata, self.layer_id)
        if prefix is None:
            raise FinderInputError("this layer is not a photo inspection")
        if not photo_inspections.is_sha256(self.sha256):
            raise FinderInputError("a photo is named by its SHA-256: 64 lower-case hexadecimal characters")
        try:
            key = await photo_inspections.stored_photo_key(self.s3, self.bucket, prefix, self.sha256)
            if key is None:
                raise FinderInputError("this inspection has no such photo")
            response = await self.s3.get_object(Bucket=self.bucket, Key=key)
            if response["ContentLength"] > MAX_PHOTO_BYTES:
                raise FinderInputError(f"the photo is {response['ContentLength']:,} bytes; at most "
                                       f"{MAX_PHOTO_BYTES:,} are read")
            async with response["Body"] as body:
                data = await body.read()
        except FinderInputError:
            raise
        except Exception as e:  # storage or network: asking again may work
            raise PictureReadError(f"the photo could not be read from storage ({type(e).__name__})") from e
        picture = await Photo(data=data, view=self.view).read()
        if picture.sha256 != self.sha256:
            raise PictureReadError("the stored photo is not the one its name says")
        return picture


def _window_raw(url: str, bbox: Box, bbox_crs: Optional[str], max_px: int, env: dict[str, str]) -> tuple[str, Any]:
    """In a raster worker: ("picture", (rgb, transform, crs_wkt)), ("outside", None), ("too_big", (cols, rows)),
    ("not_rgb", (bands, dtype)) or ("error", message)."""
    try:
        with rasterio.Env(**env), rasterio.open(url) as src:
            if src.count < 3 or src.dtypes[0] != "uint8":
                return "not_rgb", (src.count, src.dtypes[0])
            bounds = transform_bounds(bbox_crs, src.crs, *bbox, densify_pts=_DENSIFY) if bbox_crs else bbox
            w = from_bounds(*bounds, transform=src.transform)
            col0 = max(0, math.floor(w.col_off + 1e-6))
            row0 = max(0, math.floor(w.row_off + 1e-6))
            col1 = min(src.width, math.ceil(w.col_off + w.width - 1e-6))
            row1 = min(src.height, math.ceil(w.row_off + w.height - 1e-6))
            if col1 <= col0 or row1 <= row0:
                return "outside", None
            if col1 - col0 > max_px or row1 - row0 > max_px:
                return "too_big", (col1 - col0, row1 - row0)
            window = Window(col0, row0, col1 - col0, row1 - row0)
            bands = src.read([1, 2, 3], window=window)
            transform = tuple(src.window_transform(window))[:6]
            return "picture", (np.ascontiguousarray(bands.transpose(1, 2, 0)), transform, src.crs.to_wkt())
    except Exception as e:  # returned, not raised: the caller decides and logs (raster_process contract)
        return "error", f"{type(e).__name__}: {e}"


def window_sha256(pixels: np.ndarray, frame: MapFrame) -> str:
    """The pixels and where they are: the same pixels in another frame give boxes in other coordinates."""
    digest = hashlib.sha256(b"orthophoto-window\0")
    digest.update(json.dumps({"frame": frame.as_dict(), "shape": list(pixels.shape)}, sort_keys=True).encode())
    digest.update(np.ascontiguousarray(pixels).tobytes())
    return digest.hexdigest()


def check_window(bbox: Sequence[Any], crs: str) -> Box:
    if crs not in FRAMES:
        raise FinderInputError(f"the window's coordinates must be one of {list(FRAMES)}, not {crs!r}")
    if len(bbox) != 4 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                                 for v in bbox):
        raise FinderInputError("the window must be four numbers: west, south, east, north")
    x0, y0, x1, y1 = (float(v) for v in bbox)
    if not (x0 < x1 and y0 < y1):
        raise FinderInputError("the window's west must be less than its east, and its south less than its north")
    if crs == LONLAT and not (-180 <= x0 and x1 <= 180 and -90 <= y0 and y1 <= 90):
        raise FinderInputError("a lon/lat window must lie within -180..180 and -90..90")
    return (x0, y0, x1, y1)


@dataclass(frozen=True)
class OrthophotoWindow:
    """A window of an orthophoto's optimised photo (COG), read at the photo's own resolution. An orthophoto looks
    straight down (NADIR)."""

    cog_url: str
    bbox: Sequence[float]  # west, south, east, north in `crs`
    crs: str = LONLAT

    async def read(self) -> Picture:
        bbox = check_window(self.bbox, self.crs)
        bbox_crs = _LONLAT_CRS if self.crs == LONLAT else None
        try:
            kind, got = await asyncio.to_thread(
                raster_process.run, _window_raw, self.cog_url, bbox, bbox_crs, MAX_WINDOW_PX, GDAL_HTTP_TIMEOUTS)
        except Exception as e:  # the worker died or never answered
            raise PictureReadError(f"the photo could not be read (raster worker: {e!r})") from e
        if kind == "error":
            raise PictureReadError(f"the photo could not be read ({got})")
        if kind == "not_rgb":
            raise FinderInputError(f"the finder needs a colour photo with 8 bits a channel; this one has {got[0]} "
                                   f"band(s) of {got[1]}")
        if kind == "outside":
            raise FinderInputError("the window does not overlap the photo")
        if kind == "too_big":
            raise FinderInputError(f"the window is {got[0]} x {got[1]} px of the photo; at most {MAX_WINDOW_PX} x "
                                   f"{MAX_WINDOW_PX} px are read at once, so choose a smaller window")
        pixels, transform, crs_wkt = got
        frame = MapFrame(crs=self.crs, layer_crs=crs_wkt, transform=tuple(float(v) for v in transform))
        return Picture(pixels=pixels, sha256=window_sha256(pixels, frame), view=answer_levels.NADIR, frame=frame)
