# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""NDVI classes: one scale for map colours, legends and Sage's wording.

Sage's NDVI tool notes and the dashboard map used different cut-offs, so a
district Sage called "cropland" could be coloured "sparse". Both now read
this scale. Labels describe what the index usually means at district or
field scale; they are not crop identification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class NdviClass:
    key: str
    label: str
    min_ndvi: float  # inclusive lower bound
    color: str


NDVI_CLASSES: tuple[NdviClass, ...] = (
    NdviClass("bare", "Bare soil or cloud", -1.0, "#d73027"),
    NdviClass("sparse", "Sparse vegetation", 0.1, "#fc8d59"),
    NdviClass("moderate", "Cropland or moderate vegetation", 0.3, "#a6d96a"),
    NdviClass("dense", "Dense vegetation", 0.6, "#1a9850"),
)


def ndvi_class(value: Optional[float]) -> Optional[NdviClass]:
    """The class for an NDVI value, or None when there is no value."""
    if value is None:
        return None
    found = NDVI_CLASSES[0]
    for cls in NDVI_CLASSES:
        if value >= cls.min_ndvi:
            found = cls
    return found


def _range(i: int) -> str:
    cls = NDVI_CLASSES[i]
    if i == 0:
        return f"below {NDVI_CLASSES[1].min_ndvi:g}"
    if i == len(NDVI_CLASSES) - 1:
        return f"{cls.min_ndvi:g} and above"
    return f"{cls.min_ndvi:g}-{NDVI_CLASSES[i + 1].min_ndvi:g}"


def legend() -> dict[str, Any]:
    return {
        "title": "Vegetation (NDVI)",
        "items": [{"key": c.key, "label": c.label, "range": _range(i), "color": c.color}
                  for i, c in enumerate(NDVI_CLASSES)],
    }


def scale_text() -> str:
    """The scale as one sentence, for tool results Sage reads."""
    parts = [f"{_range(i)} = {c.label.lower()}" for i, c in reversed(list(enumerate(NDVI_CLASSES)))]
    return "NDVI values: " + ", ".join(parts) + "."
