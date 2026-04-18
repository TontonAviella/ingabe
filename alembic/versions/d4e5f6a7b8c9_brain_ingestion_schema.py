"""brain ingestion schema: partner isolation + sources + entities + query log

Delta over c3d4e5f6a7b8_add_brain_tables. Adds:
- brain_pages columns: language, license, source_id, fetched_at, access_scope,
  partner_id (nullable UUID — NULL means non-partner/public data)
- brain_tables: structured numeric index (E3)
- brain_sources: fetcher registry + ToS state + tier tag
- brain_entities + brain_entity_refs: cross-source entity reconciler (E7)
- sage_query_log: retrieval feedback loop for quality learning
- partner_isolation_brain_pages RLS policy: extends existing CASE WHEN
  empty->true pattern with access_scope + app.partner_id + app.role=admin

Reuses existing: brain_pages.geom (GIST), brain_pages.content_hash,
brain_page_versions, brain_content_chunks HNSW. Does not duplicate.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-17 19:25:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Extend existing RLS with partner_id + access_scope + admin role.
# Preserves the `empty app.user_id -> true` contract so migrations and
# background workers continue to function.
#
# Semantics:
#   * empty app.user_id (worker/migration context): visible
#   * role=admin: visible (ops/debugging — logged elsewhere)
#   * access_scope=public: visible to any authenticated session
#   * access_scope=partner_internal: visible only if
#       brain_pages.partner_id matches app.partner_id
#   * access_scope=mundi_only: admin only
#   * NULL access_scope: treated as public (legacy rows pre-backfill)
_PARTNER_ISOLATION_POLICY = """
    CREATE POLICY partner_isolation_brain_pages ON brain_pages
    USING (
        CASE
            WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
            WHEN current_setting('app.role', true) = 'admin' THEN true
            WHEN access_scope IS NULL OR access_scope = 'public' THEN true
            WHEN access_scope = 'partner_internal' THEN
                partner_id IS NOT NULL
                AND partner_id::text = coalesce(current_setting('app.partner_id', true), '')
            WHEN access_scope = 'mundi_only' THEN false
            ELSE false
        END
    )
