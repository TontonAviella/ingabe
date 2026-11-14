"""The area of a shape on the ground. One home for it: plots, cards and tapped field outlines all quote hectares."""

from __future__ import annotations

from typing import Any

from pyproj import Geod

_GEOD = Geod(ellps="WGS84")


def area_ha(geom_wgs84: Any) -> float:
    """Hectares inside a shape given in longitude and latitude, measured on the ellipsoid (not in map units, which
    stretch away from the equator)."""
    return abs(_GEOD.geometry_area_perimeter(geom_wgs84)[0]) / 10_000
