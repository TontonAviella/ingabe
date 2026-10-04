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
"""Risk classes for H3 hexagon layers: one place for labels and colours.

Every H3 risk layer (spatial insight, raster context; saved or sent inline)
labels and colours a 0-100 ``risk_score`` with these classes, so the label a
user reads in a popup matches the colour on the map and the legend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RiskClass:
    label: str
    min_score: float  # inclusive lower bound
    color: str


RISK_CLASSES: tuple[RiskClass, ...] = (
    RiskClass("low", 0.0, "#22c55e"),
    RiskClass("moderate", 40.0, "#facc15"),
    RiskClass("high", 60.0, "#f97316"),
    RiskClass("severe", 80.0, "#dc2626"),
)


def risk_level(score: float) -> str:
    """The class label for a 0-100 risk score."""
    label = RISK_CLASSES[0].label
    for cls in RISK_CLASSES:
        if score >= cls.min_score:
            label = cls.label
    return label


def inline_style_stops() -> list[dict[str, Any]]:
    """Colour stops for layers sent inline over the websocket (`max` is exclusive)."""
    bounds = [cls.min_score for cls in RISK_CLASSES[1:]] + [101.0]
    return [{"max": int(upper), "color": cls.color} for cls, upper in zip(RISK_CLASSES, bounds)]
