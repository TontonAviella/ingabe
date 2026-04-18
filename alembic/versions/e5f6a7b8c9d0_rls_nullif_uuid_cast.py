"""fix RLS uuid cast: NULLIF guard for subquery-pullup edge case

The existing CASE WHEN short-circuit works for simple policies, but when
a policy's ELSE branch contains a correlated subquery (e.g. child
tables joining to brain_pages), PostgreSQL's optimizer may pull the
subquery up and evaluate the ''::uuid cast even when app.user_id is
empty. That regresses to 'invalid input syntax for type uuid: ""'.

Fix: replace `current_setting('app.user_id', true)::uuid` with
`NULLIF(current_setting('app.user_id', true), '')::uuid`. When user_id
is '', NULLIF returns NULL; NULL::uuid is NULL (legal); NULL = ANY(arr)
is NULL (falsy in WHERE). The cast is now safe regardless of plan
shape.

Scope: brain_* tables only. The T1 partner-isolation gate fails
without this fix because the search-vector trigger on brain_pages
SELECTs from brain_timeline_entries under RLS, and that SELECT hits
the subquery pullup.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-17 20:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OWNER_NULLIF = """
    CREATE POLICY tenant_isolation_{table} ON {table}
    USING (
        CASE
            WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
            ELSE
                owner_uuid::text = current_setting('app.user_id', true)
                OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(viewer_uuids)
                OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(editor_uuids)
        END
    )
"""

_CHILD_NULLIF = """
    CREATE POLICY tenant_isolation_{table} ON {table}
    USING (
        CASE
            WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
            ELSE
                page_id IN (
                    SELECT id FROM brain_pages
                    WHERE owner_uuid::text = current_setting('app.user_id', true)
                       OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(viewer_uuids)
                       OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(editor_uuids)
                )
        END
    )
"""

_LINKS_NULLIF = """
    CREATE POLICY tenant_isolation_brain_links ON brain_links
    USING (
        CASE
            WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
            ELSE
                from_page_id IN (
                    SELECT id FROM brain_pages
                    WHERE owner_uuid::text = current_setting('app.user_id', true)
                       OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(viewer_uuids)
                       OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(editor_uuids)
                )
        END
    )
"""

_FILES_NULLIF = """
    CREATE POLICY tenant_isolation_brain_files ON brain_files
    USING (
        CASE
            WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
            ELSE
                page_slug IN (
                    SELECT slug FROM brain_pages
                    WHERE owner_uuid::text = current_setting('app.user_id', true)
                       OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(viewer_uuids)
                       OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(editor_uuids)
                )
        END
    )
"""

_CHILD_TABLES = [
    "brain_content_chunks",
    "brain_tags",
    "brain_raw_data",
    "brain_timeline_entries",
    "brain_page_versions",
]


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_pages ON brain_pages")
    op.execute(_OWNER_NULLIF.format(table="brain_pages"))

    for table in _CHILD_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(_CHILD_NULLIF.format(table=table))

    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_links ON brain_links")
    op.execute(_LINKS_NULLIF)

    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_files ON brain_files")
    op.execute(_FILES_NULLIF)


def downgrade() -> None:
    _OWNER_ORIG = """
        CREATE POLICY tenant_isolation_{table} ON {table}
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE
                    owner_uuid::text = current_setting('app.user_id', true)
                    OR current_setting('app.user_id', true)::uuid = ANY(viewer_uuids)
                    OR current_setting('app.user_id', true)::uuid = ANY(editor_uuids)
            END
        )
    """
    _CHILD_ORIG = """
        CREATE POLICY tenant_isolation_{table} ON {table}
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE
                    page_id IN (
                        SELECT id FROM brain_pages
                        WHERE owner_uuid::text = current_setting('app.user_id', true)
                           OR current_setting('app.user_id', true)::uuid = ANY(viewer_uuids)
                           OR current_setting('app.user_id', true)::uuid = ANY(editor_uuids)
                    )
            END
        )
    """
    _LINKS_ORIG = """
        CREATE POLICY tenant_isolation_brain_links ON brain_links
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE
                    from_page_id IN (
                        SELECT id FROM brain_pages
                        WHERE owner_uuid::text = current_setting('app.user_id', true)
                           OR current_setting('app.user_id', true)::uuid = ANY(viewer_uuids)
                           OR current_setting('app.user_id', true)::uuid = ANY(editor_uuids)
                    )
            END
        )
    """
    _FILES_ORIG = """
        CREATE POLICY tenant_isolation_brain_files ON brain_files
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE
                    page_slug IN (
                        SELECT slug FROM brain_pages
                        WHERE owner_uuid::text = current_setting('app.user_id', true)
                           OR current_setting('app.user_id', true)::uuid = ANY(viewer_uuids)
                           OR current_setting('app.user_id', true)::uuid = ANY(editor_uuids)
                    )
            END
        )
    """
    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_files ON brain_files")
    op.execute(_FILES_ORIG)
    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_links ON brain_links")
    op.execute(_LINKS_ORIG)
    for table in _CHILD_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(_CHILD_ORIG.format(table=table))
    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_pages ON brain_pages")
    op.execute(_OWNER_ORIG.format(table="brain_pages"))
