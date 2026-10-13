"""Photo inspections: a set of drone or field photos (JPG/PNG) uploaded as one layer of camera positions.

The one owner of what a photo inspection is (rules: docs/rules/PHOTO_INSPECTIONS_RULES.md): which files count as
photos, the limits on one inspection, the facts read from each photo (EXIF, and DJI's XMP flight and gimbal
values), where the originals are stored, and the GeoJSON of camera points the map shows.

Every fact is read from the photo itself. A value the photo does not carry is None, never 0.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import struct
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from src.services import answer_levels, stored_files

# The switch: photo inspections are off unless PHOTO_INSPECTIONS is set to a true value.
_SWITCH = "PHOTO_INSPECTIONS"

PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
# What Pillow must find inside a file with each extension (DJI writes some JPGs as MPO, a multi-picture JPEG).
_FORMATS = {".jpg": {"JPEG", "MPO"}, ".jpeg": {"JPEG", "MPO"}, ".png": {"PNG"}}

# What shows a ZIP is a 3D model (a mesh and its textures, as model downloads and photogrammetry exports package one).
# Ingabe takes 3D only as a point cloud (a LAS or LAZ file; Roger, 2026-10-11), so such a ZIP is refused, and named
# as a 3D model, when it has no photo with a GPS position. Only file names are read: a ZIP inside the ZIP is not opened.
MODEL_EXTENSIONS = frozenset({".obj", ".mtl", ".fbx", ".gltf", ".glb", ".ply", ".stl", ".dae", ".3ds", ".usdz", ".blend",
                              ".osgb", ".b3dm", ".slpk"})
TEXTURE_FOLDERS = frozenset({"textures", "texture"})
MODEL_SOURCE_FOLDERS = frozenset({"source"})  # a model download keeps the model (often zipped) in source/

# Limits on one inspection (a ZIP unpacks no more than this).
MAX_PHOTOS = 1_000
MAX_UNPACKED_BYTES = 15 * 1024 ** 3  # 15 GiB

_STORAGE_ROOT = "inspections"
# Every photo's facts, kept beside the originals when the inspection is uploaded (camera points hold only the photos
# with a GPS position). Not a photo name, so photo_key_in never matches it.
FACTS_NAME = "facts.json"
_FACTS_VERSION = 1
_SEGMENT = r"[A-Za-z0-9_-]+"
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Stored photos deleted at the same time.
_DELETE_CONCURRENCY = 8
# A JPEG keeps its XMP packet near the start of the file (an APP segment); a PNG in a text chunk before the pixels.
_XMP_SCAN_BYTES = 1024 * 1024
# How much of an XMP packet is searched, and the longest value text read. DJI writes its values in the first few KB;
# the caps keep the search short whatever a file holds (a 1 KB PNG can carry a 1,000,000-character packet).
_XMP_MAX_CHARS = 256 * 1024
_XMP_MAX_VALUE = 64

# EXIF tags (TIFF/EXIF 2.32).
_MAKE, _MODEL, _ORIENTATION, _DATETIME = 271, 272, 274, 306
# EXIF orientations that turn the picture a quarter: shown upright, its width and height swap.
_QUARTER_TURNS = frozenset({5, 6, 7, 8})
_EXIF_IFD, _GPS_IFD = 0x8769, 0x8825
_DATETIME_ORIGINAL, _OFFSET_TIME_ORIGINAL = 36867, 36881
_GPS_LAT_REF, _GPS_LAT, _GPS_LON_REF, _GPS_LON, _GPS_ALT_REF, _GPS_ALT, _GPS_STATUS = 1, 2, 3, 4, 5, 6, 9

# DJI's XMP names (namespace drone-dji) for the values EXIF does not hold.
_XMP_FIELDS = {
    "relative_altitude_m": "RelativeAltitude",
    "gimbal_pitch": "GimbalPitchDegree",
    "gimbal_yaw": "GimbalYawDegree",
    "flight_yaw": "FlightYawDegree",
}


# Which way a photo looked, from DJI's gimbal pitch (degrees; 0 is level, -90 straight down). A cut-off set with the
# towers spec on 2026-10-11, not measured: a photo within this many degrees of straight down looked straight down
# (answer_levels.NADIR), any other pitch at an angle (OBLIQUE). No pitch: not known (None), never guessed.
NADIR_WITHIN_DEG = 10.0


class UnreadablePhoto(ValueError):
    """A file with a photo's extension that cannot be read as a photo."""


