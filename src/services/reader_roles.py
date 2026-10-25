"""Who a person is when they read Ingabe's questions and answers: their role, per industry (Roger, 2026-10-25).

Each industry has its own roles. Agriculture: farmer, cooperative or farm manager, agronomist, insurer, scientist,
government or district, buyer or processor (the readers of the drone cards brief). Telecom Towers and Power Grid:
owner, operations manager, field technician. A role decides which questions a person is asked and how they are
worded (src/services/drone_cards.py, src/services/tower_cards.py); the answers are written in one of a few voices,
and each role reads in one of them (VOICE): a cooperative manager reads the farmer's plain words, a government
officer the insurer's evidence-first words, an operations manager the owner's.

A person's role is saved on their account (users.report_audience), else their company's default
(organizations.metadata.default_audience), else the industry's default. A role saved for another industry (Ingabe's
own people switch industries) falls back to the default of the industry being read.
"""

from __future__ import annotations

from typing import Any, Optional

AGRICULTURE = {
    "farmer": "Farmer",
    "cooperative": "Cooperative / farm manager",
    "agronomist": "Agronomist",
    "insurance": "Insurer",
    "scientist": "Scientist",
    "government": "Government / district",
    "buyer": "Buyer / processor",
}
ASSETS = {
    "owner": "Owner",
    "manager": "Operations manager",
    "technician": "Field technician",
}
ROLES: dict[str, dict[str, str]] = {"agriculture": AGRICULTURE, "telecom": ASSETS, "power_grid": ASSETS}
DEFAULT_ROLE = {"agriculture": "agronomist", "telecom": "owner", "power_grid": "owner"}
ALL_ROLES = frozenset(AGRICULTURE) | frozenset(ASSETS)

# The voice each role's answers are written in (the answer texts exist for these voices only).
VOICE = {
    "farmer": "farmer", "cooperative": "farmer", "buyer": "farmer",
    "agronomist": "agronomist", "scientist": "scientist",
    "insurance": "insurance", "government": "insurance",
    "owner": "owner", "manager": "owner", "technician": "technician",
}

# Other names people (or the model) use for a role.
ALIASES = {
    "underwriter": "insurance", "insurer": "insurance", "insurance_officer": "insurance", "claims": "insurance",
    "extension_officer": "agronomist", "extension": "agronomist", "researcher": "scientist",
    "farm_manager": "cooperative", "manager_farm": "cooperative", "coop": "cooperative",
    "district": "government", "rab": "government", "processor": "buyer",
    "asset_manager": "manager", "operations_manager": "manager", "field_technician": "technician",
}


def known(role: Optional[str]) -> Optional[str]:
    """The role for a name (aliases accepted); None when it is no role."""
    key = (role or "").strip().lower().replace(" ", "_").replace("/", "_")
    key = ALIASES.get(key, key)
    return key if key in ALL_ROLES else None


def for_industry(industry: Optional[str], role: Optional[str]) -> str:
    """The role a person reads with in an industry: theirs when it is one of the industry's, else its default."""
    roles = ROLES.get(industry or "agriculture", AGRICULTURE)
    found = known(role)
    return found if found in roles else DEFAULT_ROLE.get(industry or "agriculture", "agronomist")


def voice(role: str) -> str:
    """The voice the answers are written in for a role."""
    return VOICE[role]


def options(industry: Optional[str]) -> list[dict[str, str]]:
    """The roles a reader can pick in an industry, for the screen."""
    return [{"id": key, "label": label} for key, label in ROLES.get(industry or "agriculture", AGRICULTURE).items()]


def label(role: str) -> str:
    return AGRICULTURE.get(role) or ASSETS[role]


async def saved_role(conn: Any, user_id: Optional[str], partner_id: Optional[str]) -> tuple[Optional[str], str]:
    """(role, source) from saved settings: the user's own ("user"), else their company's default ("partner"), else
    (None, "default")."""
    if user_id:
        mine = known(await conn.fetchval("SELECT report_audience FROM users WHERE internal_uuid = $1", user_id))
        if mine:
            return mine, "user"
    if partner_id:
        theirs = known(await conn.fetchval(
            "SELECT metadata->>'default_audience' FROM organizations WHERE id::text = $1", partner_id))
        if theirs:
            return theirs, "partner"
    return None, "default"


async def save_role(conn: Any, user_id: str, role: Optional[str]) -> bool:
    """Save (or clear, with None) the user's role. ValueError for an unknown one; False without an account row."""
    value = None if role is None else known(role)
    if role is not None and value is None:
        raise ValueError(f"the role must be one of {', '.join(sorted(ALL_ROLES))}")
    status = await conn.execute("UPDATE users SET report_audience = $2 WHERE internal_uuid = $1", user_id, value)
    return status.endswith(" 1")


async def role_in(conn: Any, industry: Optional[str], requested: Optional[str], user_id: Optional[str],
                  partner_id: Optional[str]) -> str:
    """The role to read with: one asked for now, else the saved one, each kept only when it is the industry's."""
    if known(requested) in ROLES.get(industry or "agriculture", AGRICULTURE):
        return known(requested)  # type: ignore[return-value]
    saved, _source = await saved_role(conn, user_id, partner_id)
    return for_industry(industry, saved)
