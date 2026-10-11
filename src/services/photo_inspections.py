"""Photo inspections: a set of drone or field photos (JPG/PNG) uploaded as one layer of camera positions.

The one owner of what a photo inspection is (rules: docs/rules/PHOTO_INSPECTIONS_RULES.md): which files count as
photos, the limits on one inspection, the facts read from each photo (EXIF, and DJI's XMP flight and gimbal
values), where the originals are stored, and the GeoJSON of camera points the map shows.

Every fact is read from the photo itself. A value the photo does not carry is None, never 0.
"""

from __future__ import annotations

import math
import os
import re
import struct
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable, Optional

from src.services import photo_content

# The switch: photo inspections are off unless PHOTO_INSPECTIONS is set to a true value.
_SWITCH = "PHOTO_INSPECTIONS"

PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
# What Pillow must find inside a file with each extension (DJI writes some JPGs as MPO, a multi-picture JPEG).
_FORMATS = {".jpg": {"JPEG", "MPO"}, ".jpeg": {"JPEG", "MPO"}, ".png": {"PNG"}}

# Limits on one inspection (a ZIP unpacks no more than this).
MAX_PHOTOS = 1_000
MAX_UNPACKED_BYTES = 15 * 1024 ** 3  # 15 GiB

_STORAGE_ROOT = "inspections"
_SEGMENT = r"[A-Za-z0-9_-]+"
_SHA256 = re.compile(r"[0-9a-f]{64}")
# A JPEG keeps its XMP packet near the start of the file (an APP segment); a PNG in a text chunk before the pixels.
_XMP_SCAN_BYTES = 1024 * 1024

# EXIF tags (TIFF/EXIF 2.32).
_MAKE, _MODEL, _DATETIME = 271, 272, 306
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


class UnreadablePhoto(ValueError):
    """A file with a photo's extension that cannot be read as a photo."""


class NoPhotoPosition(ValueError):
    """None of the photos carries a GPS position, so the inspection has nowhere to go on the map."""


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

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None

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


def is_photo_set(names: Iterable[str]) -> bool:
    """True when an archive with these file names is a photo inspection: at least one photo and no shapefile (a
    zipped shapefile keeps the vector path). Other files (flight logs, videos) are left out of the inspection."""
    names = list(names)
    if any(name.lower().endswith(".shp") for name in names):
        return False
    return any(is_photo_name(name) for name in names)


def read_photo(path: str, filename: str) -> PhotoFacts:
    """The facts of the photo at `path` (`filename` is how the user named it). Reads the file once to hash it and
    the headers only (not the pixels) for the rest. Raises UnreadablePhoto for a file that is not a photo."""
    from PIL import Image  # lazy: Pillow is heavy and only photo uploads need it

    sha256 = photo_content.file_sha256(path)
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
    )


def read_photos(photos: Iterable[tuple[str, str]]) -> list[PhotoFacts]:
    """The facts of each (path, filename), in the same order. Raises UnreadablePhoto for a file that is not a
    photo."""
    return [read_photo(path, filename) for path, filename in photos]


def check_placeable(facts: list[PhotoFacts]) -> None:
    """Raise NoPhotoPosition when no photo has a GPS position: the inspection would have nowhere to go on the
    map."""
    if not any(f.has_position for f in facts):
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


def is_sha256(value: str) -> bool:
    return bool(_SHA256.fullmatch(value))


def stored_prefix(layer_metadata: dict, layer_id: str) -> Optional[str]:
    """The storage prefix recorded on a photo-inspection layer, or None when the layer is not one (or the recorded
    prefix is not this layer's own)."""
    inspection = layer_metadata.get("photo_inspection") if isinstance(layer_metadata, dict) else None
    prefix = inspection.get("storage_prefix") if isinstance(inspection, dict) else None
    if not isinstance(prefix, str):
        return None
    if not re.fullmatch(rf"{_STORAGE_ROOT}/{_SEGMENT}/{re.escape(layer_id)}/", prefix):
        return None
    return prefix


def photo_key_in(prefix: str, sha256: str, keys: Iterable[str]) -> Optional[str]:
    """The key among `keys` that is the photo `sha256` of the inspection stored under `prefix`, or None."""
    extensions = "|".join(re.escape(ext) for ext in sorted(PHOTO_EXTENSIONS))
    pattern = re.compile(rf"{re.escape(prefix)}{re.escape(sha256)}(?:{extensions})")
    return next((key for key in keys if pattern.fullmatch(key)), None)


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


def _text(value) -> Optional[str]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    value = value.replace("\x00", "").strip()
    return value or None


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
    """The photo's XMP packet as text ("" when it has none): from Pillow's reading of the file when it found one,
    else from the start of the file."""
    for key in ("xmp", "XML:com.adobe.xmp"):
        packet = info.get(key)
        if isinstance(packet, bytes):
            return packet.decode("utf-8", errors="replace")
        if isinstance(packet, str) and packet:
            return packet
    with open(path, "rb") as f:
        head = f.read(_XMP_SCAN_BYTES)
    start = head.find(b"<x:xmpmeta")
    end = head.find(b"</x:xmpmeta>", start)
    if start < 0 or end < 0:
        return ""
    return head[start:end].decode("utf-8", errors="replace")


def _xmp_number(packet: str, name: str) -> Optional[float]:
    """A number from the XMP packet, written as an attribute (drone-dji:Name="+12.3") or an element
    (<drone-dji:Name>+12.3</drone-dji:Name>). Parsed as text: no XML parser, so no entity expansion."""
    match = (re.search(rf"[\w.-]+:{name}\s*=\s*\"([^\"]*)\"", packet)
             or re.search(rf"<[\w.-]+:{name}>([^<]*)<", packet))
    return _finite(match.group(1).strip()) if match else None
