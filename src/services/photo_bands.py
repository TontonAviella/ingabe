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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

COLOUR = "colour"
MULTISPECTRAL = "multispectral"
UNKNOWN = "unknown"

ROLES = ("red", "green", "blue", "red_edge", "nir")

# Centre wavelengths (nm) the cameras in use put their bands at; gaps between ranges stay unnamed.
_WAVELENGTHS = (((430, 510), "blue"), ((520, 590), "green"), ((620, 690), "red"),
                ((700, 760), "red_edge"), ((770, 900), "nir"))
_NM = re.compile(r"(\d{3})(?:\.\d+)?\s*nm")
# Whole-description names, after lower-casing and dropping everything but letters.
_NAMES = {
    "nir": "nir", "nearinfrared": "nir", "nearir": "nir", "infrared": "nir",
    "rededge": "red_edge", "re": "red_edge",
    "red": "red", "r": "red", "green": "green", "g": "green", "blue": "blue", "b": "blue",
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

    @property
    def rgb(self) -> list[int]:
        """Red, green, blue band numbers, for a picture of the ground."""
        if self.red is None or self.green is None or self.blue is None:
            raise BandsUnknown("this photo does not say which bands are red, green and blue")
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
    nm = _NM.search(text)
    if nm:
        wavelength = int(nm.group(1))
        for (low, high), role in _WAVELENGTHS:
            if low <= wavelength <= high:
                return role
        return None  # a wavelength we have no name for: not guessed from the words around it
    return _NAMES.get(re.sub(r"[^a-z]", "", text))


def _role_of_interp(interp: Any) -> Optional[str]:
    name = re.sub(r"[^a-z]", "", str(getattr(interp, "name", "") or "").lower())
    return {"red": "red", "green": "green", "blue": "blue", "nir": "nir", "rededge": "red_edge"}.get(name)


def roles(descriptions: Any, interps: Any, count: int) -> Bands:
    """Bands from what a file says about itself. descriptions and interps are per band, in file order."""
    named: dict[str, int] = {}
    clash = False
    for number in range(1, count + 1):
        description = descriptions[number - 1] if number <= len(descriptions) else None
        interp = interps[number - 1] if number <= len(interps) else None
        role = _role_of_name(description) or _role_of_interp(interp)
        if role is None:
            continue
        if role in named:
            clash = True  # two bands claim one role: the file contradicts itself
        named[role] = number
    extra = "nir" in named or "red_edge" in named
    if clash:
        return Bands(kind=UNKNOWN, red=None, green=None, blue=None)
    if extra:
        return Bands(kind=MULTISPECTRAL, **{role: named.get(role) for role in ROLES})
    if count >= 5:  # too many bands for a colour photo, and none named as an extra one
        return Bands(kind=UNKNOWN, red=None, green=None, blue=None)
    if {"red", "green", "blue"} <= named.keys():  # a colour photo that names its own order
        return Bands(kind=COLOUR, red=named["red"], green=named["green"], blue=named["blue"])
    if count >= 3:
        return Bands()  # an ordinary colour photo: red, green, blue, then transparency
    return Bands(kind=UNKNOWN, red=None, green=None, blue=None)


def of(ds: Any) -> Bands:
    """Bands of an open rasterio dataset."""
    return roles(list(ds.descriptions or ()), list(ds.colorinterp or ()), int(ds.count))


def index(high: Any, low: Any) -> Any:
    """(high - low) / (high + low) per pixel as a float array; NaN where the sum is 0 or either band is masked."""
    a = np.ma.filled(np.ma.asarray(high, dtype="float32"), np.nan)
    b = np.ma.filled(np.ma.asarray(low, dtype="float32"), np.nan)
    total = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(total > 0, (a - b) / total, np.nan)


def ndvi(nir: Any, red: Any) -> Any:
    return index(nir, red)


def ndre(nir: Any, red_edge: Any) -> Any:
    return index(nir, red_edge)
