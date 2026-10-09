"""The read-only database login behind every project's internal "Rwanda Agriculture (internal)" PostGIS connection.

Sage's new_layer_from_postgis (and the layers it creates) run SQL written by the model against the app's own
database. That SQL must only ever read the Rwanda boundary and cache tables below, so the connection logs in as
mundi_rwanda_reader: a role that can SELECT exactly these relations, is read-only, has a statement timeout, and
can read nothing else (users, projects, Brain, other connections' credentials). The allowlist regex in
message_routes stays as a friendly early error; this role is the boundary.

The role's password is derived from POSTGRES_PASSWORD, so no new secret has to be configured: the migration that
creates the role and the app that builds the connection URI compute the same value.
"""

from __future__ import annotations

import hashlib
import os
from urllib.parse import quote

READER_ROLE = "mundi_rwanda_reader"

INTERNAL_RWANDA_ALLOWED_TABLES = frozenset(
    {
        "rwanda_province_boundaries",
        "rwanda_district_boundaries",
        "rwanda_sector_boundaries",
        "rwanda_cell_boundaries",
        "rwanda_village_boundaries",
        "ndvi_cell_cache",
        "ndvi_field_cache",
        "ndvi_parcel_cache",
        "agri_indices_cache",
        "anomaly_alerts_cache",
        "crop_classification_cache",
        "drought_cache",
        "emissions_annual_cache",
        "phenology_cache",
        "weather_daily_cache",
        "yield_risk_cache",
    }
)


def reader_password() -> str:
    """The reader role's password: a one-way derivation of the app's own database password."""
    base = os.environ.get("POSTGRES_PASSWORD", "changeme")
    return hashlib.sha256(f"{base}:{READER_ROLE}".encode()).hexdigest()


def reader_uri() -> str:
    """Connection URI for the internal Rwanda connection, logging in as the reader role."""
    host = os.environ.get("POSTGRES_HOST", "postgresdb")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "mundidb")
    return (f"postgresql://{READER_ROLE}:{quote(reader_password(), safe='')}"
            f"@{host}:{port}/{quote(db, safe='')}?sslmode=disable")
