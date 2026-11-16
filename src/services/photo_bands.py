"""Which band of a drone photo is which: red, green, blue, red edge, near-infrared.

A colour photo is red, green, blue in that order (a fourth band is transparency).
A multispectral photo has no fixed order: one maker stacks blue first, another green
first. So for a file with five or more bands, or one that names its bands, the roles
are read from the file itself (each band's description, its colour interpretation, or
a wavelength in nanometres written in the description) and never guessed from position.
When the file does not say, the roles are unknown and nothing is measured from it: a
greenness worked out from the wrong two bands is a wrong number that looks right.

The health indices the extra bands allow are here too, one formula each:
NDVI = (NIR - Red) / (NIR + Red), NDRE = (NIR - RedEdge) / (NIR + RedEdge).

A thermal band is different in kind: it does not record sunlight thrown back but the heat the surface itself
gives off (8 to 14 micrometres), which a radiometric camera turns into a temperature. Cameras store it in
different scales (degrees, kelvin, tenths or hundredths of either), and the values alone do not say which, so a
temperature is said in degrees only when the file states its unit (`thermal_unit`); otherwise only which field is
warmer. A thermal picture saved as 8-bit colours holds no temperature and is not measured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

COLOUR = "colour"
MULTISPECTRAL = "multispectral"
UNKNOWN = "unknown"

ROLES = ("red", "green", "blue", "red_edge", "nir", "thermal")

# Centre wavelengths (nm) the cameras in use put their bands at; gaps between ranges stay unnamed.
_WAVELENGTHS = (((430, 510), "blue"), ((520, 590), "green"), ((620, 690), "red"),
                ((700, 760), "red_edge"), ((770, 900), "nir"))
_THERMAL_NM = (8000, 14000)
_LONG_NM = re.compile(r"(?<![\d.])(\d{4,5})(?:\.\d+)?\s*nm")  # 11000 nm
_UM = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d+)?)\s*(?:um|µm|μm|micron)")  # 11 um, 10.5 µm
_NM = re.compile(r"(?<![\d.])(\d{3})(?:\.\d+)?\s*nm")  # 842 nm, never the 550 inside 1550 nm
# Whole-description names, after lower-casing and dropping everything but letters and digits
# ("B1" or "NIR2" is no name we know, so it names nothing).
_NAMES = {
    "nir": "nir", "nearinfrared": "nir", "nearir": "nir", "infrared": "nir",
    "rededge": "red_edge", "re": "red_edge",
    "red": "red", "r": "red", "green": "green", "g": "green", "blue": "blue", "b": "blue",
    "thermal": "thermal", "lwir": "thermal", "tir": "thermal", "thermalinfrared": "thermal",
    "longwaveinfrared": "thermal", "temperature": "thermal",
}


class BandsUnknown(ValueError):
    """The file does not say which band is which, so nothing may be measured from it."""


@dataclass(frozen=True)
class Bands:
    """1-based band numbers by role; None where the photo has no such band or does not say."""
    kind: str = COLOUR
    red: Optional[int] = 1
    green: Optional[int] = 2
    blue: Optional[int] = 3
    red_edge: Optional[int] = None
    nir: Optional[int] = None
    thermal: Optional[int] = None
    eight_bit: bool = True  # the bands hold 0 to 255, as a picture does

    @property
    def picture(self) -> bool:
        """Whether a true-colour picture can be read: red, green and blue known, as 8-bit values."""
        return self.eight_bit and None not in (self.red, self.green, self.blue)

    @property
    def rgb(self) -> list[int]:
        """Red, green, blue band numbers, for a picture of the ground."""
        if not self.picture:
            raise BandsUnknown("this photo has no red, green and blue bands a picture can be made from")
        return [self.red, self.green, self.blue]

    @property
    def red_green(self) -> list[int]:
        if self.red is None or self.green is None:
            raise BandsUnknown("this photo does not say which bands are red and green")
        return [self.red, self.green]


# Where each band of the cameras in use is centred (nm). When a file describes two bands by wavelengths that are
# the same kind of light (880 nm and 900 nm are both near-infrared), the one nearer the usual centre takes the role.
_CENTRE_NM = {"blue": 475.0, "green": 560.0, "red": 668.0, "red_edge": 717.0, "nir": 842.0}


def _wavelength_nm(description: Optional[str]) -> Optional[float]:
    """The wavelength a band's description gives in nanometres of visible or near-infrared light, if it gives one."""
    found = _NM.search(description.lower()) if description else None
    return float(found.group(1)) if found else None


