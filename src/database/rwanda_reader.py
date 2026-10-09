"""The read-only database logins behind every project's internal "Rwanda data (internal)" PostGIS connection.

Sage's new_layer_from_postgis (and the layers it creates) run SQL written by the model against the app's own
database. That SQL must only ever read approved Rwanda tables, so the connection logs in as a dedicated role that
can SELECT exactly those relations, is read-only, has a statement timeout, and can read nothing else (users,
projects, Brain, other connections' credentials). The allowlist regex in message_routes stays as a friendly early
error; the roles are the boundary (audit 2026-10-09, R1-5).

Two roles, so industries stay apart in the database too (R1-13):
- mundi_rwanda_reader: agriculture projects; boundaries, weather and the farm caches (district/cell NDVI,
  indices, anomalies, crop classes, drought, phenology, yield risk, emissions).
- mundi_rwanda_reader_general: every other industry; boundaries and weather only.
ndvi_parcel_cache is in neither: it holds every user's uploaded parcels with no owner column.

Passwords are derived from POSTGRES_PASSWORD, so no new secret has to be configured: the migration that creates
the roles and the app that builds the connection URIs compute the same values.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional
from urllib.parse import quote

READER_ROLE = "mundi_rwanda_reader"  # agriculture projects
GENERAL_READER_ROLE = "mundi_rwanda_reader_general"  # power grid, telecom
READER_ROLES = (READER_ROLE, GENERAL_READER_ROLE)

BOUNDARY_TABLES = frozenset(
    {
        "rwanda_province_boundaries",
        "rwanda_district_boundaries",
        "rwanda_sector_boundaries",
        "rwanda_cell_boundaries",
        "rwanda_village_boundaries",
        "weather_daily_cache",
    }
)
AGRICULTURE_TABLES = frozenset(
    {
        "ndvi_cell_cache",
        "ndvi_field_cache",
        "agri_indices_cache",
        "anomaly_alerts_cache",
        "crop_classification_cache",
        "drought_cache",
        "emissions_annual_cache",
        "phenology_cache",
        "yield_risk_cache",
    }
)
# Everything any internal connection may read (the agriculture role's grants).
INTERNAL_RWANDA_ALLOWED_TABLES = BOUNDARY_TABLES | AGRICULTURE_TABLES


def role_for(industry: Optional[str]) -> str:
    """Agriculture projects read the farm caches; every other (or unknown) industry reads boundaries only."""
    return READER_ROLE if industry == "agriculture" else GENERAL_READER_ROLE


def tables_for(industry: Optional[str]) -> frozenset[str]:
    """The tables a project of this industry may read through its internal connection."""
    return INTERNAL_RWANDA_ALLOWED_TABLES if role_for(industry) == READER_ROLE else BOUNDARY_TABLES


def tables_of_role(role: str) -> frozenset[str]:
    return INTERNAL_RWANDA_ALLOWED_TABLES if role == READER_ROLE else BOUNDARY_TABLES


def reader_password(role: str = READER_ROLE) -> str:
    """A reader role's password: a one-way derivation of the app's own database password."""
    base = os.environ.get("POSTGRES_PASSWORD", "changeme")
    return hashlib.sha256(f"{base}:{role}".encode()).hexdigest()


def reader_uri(industry: Optional[str] = "agriculture") -> str:
    """Connection URI for a project's internal Rwanda connection, logging in as its industry's reader role."""
    role = role_for(industry)
    host = os.environ.get("POSTGRES_HOST", "postgresdb")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "mundidb")
    return (f"postgresql://{role}:{quote(reader_password(role), safe='')}"
            f"@{host}:{port}/{quote(db, safe='')}?sslmode=disable")
