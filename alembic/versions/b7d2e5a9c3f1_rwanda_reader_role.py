"""Read-only logins for the internal Rwanda PostGIS connection, one per industry group (audit 2026-10-09, R1-5/R1-13)

Revision ID: b7d2e5a9c3f1
Revises: a4c9e2f7b1d3
Create Date: 2026-10-09

The internal "Rwanda Agriculture (internal)" connection logged in as the app's own user, and its SQL allowlist
was a regex that a comma join slipped past, so Sage's new_layer_from_postgis could read users, projects, private
Brain pages and other partners' connection passwords. This creates two roles (src/database/rwanda_reader.py):
- mundi_rwanda_reader (agriculture projects): boundaries, weather and the farm caches;
- mundi_rwanda_reader_general (other industries): boundaries and weather only;
both read-only with a statement timeout and nothing else granted. Every existing internal connection is renamed
"Rwanda data (internal)" and moved onto its project's industry's role. The app user has CREATEROLE; a CREATEROLE
user may not name SUPERUSER/REPLICATION/BYPASSRLS, which new roles lack by default.
"""

from typing import Sequence, Union

from alembic import op

from src.database.rwanda_reader import READER_ROLES, reader_password, reader_uri, role_for, tables_of_role

revision: str = "b7d2e5a9c3f1"
down_revision: Union[str, None] = "a4c9e2f7b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_NAME = "Rwanda Agriculture (internal)"
_NEW_NAME = "Rwanda data (internal)"


def upgrade() -> None:
    for role in READER_ROLES:
        op.execute(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN "
            f"CREATE ROLE {role} LOGIN; END IF; END $$;"
        )
        op.execute(f"ALTER ROLE {role} WITH LOGIN NOCREATEDB NOCREATEROLE PASSWORD '{reader_password(role)}'")
        op.execute(f"ALTER ROLE {role} SET default_transaction_read_only = on")
        op.execute(f"ALTER ROLE {role} SET statement_timeout = '30s'")
        op.execute(f"DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO {role}', current_database()); END $$;")
        op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        for table in sorted(tables_of_role(role)):
            op.execute(
                f"DO $$ BEGIN IF to_regclass('public.{table}') IS NOT NULL THEN "
                f"GRANT SELECT ON public.{table} TO {role}; END IF; END $$;"
            )
        # Every user's parcels with no owner column: never readable through an internal connection.
        op.execute(f"DO $$ BEGIN IF to_regclass('public.ndvi_parcel_cache') IS NOT NULL THEN "
                   f"REVOKE ALL ON public.ndvi_parcel_cache FROM {role}; END IF; END $$;")
    # Every project's internal connection: neutral name, and its industry's reader (the app writes the same URI).
    for industry in ("agriculture", "power_grid", "telecom"):
        uri = reader_uri(industry).replace("'", "''")
        op.execute(
            f"UPDATE project_postgres_connections c SET connection_uri = '{uri}', connection_name = '{_NEW_NAME}' "
            f"FROM user_mundiai_projects p WHERE p.id = c.project_id AND p.industry = '{industry}' "
            f"AND c.connection_name IN ('{_OLD_NAME}', '{_NEW_NAME}')"
        )
    assert role_for("agriculture") != role_for("power_grid")


def downgrade() -> None:
    # The previous code rewrites the internal connections to the app user on the next message.
    op.execute(f"UPDATE project_postgres_connections SET connection_name = '{_OLD_NAME}' "
               f"WHERE connection_name = '{_NEW_NAME}'")
    for role in READER_ROLES:
        for table in sorted(tables_of_role(role)):
            op.execute(
                f"DO $$ BEGIN IF to_regclass('public.{table}') IS NOT NULL THEN "
                f"REVOKE ALL ON public.{table} FROM {role}; END IF; END $$;"
            )
        op.execute(f"REVOKE USAGE ON SCHEMA public FROM {role}")
        op.execute(f"DO $$ BEGIN EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {role}', current_database()); END $$;")
        op.execute(f"DROP ROLE IF EXISTS {role}")