_PROGRAMS = (("pix4dfields", "Pix4Dfields"), ("pix4dmapper", "Pix4Dmapper"), ("pix4d", "Pix4D"),
             ("odm", "OpenDroneMap"), ("opendronemap", "OpenDroneMap"), ("webodm", "OpenDroneMap"),
             ("agisoft", "Agisoft Metashape"), ("metashape", "Agisoft Metashape"), ("photoscan", "Agisoft Metashape"),
             ("dji terra", "DJI Terra"), ("djiterra", "DJI Terra"), ("drone2map", "ArcGIS Drone2Map"),
             ("dronedeploy", "DroneDeploy"), ("correlator3d", "Correlator3D"))


def program(tags: Any) -> Optional[str]:
    """The program that made a photo, when the file says so (its software tag); None otherwise. Many programs
    (Agisoft Metashape, DJI Terra) write nothing, so a photo without one is not from "no program"."""
    said = str((tags or {}).get("TIFFTAG_SOFTWARE") or "").strip().lower()
    for start, name in _PROGRAMS:
        if said.startswith(start):
            return name
    return None


def _role_of_name(description: Optional[str]) -> Optional[str]:
    if not description:
        return None
    text = description.lower()
    long_nm, um = _LONG_NM.search(text), _UM.search(text)
    if long_nm or um:  # a wavelength beyond what sunlight cameras see
        wavelength_nm = int(long_nm.group(1)) if long_nm else float(um.group(1)) * 1000  # type: ignore[union-attr]
        return "thermal" if _THERMAL_NM[0] <= wavelength_nm <= _THERMAL_NM[1] else None
    nm = _NM.search(text)
    if nm:
        wavelength = int(nm.group(1))
        for (low, high), role in _WAVELENGTHS:
            if low <= wavelength <= high:
                return role
        return None  # a wavelength we have no name for: not guessed from the words around it
    return _NAMES.get(re.sub(r"[^a-z0-9]", "", text))


def _role_of_interp(interp: Any) -> Optional[str]:
    name = re.sub(r"[^a-z]", "", str(getattr(interp, "name", "") or "").lower())
    return {"red": "red", "green": "green", "blue": "blue", "nir": "nir", "rededge": "red_edge"}.get(name)


def roles(descriptions: Any, interps: Any, count: int, dtypes: Any = ()) -> Bands:
    """Bands from what a file says about itself. descriptions, interps and dtypes are per band, in file order.

    A transparency band never gets a role, whatever its description says. A description that contradicts the
    band's own colour interpretation, or two bands described as the same thing, make the file unknown. A
    multispectral photo's roles come from descriptions only: writers often stamp red, green, blue onto the first
    three bands of a stack that is in another order."""
    said: dict[str, int] = {}  # from descriptions
    stamped: dict[str, int] = {}  # from colour interpretation
    said_nm: dict[str, float] = {}  # the wavelength behind a description, where it gave one
    clash = False
    for number in range(1, count + 1):
        interp = interps[number - 1] if number <= len(interps) else None
        if str(getattr(interp, "name", "")).lower() == "alpha":
            continue
        by_name = _role_of_name(descriptions[number - 1] if number <= len(descriptions) else None)
        by_stamp = _role_of_interp(interp)
        if by_name and by_stamp and by_name != by_stamp:
            clash = True
        if by_name:
            nm = _wavelength_nm(descriptions[number - 1])
            if by_name in said and nm is not None and by_name in said_nm and by_name in _CENTRE_NM:
                # two bands of one kind of light, each described by its wavelength: the nearer the usual centre
                if abs(nm - _CENTRE_NM[by_name]) < abs(said_nm[by_name] - _CENTRE_NM[by_name]):
                    said[by_name], said_nm[by_name] = number, nm
                continue
            clash = clash or by_name in said
            said[by_name] = number
            if nm is not None:
                said_nm[by_name] = nm
        if by_stamp and by_stamp not in stamped:
            stamped[by_stamp] = number

    def eight_bit(numbers: list[int]) -> bool:
        return all(str(dtypes[n - 1]) == "uint8" for n in numbers if n <= len(dtypes))

    unknown = Bands(kind=UNKNOWN, red=None, green=None, blue=None)
    if ("nir" in said or "red_edge" in said) and (count >= 5 or "red" in said):
        if clash or "red" not in said:
            return unknown
        found = {role: said.get(role) for role in ROLES}
        picture = [n for role, n in found.items() if n and role != "thermal"]
        return Bands(kind=MULTISPECTRAL, eight_bit=eight_bit(picture), **found)
    if count >= 5 or count < 3:  # too many bands for a colour photo and no extra one described, or too few
        return unknown
    named = {**stamped, **said}
    if not clash and {"red", "green", "blue"} <= named.keys():  # a colour photo that says its own order
        order = [named["red"], named["green"], named["blue"]]
        return Bands(kind=COLOUR, red=order[0], green=order[1], blue=order[2], eight_bit=eight_bit(order),
                     thermal=said.get("thermal"))
    if not eight_bit([1, 2, 3]):  # not an ordinary picture, and it does not say what its bands are
        return unknown
    thermal = said.get("thermal") if not clash and said.get("thermal", 0) > 3 else None
    return Bands(thermal=thermal)  # an ordinary colour photo: red, green, blue, then transparency or a described extra