class NoPhotoPosition(ValueError):
    """None of the photos carries a GPS position, so the inspection has nowhere to go on the map."""


class ModelNotPhotos(NoPhotoPosition):
    """The ZIP is a 3D model (model files, textures), and none of its pictures is a photo with a GPS position."""


@dataclass(frozen=True)
class PhotoFacts:
    """What one photo says about itself. Angles are degrees; altitudes metres (altitude_m above sea level as the
    GPS recorded it, relative_altitude_m above the take-off point as DJI records it)."""

    sha256: str
    filename: str
    width: int
    height: int
    taken_at: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    altitude_m: Optional[float]
    relative_altitude_m: Optional[float]
    gimbal_pitch: Optional[float]
    gimbal_yaw: Optional[float]
    flight_yaw: Optional[float]
    make: Optional[str]
    model: Optional[str]
    orientation: Optional[int] = None  # EXIF orientation (1-8); None when the photo has none

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def upright_size(self) -> tuple[int, int]:
        """(width, height) as the photo is shown: its EXIF orientation applied, as a browser and the example
        finder read it. Boxes on a photo are in these pixels."""
        if self.orientation in _QUARTER_TURNS:
            return self.height, self.width
        return self.width, self.height

    @property
    def extension(self) -> str:
        """The stored copy's extension: the original's, lower case."""
        return os.path.splitext(self.filename)[1].lower()


def enabled() -> bool:
    """True when photo inspections are switched on (PHOTO_INSPECTIONS=1/true/yes/on). Off by default."""
    return os.environ.get(_SWITCH, "0").strip().lower() in {"1", "true", "yes", "on"}


def is_photo_name(name: str) -> bool:
    """True for a file name with a photo extension. macOS metadata files ("._DJI_0001.JPG") are not photos."""
    base = os.path.basename(name)
    return not base.startswith("._") and os.path.splitext(base)[1].lower() in PHOTO_EXTENSIONS


def _has_shapefile(names: list[str]) -> bool:
    return any(name.lower().endswith(".shp") for name in names)


def is_photo_set(names: Iterable[str]) -> bool:
    """True when an archive with these file names is a photo inspection: at least one photo and no shapefile (a
    zipped shapefile keeps the vector path). Other files (flight logs, videos) are left out of the inspection."""
    names = list(names)
    if _has_shapefile(names):
        return False
    return any(is_photo_name(name) for name in names)


def _folders_and_base(name: str) -> tuple[list[str], str]:
    """The folders and the file name of a name inside an archive, in lower case, / or \\ separated."""
    *folders, base = (part.lower() for part in re.split(r"[\\/]", name))
    return folders, base


def _is_texture(name: str) -> bool:
    """True for a picture in a textures folder (a 3D model's textures, not photos of anything)."""
    folders, _base = _folders_and_base(name)
    return is_photo_name(name) and bool(TEXTURE_FOLDERS.intersection(folders))


