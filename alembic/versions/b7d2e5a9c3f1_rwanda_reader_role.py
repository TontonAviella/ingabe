"""A read-only login for the internal Rwanda PostGIS connection (audit 2026-10-09, finding R1-5)

Revision ID: b7d2e5a9c3f1
Revises: a4c9e2f7b1d3
Create Date: 2026-10-09

The internal "Rwanda Agriculture (internal)" connection logged in as the app's own user, and its SQL allowlist
was a regex that a comma join slipped past, so Sage's new_layer_from_postgis could read users, projects, private
Brain pages and other partners' connection passwords. This creates mundi_rwanda_reader, which can SELECT only the
approved Rwanda boundary and cache relations (src/database/rwanda_reader.py), always runs read-only and with a
statement timeout, and moves every existing internal connection onto it. The app user has CREATEROLE.
"""

from typing import Sequence, Union

from alembic import op

from src.database.rwanda_reader import INTERNAL_RWANDA_ALLOWED_TABLES, READER_ROLE, reader_password, reader_uri

revision: str = "b7d2e5a9c3f1"
down_revision: Union[str, None] = "a4c9e2f7b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INTERNAL_NAME = "Rwanda Agriculture (internal)"


def upgrade() -> None:
    password = reader_password()  # hex, safe to inline
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{READER_ROLE}') THEN
                CREATE ROLE {READER_ROLE} LOGIN;
            END IF;
        END $$;
        """
    )
    # New roles are NOSUPERUSER NOREPLICATION NOBYPASSRLS by default (and a CREATEROLE user may not even name them).
    op.execute(f"ALTER ROLE {READER_ROLE} WITH LOGIN NOCREATEDB NOCREATEROLE PASSWORD '{password}'")
    op.execute(f"ALTER ROLE {READER_ROLE} SET default_transaction_read_only = on")
    op.execute(f"ALTER ROLE {READER_ROLE} SET statement_timeout = '30s'")
    op.execute(f"DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO {READER_ROLE}', current_database()); END $$;")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {READER_ROLE}")
    for table in sorted(INTERNAL_RWANDA_ALLOWED_TABLES):
        op.execute(
            f"DO $$ BEGIN IF to_regclass('public.{table}') IS NOT NULL THEN "
            f"GRANT SELECT ON public.{table} TO {READER_ROLE}; END IF; END $$;"
        )
    # Every project's internal connection logs in as the reader from now on (the app writes the same URI).
    uri = reader_uri().replace("'", "''")
    op.execute(f"UPDATE project_postgres_connections SET connection_uri = '{uri}' WHERE connection_name = '{_INTERNAL_NAME}'")


def downgrade() -> None:
    # The previous code rewrites the internal connections to the app user on the next message.
    for table in sorted(INTERNAL_RWANDA_ALLOWED_TABLES):
        op.execute(
            f"DO $$ BEGIN IF to_regclass('public.{table}') IS NOT NULL THEN "
            f"REVOKE ALL ON public.{table} FROM {READER_ROLE}; END IF; END $$;"
        )
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {READER_ROLE}")
    op.execute(f"DO $$ BEGIN EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {READER_ROLE}', current_database()); END $$;")
    op.execute(f"DROP ROLE IF EXISTS {READER_ROLE}")