def of(ds: Any) -> Bands:
    """Bands of an open rasterio dataset."""
    return roles(list(ds.descriptions or ()), list(ds.colorinterp or ()), int(ds.count), list(ds.dtypes or ()))


def looks_like_reflectance(*bands: Any) -> bool:
    """Whether bands hold reflectance (decimal numbers from 0 to about 1) and not raw camera counts. Only then
    does an index mean the same from one photo to the next."""
    for band in bands:
        data = np.ma.asarray(band)
        if data.dtype.kind != "f":
            return False
        values = data.compressed()
        values = values[np.isfinite(values)]
        if values.size == 0 or float(values.min()) < -0.05 or float(np.percentile(values, 99)) > 1.2:
            return False
    return True


def index(high: Any, low: Any) -> Any:
    """(high - low) / (high + low) per pixel as a float array; NaN where the sum is 0 or either band is masked."""
    a = np.ma.filled(np.ma.asarray(high, dtype="float32"), np.nan)
    b = np.ma.filled(np.ma.asarray(low, dtype="float32"), np.nan)
    a, b = np.where(a < 0, np.nan, a), np.where(b < 0, np.nan, b)  # light sent back is never negative
    total = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(total > 0, (a - b) / total, np.nan)


def ndvi(nir: Any, red: Any) -> Any:
    return index(nir, red)


def ndre(nir: Any, red_edge: Any) -> Any:
    return index(nir, red_edge)


CELSIUS, KELVIN = "celsius", "kelvin"
_CELSIUS_WORDS = re.compile(r"celsius|centigrade|deg(?:rees?)?\s*c\b|°\s*c\b")


def thermal_unit(unit: Optional[str], description: Optional[str]) -> Optional[str]:
    """The temperature unit a thermal band states for itself (its unit field or its description), or None. It is
    never read from the values: a stored 300 is 300 kelvin for one camera and 30.0 degrees for another."""
    stated = (unit or "").strip().lower()
    text = f"{stated} {(description or '').lower()}"
    if stated in ("c", "°c", "degc") or _CELSIUS_WORDS.search(text):
        return CELSIUS
    if stated == "k" or "kelvin" in text:
        return KELVIN
    return None


# Where the middle of a temperature band over land can lie in the scales cameras store: degrees Celsius, kelvin,
# tenths and hundredths of a kelvin. Used only to tell that a band could be a temperature at all, never which scale.
_PLAUSIBLE = ((-40.0, 90.0), (230.0, 365.0), (2300.0, 3650.0), (23000.0, 36500.0))


def could_be_temperature(band: Any) -> bool:
    """Whether a band's values could be surface temperatures in some scale, and are not an index (-1 to 1), a
    height map or a count. A reader's name for a layer is believed only when the values do not contradict it."""
    data = np.ma.asarray(band, dtype="float32").compressed()
    data = data[np.isfinite(data) & (data != 0)]
    if data.size == 0:
        return False
    low, middle, high = (float(v) for v in np.percentile(data, [2, 50, 98]))
    if -1.5 <= low and high <= 1.5:  # an index
        return False
    return any(a <= middle <= b and a <= low and high <= b for a, b in _PLAUSIBLE)


def unit_fits(band: Any, unit: str, scale: float = 1.0, offset: float = 0.0, whole_numbers: bool = False) -> bool:
    """Whether a band's values agree with the unit the file states: hardly any of them fall outside what a land
    surface can be. A file that says kelvin over hundredths of a kelvin does not fit, and gets no degrees."""
    raw = np.ma.filled(np.ma.asarray(band, dtype="float32"), np.nan)
    stored = int(np.isfinite(raw[raw != 0] if whole_numbers else raw).sum())
    kept = int(np.isfinite(temperature(band, unit, scale, offset, whole_numbers)).sum())
    return stored > 0 and kept >= 0.95 * stored


def temperature(band: Any, unit: Optional[str], scale: float = 1.0, offset: float = 0.0,
                whole_numbers: bool = False) -> Any:
    """A thermal band as a float array with NaN where empty: degrees Celsius when `unit` is known, else the stored
    values as they are (only their order means anything then). The file's own scale and offset are applied. A zero
    in a band of whole numbers is an empty pixel, not a temperature."""
    data = np.ma.filled(np.ma.asarray(band, dtype="float32"), np.nan)
    if whole_numbers:
        data = np.where(data == 0, np.nan, data)
    data = data * float(scale or 1.0) + float(offset or 0.0)
    if unit == KELVIN:
        data = data - 273.15
    if unit is not None:
        data = np.where((data < -60) | (data > 120), np.nan, data)  # no land surface is outside this
    return np.where(np.isfinite(data), data, np.nan)