def model_in(names: Iterable[str]) -> Optional[str]:
    """What in an archive with these file names shows it is a 3D model, in words ("source/model.zip and 8 images in
    a textures folder"); None when nothing does. A model file (MODEL_EXTENSIONS), a ZIP in a source folder and the
    pictures in a textures folder count (folders matched in any case, / or \\ separated); macOS metadata ("._name")
    does not. Names only: a ZIP inside the ZIP is not opened."""
    models: list[str] = []
    textures = 0
    for name in names:
        folders, base = _folders_and_base(name)
        if base.startswith("._"):
            continue
        extension = os.path.splitext(base)[1]
        if extension in MODEL_EXTENSIONS or (extension == ".zip" and MODEL_SOURCE_FOLDERS.intersection(folders)):
            models.append(name)
        elif _is_texture(name):
            textures += 1
    found = []
    if models:
        more = len(models) - 1
        found.append(models[0] + (f" (and {more} more model file{'s' if more > 1 else ''})" if more else ""))
    if textures:
        found.append(f"{textures} image{'s' if textures > 1 else ''} in a textures folder")
    return " and ".join(found) or None


def is_model_archive(names: Iterable[str]) -> bool:
    """True when an archive with these file names holds a 3D model (model_in) and no shapefile. With the switch on,
    such a ZIP goes to the photo inspection path even without a photo, only to be refused there as a 3D model
    (check_not_a_model): the vector path would refuse it too, as "no shapefile", so what is accepted does not
    change."""
    names = list(names)
    return not _has_shapefile(names) and model_in(names) is not None


def check_not_a_model(archive_names: Iterable[str]) -> None:
    """Raise ModelNotPhotos when an archive with these file names holds a 3D model. Called only where the upload is
    already refused (no photo has a GPS position: check_placeable; a picture could not be read:
    check_unreadable_not_a_model), so it changes the message, never what is accepted."""
    what = model_in(archive_names)
    if what:
        raise ModelNotPhotos(f"This ZIP is a 3D model, not photos: it holds {what}. Ingabe takes 3D only as a LAS or "
                             "LAZ point cloud; for a photo inspection, upload the photos with their GPS.")


def check_unreadable_not_a_model(archive_names: Iterable[str]) -> None:
    """Raise ModelNotPhotos when a picture in an archive with these file names could not be read (or none could be
    unpacked) and the archive is a 3D model whose only pictures are its textures. An archive with a photo outside a
    textures folder holds photos, so it is not "a 3D model, not photos": nothing is raised here and the refusal keeps
    naming the file that could not be read."""
    names = list(archive_names)
    if not any(is_photo_name(name) and not _is_texture(name) for name in names):
        check_not_a_model(names)


def read_photo(path: str, filename: str) -> PhotoFacts:
    """The facts of the photo at `path` (`filename` is how the user named it). Reads the file once to hash it and
    the headers only (not the pixels) for the rest. Raises UnreadablePhoto for a file that is not a photo."""
    from PIL import Image  # lazy: Pillow is heavy and only photo uploads need it

    sha256 = stored_files.file_sha256(path)
    try:
        with Image.open(path) as image:
            found_format = image.format
            width, height = image.size
            exif = image.getexif()
            gps = exif.get_ifd(_GPS_IFD)
            exif_ifd = exif.get_ifd(_EXIF_IFD)
            xmp = _xmp_packet(image.info, path)
    except Image.DecompressionBombError as e:
        raise UnreadablePhoto(f"{filename} has more pixels than the photo reader accepts") from e
    except (OSError, SyntaxError, ValueError, struct.error) as e:  # UnidentifiedImageError is an OSError
        raise UnreadablePhoto(f"{filename} could not be read as a photo") from e
    if found_format not in _FORMATS.get(os.path.splitext(filename)[1].lower(), ()):
        raise UnreadablePhoto(f"{filename} is not a JPEG or PNG photo")

    lat, lon = _gps_position(gps)
    return PhotoFacts(
        sha256=sha256,
        filename=filename,
        width=width,
        height=height,
        taken_at=_taken_at(exif_ifd.get(_DATETIME_ORIGINAL) or exif.get(_DATETIME),
                           exif_ifd.get(_OFFSET_TIME_ORIGINAL)),
        lat=lat,
        lon=lon,
        altitude_m=_gps_altitude(gps),
        relative_altitude_m=_xmp_number(xmp, _XMP_FIELDS["relative_altitude_m"]),
        gimbal_pitch=_xmp_number(xmp, _XMP_FIELDS["gimbal_pitch"]),
        gimbal_yaw=_xmp_number(xmp, _XMP_FIELDS["gimbal_yaw"]),
        flight_yaw=_xmp_number(xmp, _XMP_FIELDS["flight_yaw"]),
        make=_text(exif.get(_MAKE)),
        model=_text(exif.get(_MODEL)),
        orientation=_orientation(exif.get(_ORIENTATION)),
    )


