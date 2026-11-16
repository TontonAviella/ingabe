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
            clash = clash or by_name in said
            said[by_name] = number
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
