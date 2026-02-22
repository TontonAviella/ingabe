"""Database connection URL for Alembic migrations.

Runtime database access uses the asyncpg pool in ``src.structures``
(via ``get_async_db_connection``).  This module exists solely to provide
the SQLAlchemy-style ``DATABASE_URL`` that Alembic's ``env.py`` imports.
"""

import os
from src.database.pool import _build_postgres_url

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    _build_postgres_url().replace("postgresql://", "postgresql+asyncpg://"),
)