def read_photos(photos: Iterable[tuple[str, str]]) -> list[PhotoFacts]:
    """The facts of each (path, filename), in the same order. Raises UnreadablePhoto for a file that is not a
    photo."""
    return [read_photo(path, filename) for path, filename in photos]


def view_of(facts: PhotoFacts) -> Optional[str]:
    """Which way the photo looked (answer_levels.NADIR or OBLIQUE) from its gimbal pitch; None when the photo does
    not say (no DJI gimbal pitch: a phone photo, another camera)."""
    if facts.gimbal_pitch is None:
        return None
    return answer_levels.NADIR if facts.gimbal_pitch <= -90 + NADIR_WITHIN_DEG else answer_levels.OBLIQUE


def check_placeable(facts: list[PhotoFacts], archive_names: Iterable[str] = ()) -> None:
    """Raise NoPhotoPosition when no photo has a GPS position: the inspection would have nowhere to go on the
    map. When the ZIP (its file names: archive_names) is a 3D model, the refusal says so (ModelNotPhotos)."""
    if not any(f.has_position for f in facts):
        check_not_a_model(archive_names)
        if len(facts) == 1:
            raise NoPhotoPosition("This photo has no map position and no GPS position, so it cannot be placed "
                                  "on the map.")
        raise NoPhotoPosition(f"None of the {len(facts)} photos has a GPS position, so the inspection cannot be "
                              "placed on the map.")


def storage_prefix(project_id: str, layer_id: str) -> str:
    """Where the original photos of this inspection are stored."""
    return f"{_STORAGE_ROOT}/{project_id}/{layer_id}/"


def photo_key(prefix: str, facts: PhotoFacts) -> str:
    """The storage key of one original photo: named by its content, so the same photo is stored once."""
    return f"{prefix}{facts.sha256}{facts.extension}"


def facts_key(prefix: str) -> str:
    """Where every photo's facts are kept for the inspection stored under `prefix`."""
    return f"{prefix}{FACTS_NAME}"


async def save_facts(s3: Any, bucket: str, prefix: str, facts: list[PhotoFacts]) -> None:
    """Keep every photo's facts (in upload order) beside the originals."""
    body = json.dumps({"version": _FACTS_VERSION, "photos": [asdict(f) for f in facts]}).encode()
    await s3.put_object(Bucket=bucket, Key=facts_key(prefix), Body=body, ContentType="application/json")


async def load_facts(s3: Any, bucket: str, prefix: str) -> Optional[list[PhotoFacts]]:
    """Every photo's facts, in upload order; None when the inspection has none kept (uploaded before they were
    kept, 2026-10-11). A storage error raises."""
    record = await stored_files.load_json(s3, bucket, facts_key(prefix))
    if record is None:
        return None
    if record.get("version") != _FACTS_VERSION:
        raise ValueError(f"the photo facts under {prefix!r} are version {record.get('version')!r}, "
                         f"not {_FACTS_VERSION}")
    return [PhotoFacts(**row) for row in record["photos"]]


def is_sha256(value: str) -> bool:
    return bool(_SHA256.fullmatch(value))


def stored_prefix(layer_metadata: dict, layer_id: str) -> Optional[str]:
    """The storage prefix recorded on a photo-inspection layer, or None when the layer is not one (or the recorded
    prefix is not this layer's own)."""
    inspection = layer_metadata.get("photo_inspection") if isinstance(layer_metadata, dict) else None
    prefix = inspection.get("storage_prefix") if isinstance(inspection, dict) else None
    if not isinstance(prefix, str) or not _is_layers_prefix(prefix, layer_id):
        return None
    return prefix