"""


def upgrade() -> None:
    # ── brain_pages column delta ───────────────────────────────────
    # Additive. Existing rows get NULLs — RLS treats NULL access_scope as
    # public (legacy). A background job backfills public/mundi_only later.
    op.execute("""
        ALTER TABLE brain_pages
            ADD COLUMN IF NOT EXISTS language      TEXT,
            ADD COLUMN IF NOT EXISTS license       TEXT,
            ADD COLUMN IF NOT EXISTS source_id     TEXT,
            ADD COLUMN IF NOT EXISTS fetched_at    TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS access_scope  TEXT,
            ADD COLUMN IF NOT EXISTS partner_id    UUID
    """)
    op.execute("""
        ALTER TABLE brain_pages
            ADD CONSTRAINT brain_pages_access_scope_chk
            CHECK (access_scope IS NULL
                   OR access_scope IN ('public', 'partner_internal', 'mundi_only'))
    """)
    op.execute("""
        ALTER TABLE brain_pages
            ADD CONSTRAINT brain_pages_partner_scope_chk
            CHECK (access_scope <> 'partner_internal' OR partner_id IS NOT NULL)
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_pages_source_id ON brain_pages(source_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_pages_language  ON brain_pages(language)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_pages_fetched   ON brain_pages(fetched_at)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_pages_scope_partner "
        "ON brain_pages(access_scope, partner_id)"
    )

    op.execute(_PARTNER_ISOLATION_POLICY)

    # ── brain_tables ───────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_tables (
            id          SERIAL PRIMARY KEY,
            page_id     INTEGER     NOT NULL REFERENCES brain_pages(id) ON DELETE CASCADE,
            table_idx   INTEGER     NOT NULL,
            json_data   JSONB       NOT NULL,
            row_count   INTEGER,
            col_headers TEXT[],
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(page_id, table_idx)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_tables_page ON brain_tables(page_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_tables_json ON brain_tables USING GIN(json_data)")
    op.execute("ALTER TABLE brain_tables ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation_brain_tables ON brain_tables
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE page_id IN (SELECT id FROM brain_pages)
            END
        )
    """)

    # ── brain_sources ──────────────────────────────────────────────
    # Admin-managed fetcher registry. Not tenant-scoped. Restricted to
    # admin role via policy (non-admin sessions see nothing).
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_sources (
            source_id       TEXT PRIMARY KEY,
            url             TEXT NOT NULL,
            fetcher_type    TEXT NOT NULL,
            schedule_cron   TEXT,
            tier            TEXT NOT NULL CHECK (tier IN ('T1','T2','T3','T4')),
            last_success    TIMESTAMPTZ,
            last_error      TEXT,
            last_tos_check  TIMESTAMPTZ,
            status          TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','paused','requires_auth','opted_out','broken')),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_sources_tier   ON brain_sources(tier)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_sources_status ON brain_sources(status)")
    op.execute("ALTER TABLE brain_sources ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY admin_only_brain_sources ON brain_sources
        USING (
            coalesce(current_setting('app.user_id', true), '') = ''
            OR current_setting('app.role', true) = 'admin'
        )
    """)

    # ── brain_entities + brain_entity_refs ─────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_entities (
            id              SERIAL PRIMARY KEY,
            entity_uuid     UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
            canonical_name  TEXT         NOT NULL,
            aliases         TEXT[]       NOT NULL DEFAULT '{}',
            entity_type     TEXT         NOT NULL
                            CHECK (entity_type IN ('admin_area','institution','crop','scheme','person')),
            geom            GEOMETRY(Geometry, 4326),
            source_refs     JSONB        NOT NULL DEFAULT '[]',
            partner_id      UUID,
            access_scope    TEXT         CHECK (access_scope IS NULL
                                                OR access_scope IN ('public','partner_internal','mundi_only')),
            confidence      REAL         NOT NULL DEFAULT 1.0,
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_entities_type   ON brain_entities(entity_type)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_entities_canon  ON brain_entities(canonical_name)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_entities_aliases ON brain_entities USING GIN(aliases)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_entities_geom   ON brain_entities USING GIST(geom)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_entities_partner ON brain_entities(partner_id)")
    op.execute("ALTER TABLE brain_entities ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY partner_isolation_brain_entities ON brain_entities
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                WHEN current_setting('app.role', true) = 'admin' THEN true
                WHEN access_scope IS NULL OR access_scope = 'public' THEN true
                WHEN access_scope = 'partner_internal' THEN
                    partner_id IS NOT NULL
                    AND partner_id::text = coalesce(current_setting('app.partner_id', true), '')
                WHEN access_scope = 'mundi_only' THEN false
                ELSE false
            END
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_entity_refs (
            id            SERIAL PRIMARY KEY,
            entity_id     INTEGER     NOT NULL REFERENCES brain_entities(id) ON DELETE CASCADE,
            page_id       INTEGER     NOT NULL REFERENCES brain_pages(id)    ON DELETE CASCADE,
            mention_text  TEXT        NOT NULL,
            confidence    REAL        NOT NULL DEFAULT 1.0,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(entity_id, page_id, mention_text)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_entity_refs_entity ON brain_entity_refs(entity_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_brain_entity_refs_page   ON brain_entity_refs(page_id)")
    op.execute("ALTER TABLE brain_entity_refs ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation_brain_entity_refs ON brain_entity_refs
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE page_id IN (SELECT id FROM brain_pages)
            END
        )
    """)

    # ── sage_query_log ─────────────────────────────────────────────
    # Every Sage retrieval writes a row. acting_partner_id is who the
    # query ran as (NULL for admin/internal). page_id array stores what
    # came back (already RLS-scoped at query time).
    op.execute("""
        CREATE TABLE IF NOT EXISTS sage_query_log (
            id                  SERIAL PRIMARY KEY,
            query_uuid          UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
            query_text          TEXT         NOT NULL,
            retrieved_page_ids  INTEGER[]    NOT NULL DEFAULT '{}',
            reranked_scores     JSONB,
            user_feedback       TEXT,
            acting_user_uuid    UUID         NOT NULL,
            acting_partner_id   UUID,
            acting_role         TEXT,
            latency_ms          INTEGER,
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sage_query_log_partner_time "
        "ON sage_query_log(acting_partner_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sage_query_log_user_time "
        "ON sage_query_log(acting_user_uuid, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sage_query_log_feedback "
        "ON sage_query_log(user_feedback) WHERE user_feedback IS NOT NULL"
    )
    op.execute("ALTER TABLE sage_query_log ENABLE ROW LEVEL SECURITY")
    # SELECT/UPDATE/DELETE: users see their own queries; admins see all;
    # workers (empty app.user_id, e.g. backfill jobs) see all.
    op.execute("""
        CREATE POLICY sage_query_log_read ON sage_query_log
        FOR SELECT
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                WHEN current_setting('app.role', true) = 'admin' THEN true
                ELSE acting_user_uuid::text = current_setting('app.user_id', true)
            END
        )
    """)
    # INSERT: attribution forgery guard. A partner_user session cannot write
    # a row claiming a different user or partner. Admins and workers are
    # exempt (admin for break-glass / replay, workers for backfill).
    #
    # This is the audit-trail integrity contract. If it drifts, we lose the
    # ability to prove to partner A that their retrievals were not
    # misattributed to partner B.
    op.execute("""
        CREATE POLICY sage_query_log_insert ON sage_query_log
        FOR INSERT
        WITH CHECK (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                WHEN current_setting('app.role', true) = 'admin' THEN true
                ELSE
                    acting_user_uuid::text = current_setting('app.user_id', true)
                    AND coalesce(acting_partner_id::text, '')
                        = coalesce(current_setting('app.partner_id', true), '')
            END
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sage_query_log")
    op.execute("DROP TABLE IF EXISTS brain_entity_refs")
    op.execute("DROP TABLE IF EXISTS brain_entities")
    op.execute("DROP TABLE IF EXISTS brain_sources")
    op.execute("DROP TABLE IF EXISTS brain_tables")

    op.execute("DROP POLICY IF EXISTS partner_isolation_brain_pages ON brain_pages")

    op.execute("DROP INDEX IF EXISTS idx_brain_pages_scope_partner")
    op.execute("DROP INDEX IF EXISTS idx_brain_pages_fetched")
    op.execute("DROP INDEX IF EXISTS idx_brain_pages_language")
    op.execute("DROP INDEX IF EXISTS idx_brain_pages_source_id")

    op.execute("ALTER TABLE brain_pages DROP CONSTRAINT IF EXISTS brain_pages_partner_scope_chk")
    op.execute("ALTER TABLE brain_pages DROP CONSTRAINT IF EXISTS brain_pages_access_scope_chk")
    op.execute("""
        ALTER TABLE brain_pages
            DROP COLUMN IF EXISTS partner_id,
            DROP COLUMN IF EXISTS access_scope,
            DROP COLUMN IF EXISTS fetched_at,
            DROP COLUMN IF EXISTS source_id,
            DROP COLUMN IF EXISTS license,
            DROP COLUMN IF EXISTS language
    """)
