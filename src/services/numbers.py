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
"""Numbers from the database that may be missing.

`round(x, 4) if x else None` turns a real 0 into "missing": an NDVI of
0.0, a z-score of 0, or a VCI of 0 (the most extreme drought) disappeared
from results. Only None means missing.
"""

from __future__ import annotations

from typing import Any, Optional


def round_or_none(value: Any, digits: Optional[int] = None) -> Optional[float]:
    """`round(float(value), digits)`, or None only when value is None."""
    if value is None:
        return None
    return round(float(value), digits) if digits is not None else float(value)