async def forget_if_unused(conn: Any, s3: Any, bucket: str, prefix: str, layer_id: str) -> list[str]:
    """Delete the original photos stored under `prefix`, the inspection of layer `layer_id`, once no map_layers row
    holds that layer: after its row is deleted, or after an upload that did not create it. A retried upload that
    fails because the layer already exists leaves the photos alone.

    Call on a connection without a user (row security would hide the row). Returns the deleted keys. Raises
    ValueError for a prefix that is not this layer's, and on a storage error (what is left is only storage)."""
    if not _is_layers_prefix(prefix, layer_id):
        raise ValueError(f"{prefix!r} is not where the photos of layer {layer_id!r} are stored")
    if await conn.fetchval("SELECT count(*) FROM map_layers WHERE layer_id = $1", layer_id):
        return []
    keys = await _keys_under(s3, bucket, prefix)
    limit = asyncio.Semaphore(_DELETE_CONCURRENCY)

    async def delete(key: str) -> None:
        async with limit:
            await s3.delete_object(Bucket=bucket, Key=key)

    await asyncio.gather(*(delete(key) for key in keys))
    return keys


def photo_key_in(prefix: str, sha256: str, keys: Iterable[str]) -> Optional[str]:
    """The key among `keys` that is the photo `sha256` of the inspection stored under `prefix`, or None."""
    extensions = "|".join(re.escape(ext) for ext in sorted(PHOTO_EXTENSIONS))
    pattern = re.compile(rf"{re.escape(prefix)}{re.escape(sha256)}(?:{extensions})")
    return next((key for key in keys if pattern.fullmatch(key)), None)


async def stored_photo_key(s3: Any, bucket: str, prefix: str, sha256: str) -> Optional[str]:
    """The storage key of the photo `sha256` of the inspection stored under `prefix`, or None when it has no such
    photo. A storage error raises."""
    listing = await s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}{sha256}", MaxKeys=10)
    return photo_key_in(prefix, sha256, [o["Key"] for o in listing.get("Contents", [])])


def content_type(key: str) -> str:
    """The media type a stored photo is served with."""
    return CONTENT_TYPES[os.path.splitext(key)[1].lower()]


def camera_points(facts: Iterable[PhotoFacts], prefix: str) -> dict:
    """A GeoJSON FeatureCollection with one Point per photo that has a GPS position; properties are the photo's
    facts and its storage key."""
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [f.lon, f.lat]},
            "properties": {**asdict(f), "photo_key": photo_key(prefix, f)},
        }
        for f in facts if f.has_position
    ]
    return {"type": "FeatureCollection", "features": features}


def summary(facts: list[PhotoFacts], prefix: str) -> dict:
    """What the layer records about its inspection."""
    return {
        "photos": len(facts),
        "with_gps": sum(1 for f in facts if f.has_position),
        "without_gps": [f.filename for f in facts if not f.has_position],
        "storage_prefix": prefix,
    }


def _is_layers_prefix(prefix: str, layer_id: str) -> bool:
    """True when `prefix` is shaped like storage_prefix(<a project>, layer_id)."""
    return (re.fullmatch(_SEGMENT, layer_id) is not None
            and re.fullmatch(rf"{_STORAGE_ROOT}/{_SEGMENT}/{re.escape(layer_id)}/", prefix) is not None)


