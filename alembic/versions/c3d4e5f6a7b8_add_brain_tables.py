"""add brain tables: 9 tables for knowledge brain (ported from gbrain)

Creates brain_pages, brain_content_chunks, brain_links, brain_tags,
brain_raw_data, brain_timeline_entries, brain_page_versions,
brain_ingest_log, brain_files, and brain_pending_hooks (retry queue).

Extensions: pgvector (vector type + HNSW), pg_trgm (trigram search).
RLS: same CASE WHEN pattern as user_mundiai_projects.
Triggers: tsvector search across pages + timeline entries.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-13 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# RLS policy template — same CASE WHEN as existing projects/conversations.
# When app.user_id is empty (migrations, background workers), all rows visible.
# When set, filter by owner_uuid OR viewer/editor membership.
_RLS_POLICY_OWNER = """
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

# RLS for child tables that join to brain_pages via page_id
_RLS_POLICY_CHILD = """
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

# RLS for brain_links (uses from_page_id)
_RLS_POLICY_LINKS = """
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

# RLS for brain_files (uses page_slug -> brain_pages.slug)
_RLS_POLICY_FILES = """
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

_TABLES_ALL = [
    "brain_pages",
    "brain_content_chunks",
    "brain_links",
    "brain_tags",
    "brain_raw_data",
    "brain_timeline_entries",
    "brain_page_versions",
    "brain_ingest_log",
    "brain_files",
    "brain_pending_hooks",
]


def upgrade() -> None:
    # ── Extensions ──────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ── brain_pages ─────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_pages (
            id              SERIAL PRIMARY KEY,
            slug            TEXT        NOT NULL UNIQUE,
            type            TEXT        NOT NULL,
            title           TEXT        NOT NULL,
            compiled_truth  TEXT        NOT NULL DEFAULT '',
            timeline        TEXT        NOT NULL DEFAULT '',
            frontmatter     JSONB       NOT NULL DEFAULT '{}',
            content_hash    TEXT,
            owner_uuid      UUID        NOT NULL,
            viewer_uuids    UUID[]      NOT NULL DEFAULT '{}',
            editor_uuids    UUID[]      NOT NULL DEFAULT '{}',
            geom            GEOMETRY(Geometry, 4326),
            search_vector   tsvector,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_pages_type ON brain_pages(type)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_pages_frontmatter "
        "ON brain_pages USING GIN(frontmatter)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_pages_trgm "
        "ON brain_pages USING GIN(title gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_pages_owner "
        "ON brain_pages(owner_uuid)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_pages_geom "
        "ON brain_pages USING GIST(geom)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_pages_search "
        "ON brain_pages USING GIN(search_vector)"
    )

    # ── brain_content_chunks ────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_content_chunks (
            id              SERIAL PRIMARY KEY,
            page_id         INTEGER     NOT NULL REFERENCES brain_pages(id) ON DELETE CASCADE,
            chunk_index     INTEGER     NOT NULL,
            chunk_text      TEXT        NOT NULL,
            chunk_source    TEXT        NOT NULL DEFAULT 'compiled_truth',
            embedding       vector(1536),
            model           TEXT        NOT NULL DEFAULT 'text-embedding-3-large',
            token_count     INTEGER,
            embedded_at     TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_brain_chunks_page_index "
        "ON brain_content_chunks(page_id, chunk_index)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_chunks_page "
        "ON brain_content_chunks(page_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_chunks_embedding "
        "ON brain_content_chunks USING hnsw (embedding vector_cosine_ops)"
    )

    # ── brain_links ─────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_links (
            id              SERIAL PRIMARY KEY,
            from_page_id    INTEGER     NOT NULL REFERENCES brain_pages(id) ON DELETE CASCADE,
            to_page_id      INTEGER     NOT NULL REFERENCES brain_pages(id) ON DELETE CASCADE,
            link_type       TEXT        NOT NULL DEFAULT '',
            context         TEXT        NOT NULL DEFAULT '',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(from_page_id, to_page_id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_links_from "
        "ON brain_links(from_page_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_links_to "
        "ON brain_links(to_page_id)"
    )

    # ── brain_tags ──────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_tags (
            id      SERIAL PRIMARY KEY,
            page_id INTEGER NOT NULL REFERENCES brain_pages(id) ON DELETE CASCADE,
            tag     TEXT    NOT NULL,
            UNIQUE(page_id, tag)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_tags_tag ON brain_tags(tag)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_tags_page_id ON brain_tags(page_id)"
    )

    # ── brain_raw_data ──────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_raw_data (
            id          SERIAL PRIMARY KEY,
            page_id     INTEGER     NOT NULL REFERENCES brain_pages(id) ON DELETE CASCADE,
            source      TEXT        NOT NULL,
            data        JSONB       NOT NULL,
            fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(page_id, source)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_raw_data_page "
        "ON brain_raw_data(page_id)"
    )

    # ── brain_timeline_entries ──────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_timeline_entries (
            id          SERIAL PRIMARY KEY,
            page_id     INTEGER     NOT NULL REFERENCES brain_pages(id) ON DELETE CASCADE,
            date        DATE        NOT NULL,
            source      TEXT        NOT NULL DEFAULT '',
            summary     TEXT        NOT NULL,
            detail      TEXT        NOT NULL DEFAULT '',
            owner_uuid  UUID,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_timeline_page "
        "ON brain_timeline_entries(page_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_timeline_date "
        "ON brain_timeline_entries(date)"
    )

    # ── brain_page_versions ─────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_page_versions (
            id              SERIAL PRIMARY KEY,
            page_id         INTEGER     NOT NULL REFERENCES brain_pages(id) ON DELETE CASCADE,
            compiled_truth  TEXT        NOT NULL,
            frontmatter     JSONB       NOT NULL DEFAULT '{}',
            snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_versions_page "
        "ON brain_page_versions(page_id)"
    )

    # ── brain_ingest_log ────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_ingest_log (
            id              SERIAL PRIMARY KEY,
            source_type     TEXT        NOT NULL,
            source_ref      TEXT        NOT NULL,
            pages_updated   JSONB       NOT NULL DEFAULT '[]',
            summary         TEXT        NOT NULL DEFAULT '',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # ── brain_files ─────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_files (
            id              SERIAL PRIMARY KEY,
            page_slug       TEXT        REFERENCES brain_pages(slug)
                                        ON DELETE SET NULL ON UPDATE CASCADE,
            filename        TEXT        NOT NULL,
            storage_path    TEXT        NOT NULL,
            mime_type       TEXT,
            size_bytes      BIGINT,
            content_hash    TEXT        NOT NULL,
            metadata        JSONB       NOT NULL DEFAULT '{}',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(storage_path)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_files_page "
        "ON brain_files(page_slug)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_files_hash "
        "ON brain_files(content_hash)"
    )

    # ── brain_pending_hooks (retry queue) ───────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_pending_hooks (
            id              SERIAL PRIMARY KEY,
            hook_type       TEXT        NOT NULL,
            payload         JSONB       NOT NULL,
            attempts        INTEGER     NOT NULL DEFAULT 0,
            max_attempts    INTEGER     NOT NULL DEFAULT 5,
            last_error      TEXT,
            next_retry_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at    TIMESTAMPTZ
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_pending_hooks_retry "
        "ON brain_pending_hooks(next_retry_at) "
        "WHERE completed_at IS NULL AND attempts < max_attempts"
    )

    # ── Triggers: tsvector search across pages + timeline ───────
    op.execute("""
        CREATE OR REPLACE FUNCTION update_brain_page_search_vector() RETURNS trigger AS $$
        DECLARE
            timeline_text TEXT;
        BEGIN
            SELECT coalesce(string_agg(summary || ' ' || detail, ' '), '')
            INTO timeline_text
            FROM brain_timeline_entries
            WHERE page_id = NEW.id;

            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.compiled_truth, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(NEW.timeline, '')), 'C') ||
                setweight(to_tsvector('english', coalesce(timeline_text, '')), 'C');

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_brain_pages_search_vector
        BEFORE INSERT OR UPDATE ON brain_pages
        FOR EACH ROW
        EXECUTE FUNCTION update_brain_page_search_vector()
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION update_brain_page_search_from_timeline() RETURNS trigger AS $$
        BEGIN
            UPDATE brain_pages SET updated_at = now()
            WHERE id = coalesce(NEW.page_id, OLD.page_id);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_brain_timeline_search_vector
        AFTER INSERT OR UPDATE OR DELETE ON brain_timeline_entries
        FOR EACH ROW
        EXECUTE FUNCTION update_brain_page_search_from_timeline()
    """)

    # ── RLS ─────────────────────────────────────────────────────
    for table in _TABLES_ALL:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # brain_pages: direct owner/viewer/editor check
    op.execute(_RLS_POLICY_OWNER.format(table="brain_pages"))

    # Child tables with page_id FK
    for table in [
        "brain_content_chunks",
        "brain_tags",
        "brain_raw_data",
        "brain_timeline_entries",
        "brain_page_versions",
    ]:
        op.execute(_RLS_POLICY_CHILD.format(table=table))

    # brain_links: check from_page_id ownership
    op.execute(_RLS_POLICY_LINKS)

    # brain_files: check page_slug ownership
    op.execute(_RLS_POLICY_FILES)

    # brain_ingest_log: visible to all authenticated users (operational log)
    op.execute("""
        CREATE POLICY tenant_isolation_brain_ingest_log ON brain_ingest_log
        USING (true)
    """)

    # brain_pending_hooks: visible to all (system table, no user data)
    op.execute("""
        CREATE POLICY tenant_isolation_brain_pending_hooks ON brain_pending_hooks
        USING (true)
    """)


def downgrade() -> None:
    # Drop triggers
    op.execute("DROP TRIGGER IF EXISTS trg_brain_timeline_search_vector ON brain_timeline_entries")
    op.execute("DROP TRIGGER IF EXISTS trg_brain_pages_search_vector ON brain_pages")
    op.execute("DROP FUNCTION IF EXISTS update_brain_page_search_from_timeline()")
    op.execute("DROP FUNCTION IF EXISTS update_brain_page_search_vector()")

    # Drop RLS policies and tables in reverse dependency order
    for table in reversed(_TABLES_ALL):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
