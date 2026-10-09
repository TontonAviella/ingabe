"""The industry a user works in: Ingabe serves agriculture, power grids and telecom towers, and asks once at sign-in.

The choice decides which capabilities the app leads with; it is the user's own setting (users.industry) and can be
changed at any time.
"""

from __future__ import annotations

from typing import Any, Optional

INDUSTRIES: dict[str, dict[str, str]] = {
    "agriculture": {"label": "Agriculture", "note": "Farms, plots and crops"},
    "power_grid": {"label": "Power Grid", "note": "Transmission and distribution lines"},
    "telecom": {"label": "Telecom Towers", "note": "Masts, antennas and sites"},
}


async def industry_of(conn: Any, user_id: Optional[str]) -> Optional[str]:
    """The user's industry, or None when they have not chosen one (or have no account row)."""
    if not user_id:
        return None
    value = await conn.fetchval("SELECT industry FROM users WHERE internal_uuid = $1", user_id)
    return value if value in INDUSTRIES else None


async def save_industry(conn: Any, user_id: str, industry: str) -> bool:
    """Save the user's industry. ValueError for an unknown one; False if the user has no account row."""
    if industry not in INDUSTRIES:
        raise ValueError(f"industry must be one of {', '.join(INDUSTRIES)}")
    status = await conn.execute("UPDATE users SET industry = $2 WHERE internal_uuid = $1", user_id, industry)
    return status.endswith(" 1")