async def _keys_under(s3: Any, bucket: str, prefix: str) -> list[str]:
    """Every stored key under `prefix` (a listing returns at most 1,000 keys a page)."""
    keys: list[str] = []
    page_from: dict = {}
    while True:
        page = await s3.list_objects_v2(Bucket=bucket, Prefix=prefix, **page_from)
        keys += [item["Key"] for item in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            return keys
        page_from = {"ContinuationToken": page["NextContinuationToken"]}


def _text(value) -> Optional[str]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    value = value.replace("\x00", "").strip()
    return value or None


def _orientation(value) -> Optional[int]:
    """EXIF orientation 1-8; None when missing or not one of them."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 8 else None


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if math.isfinite(number) else None


def _degrees(value) -> Optional[float]:
    """Degrees from EXIF's (degrees, minutes, seconds) rationals."""
    if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 3:
        return None
    parts = [_finite(v) for v in value]
    if any(p is None for p in parts):
        return None
    return sum(p / 60 ** i for i, p in enumerate(parts))


def _gps_position(gps: dict) -> tuple[Optional[float], Optional[float]]:
    """(lat, lon) in WGS84 degrees, or (None, None) when the photo has no usable fix: no coordinates, no
    hemisphere, a receiver that marked the fix void, or values off the globe."""
    if _text(gps.get(_GPS_STATUS)) == "V":
        return None, None
    lat, lon = _degrees(gps.get(_GPS_LAT)), _degrees(gps.get(_GPS_LON))
    lat_ref, lon_ref = _text(gps.get(_GPS_LAT_REF)), _text(gps.get(_GPS_LON_REF))
    if lat is None or lon is None or lat_ref not in ("N", "S") or lon_ref not in ("E", "W"):
        return None, None
    lat = -lat if lat_ref == "S" else lat
    lon = -lon if lon_ref == "W" else lon
    if abs(lat) > 90 or abs(lon) > 180:
        return None, None
    return lat, lon


def _gps_altitude(gps: dict) -> Optional[float]:
    altitude = _finite(gps.get(_GPS_ALT))
    if altitude is None:
        return None
    ref = gps.get(_GPS_ALT_REF)
    below_sea_level = ref in (1, b"\x01")
    return -altitude if below_sea_level else altitude


def _taken_at(value, offset) -> Optional[str]:
    """ISO 8601 from EXIF's "YYYY:MM:DD HH:MM:SS" (with its UTC offset when the camera wrote one)."""
    text = _text(value)
    if text is None:
        return None
    try:
        moment = datetime.strptime(text[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    offset_text = _text(offset)
    suffix = offset_text if offset_text and re.fullmatch(r"[+-]\d{2}:\d{2}", offset_text) else ""
    return moment.isoformat() + suffix


def _xmp_packet(info: dict, path: str) -> str:
    """The first _XMP_MAX_CHARS of the photo's XMP packet as text ("" when it has none): from Pillow's reading of
    the file when it found one, else from the start of the file."""
    for key in ("xmp", "XML:com.adobe.xmp"):
        packet = info.get(key)
        if isinstance(packet, bytes):
            return packet[:_XMP_MAX_CHARS].decode("utf-8", errors="replace")
        if isinstance(packet, str) and packet:
            return packet[:_XMP_MAX_CHARS]
    with open(path, "rb") as f:
        head = f.read(_XMP_SCAN_BYTES)
    start = head.find(b"<x:xmpmeta")
    end = head.find(b"</x:xmpmeta>", start)
    if start < 0 or end < 0:
        return ""
    return head[start:min(end, start + _XMP_MAX_CHARS)].decode("utf-8", errors="replace")


def _xmp_number(packet: str, name: str) -> Optional[float]:
    """A number from the XMP packet, written as an attribute (drone-dji:Name="+12.3") or an element
    (<drone-dji:Name>+12.3</drone-dji:Name>). Parsed as text: no XML parser, so no entity expansion.

    Both patterns start at a fixed character (the ':' before the name, checked to follow a prefix character, or
    the '<' of the element), so the search is linear in the packet: a pattern starting with `[\\w.-]+` retried
    every position of a long run of letters and took minutes on a crafted packet, holding the GIL throughout."""
    match = (re.search(rf'(?<=[\w.-]):{name}\s*=\s*"([^"]{{0,{_XMP_MAX_VALUE}}})"', packet)
             or re.search(rf"<[\w.-]+:{name}>([^<]{{0,{_XMP_MAX_VALUE}}})<", packet))
    return _finite(match.group(1).strip()) if match else None
