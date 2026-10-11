"""Small photos made in the test, shaped like a DJI drone's: EXIF GPS (as Pillow writes it, rationals of degrees,
minutes and seconds), camera make and model, DateTimeOriginal, and DJI's XMP packet."""

from __future__ import annotations

import io
import zipfile
from typing import Optional

from PIL import Image, ExifTags

# A packet as DJI writes it (attributes on rdf:Description, signed values as text).
DJI_XMP = (
    b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description rdf:about="DJI Meta Data" xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/"'
    b' drone-dji:AbsoluteAltitude="+1532.20" drone-dji:RelativeAltitude="+45.20"'
    b' drone-dji:GimbalRollDegree="+0.00" drone-dji:GimbalYawDegree="-87.30" drone-dji:GimbalPitchDegree="-30.10"'
    b' drone-dji:FlightRollDegree="+1.20" drone-dji:FlightYawDegree="-88.10" drone-dji:FlightPitchDegree="+2.00"/>'
    b'</rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
)


def _dms(value: float) -> tuple[float, float, float]:
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = (value - degrees - minutes / 60) * 3600
    return float(degrees), float(minutes), round(seconds, 4)


def drone_jpg(lat: Optional[float] = None, lon: Optional[float] = None, *, altitude: Optional[float] = None,
              xmp: Optional[bytes] = None, make: Optional[str] = "DJI", model: Optional[str] = "M3E",
              taken: Optional[str] = "2026:10:09 14:03:22", offset: Optional[str] = None,
              gps_extra: Optional[dict] = None, size: tuple[int, int] = (64, 48), fmt: str = "JPEG") -> bytes:
    """A small photo. With lat/lon, EXIF GPS in the usual N/S, E/W form; altitude below 0 is written with
    GPSAltitudeRef 1 (below sea level)."""
    image = Image.new("RGB", size, (90, 140, 60))
    exif = Image.Exif()
    if make:
        exif[271] = make
    if model:
        exif[272] = model
    if taken:
        exif.get_ifd(ExifTags.IFD.Exif)[36867] = taken
    if offset:
        exif.get_ifd(ExifTags.IFD.Exif)[36881] = offset
    gps = {}
    if lat is not None and lon is not None:
        gps.update({1: "S" if lat < 0 else "N", 2: _dms(abs(lat)), 3: "W" if lon < 0 else "E", 4: _dms(abs(lon))})
    if altitude is not None:
        gps.update({5: 1 if altitude < 0 else 0, 6: abs(altitude)})
    gps.update(gps_extra or {})
    if gps:
        exif.get_ifd(ExifTags.IFD.GPSInfo).update(gps)
    out = io.BytesIO()
    extra = {"xmp": xmp} if xmp else {}
    image.save(out, fmt, exif=exif, **extra)
    return out.getvalue()


def photo_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """A ZIP holding (name, bytes) entries, as a phone or a computer would make it."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return out.getvalue()
