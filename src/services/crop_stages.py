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
"""Crop growth stages from days after planting (one model for the whole app).

Generic five-stage model by fraction of the crop cycle: planting, vegetative,
flowering, grain fill (pod fill for beans), maturity. Used for the expected
NDVI per stage (raster interpretation) and for stage-scoped insurance triggers
(e.g. the maize flowering rainfall minimum).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

# Upper bound of each stage as a fraction of the cycle.
_BOUNDARIES = (0.125, 0.375, 0.625, 0.875, 1.0)


def stage_labels(crop: str) -> list[str]:
    if crop == "beans":
        return ["planting", "vegetative", "flowering", "pod_fill", "maturity"]
    return ["planting", "vegetative", "flowering", "grain_fill", "maturity"]


def stage_from_dap(dap: int, total_dap: int, crop: str) -> str:
    """Days-after-planting → growth-stage label, by fraction of total cycle.

    Beans use 'pod_fill', everything else uses 'grain_fill'. If the date is
    before planting or well past harvest, returns '_any' so the threshold
    lookup falls back to the crop's vegetative range.
    """
    if total_dap <= 0 or dap < 0 or dap > total_dap + 30:
        return "_any"

    pct = dap / total_dap
    labels = stage_labels(crop)
    for i, b in enumerate(_BOUNDARIES):
        if pct <= b:
            return labels[i]
    return labels[-1]


def stage_window(stage: str, crop: str, planting_date: date, total_dap: int) -> tuple[date, date] | None:
    """(first day, last day) of a stage for this planting; None for an unknown stage."""
    labels = stage_labels(crop)
    if stage not in labels or total_dap <= 0:
        return None
    i = labels.index(stage)
    # stage_from_dap gives stage i to days with lower < dap/total <= upper
    first = math.floor(_BOUNDARIES[i - 1] * total_dap) + 1 if i else 0
    last = math.floor(_BOUNDARIES[i] * total_dap)
    return planting_date + timedelta(days=first), planting_date + timedelta(days=last)
