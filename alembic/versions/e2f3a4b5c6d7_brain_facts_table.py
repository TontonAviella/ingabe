"""brain_facts: typed-claim ledger for trajectory queries

Implements GBrain's `## Facts` fence model. Each row is one typed claim
about one brain entity at one point in time. Multiple rows per
(page_id, key) form a chronological trajectory that downstream tools
walk for regression analysis.

GBrain's published evals on relational + trajectory queries show this
shape outperforms timeline-as-text-blob by enabling structured queries
("show me Cyampirita's NDVI history with regressions flagged") instead
of having to grep through compiled_truth.

Schema is independent from brain_pages.timeline (which is a free-form
markdown log). brain_facts is structured per claim — same data can be
in both for now (the fence parser is opt-in; old pages keep their
timeline-only shape).

RLS is INHERITED via page_id → brain_pages join in queries. We
don't need a separate partner_id column because every claim is scoped
to a page, and brain_pages already enforces partner isolation via the
existing FORCE ROW LEVEL SECURITY policy. The trigger here just keeps
JOIN-based RLS working by stamping a denormalised partner_id at insert
time for direct queries that bypass the join.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-05-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS brain_facts (
            id            BIGSERIAL PRIMARY KEY,
            page_id       BIGINT NOT NULL REFERENCES brain_pages(id) ON DELETE CASCADE,
            partner_id    UUID,
            key           TEXT NOT NULL,
            value         TEXT NOT NULL,
            value_numeric DOUBLE PRECISION,
            unit          TEXT,
            valid_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until   TIMESTAMPTZ,
            status        TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'superseded', 'forgotten')),
            superseded_by BIGINT REFERENCES brain_facts(id) ON DELETE SET NULL,
            source        TEXT NOT NULL DEFAULT 'fence:reconcile',
            context       TEXT NOT NULL DEFAULT '',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)

    # Indexes for the common access patterns.
    # 1. Trajectory walk per (page, key) — the primary read pattern.
    op.execute("""
        CREATE INDEX IF NOT EXISTS brain_facts_page_key_time_idx
            ON brain_facts (page_id, key, valid_from DESC);
    """)
    # 2. Cross-entity "all NDVI claims in this partner" — secondary.
    op.execute("""
        CREATE INDEX IF NOT EXISTS brain_facts_partner_key_time_idx
            ON brain_facts (partner_id, key, valid_from DESC)
            WHERE partner_id IS NOT NULL;
    """)

    # RLS: stamp partner_id from the linked brain_pages row on every
    # insert/update. Mirrors the brain_links partner-isolation trigger
    # pattern. This lets direct WHERE partner_id = ... queries skip
    # the join when we know which partner is asking.
    op.execute("""
        CREATE OR REPLACE FUNCTION brain_facts_stamp_partner_id()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.partner_id IS NULL THEN
                SELECT partner_id INTO NEW.partner_id
                FROM brain_pages WHERE id = NEW.page_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        DROP TRIGGER IF EXISTS brain_facts_stamp_partner_id_trigger ON brain_facts;
        CREATE TRIGGER brain_facts_stamp_partner_id_trigger
            BEFORE INSERT OR UPDATE ON brain_facts
            FOR EACH ROW
            EXECUTE FUNCTION brain_facts_stamp_partner_id();
    """)

    # Enable RLS on the table and add the standard partner-isolation
    # policy. Mirrors brain_links / brain_pages so app code that's
    # already partner-scoped via app.partner_id GUC works unchanged.
    op.execute("ALTER TABLE brain_facts ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE brain_facts FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY brain_facts_partner_isolation ON brain_facts
            USING (
                partner_id IS NULL
                OR partner_id::text = COALESCE(
                    NULLIF(current_setting('app.partner_id', true), ''),
                    '')
            )
            WITH CHECK (
                partner_id IS NULL
                OR partner_id::text = COALESCE(
                    NULLIF(current_setting('app.partner_id', true), ''),
                    '')
            );
    """)


def downgrade() -> None:
    # Reverse order: policy, trigger, function, indexes, table.
    op.execute("DROP POLICY IF EXISTS brain_facts_partner_isolation ON brain_facts;")
    op.execute(
        "DROP TRIGGER IF EXISTS brain_facts_stamp_partner_id_trigger ON brain_facts;"
    )
    op.execute("DROP FUNCTION IF EXISTS brain_facts_stamp_partner_id();")
    op.execute("DROP INDEX IF EXISTS brain_facts_partner_key_time_idx;")
    op.execute("DROP INDEX IF EXISTS brain_facts_page_key_time_idx;")
    op.execute("DROP TABLE IF EXISTS brain_facts;")
