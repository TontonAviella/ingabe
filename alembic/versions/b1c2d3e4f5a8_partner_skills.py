"""partner_skills table for Phase 4 discoverable partner-scoped tool allowlists

Revision ID: b1c2d3e4f5a8
Revises: b1c2d3e4f5a7
Create Date: 2026-05-12

Phase 4 of the OpenClaw-pattern-stealing roadmap. The Sage runtime asks
this table 'which tools is partner X allowed to call?' before assembling
the tool payload sent to the LLM. Absence of any row for a partner means
no restriction (legacy behaviour). One or more rows means an allowlist —
only those skills go to the LLM.

Why an allowlist (not a denylist): partners get a smaller, sharper menu
that matches what they actually pay for. BK Insurance shouldn't see
QGIS reproject in their tool list; a research partner shouldn't see
insurance_render_payout. Smaller menus also reduce tool-confusion
hallucinations on smaller LLMs.

partner_id is TEXT (not UUID) to stay channel-agnostic — matches
alert_subscriptions.partner_id, decoupled from Clerk org UUIDs so
CLI/cron/test partners can register without a Clerk account.
"""

from alembic import op
import sqlalchemy as sa

revision: str = "b1c2d3e4f5a8"
down_revision: str = "b1c2d3e4f5a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "partner_skills",
        sa.Column("partner_id", sa.Text(), nullable=False),
        sa.Column(
            "skill_name",
            sa.Text(),
            nullable=False,
            comment="Tool/function name as it appears in tools.json or pydantic_tool_calls",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default="true",
            comment="If false, the row exists for audit but the tool is hidden",
        ),
        sa.Column(
            "note",
            sa.Text(),
            comment="Free-form note: why this skill, when it was granted, etc.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("partner_id", "skill_name", name="pk_partner_skills"),
    )

    op.create_index(
        "ix_partner_skills_partner",
        "partner_skills",
        ["partner_id", "enabled"],
    )

    op.execute("""
        CREATE OR REPLACE FUNCTION update_partner_skills_timestamp()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_partner_skills_updated_at
        BEFORE UPDATE ON partner_skills
        FOR EACH ROW EXECUTE FUNCTION update_partner_skills_timestamp();
    """)

    op.execute("ALTER TABLE partner_skills ENABLE ROW LEVEL SECURITY;")
    # FORCE so the table-owner role (mundiuser) is also subject to RLS — without
    # this, owner-bypass renders the policy decorative. Normalized to the
    # CASE WHEN coalesce(...) = '' THEN true pattern used across the codebase
    # (e.g. c1d2e3f4a5b9) so empty-GUC admin context is handled uniformly.
    op.execute("ALTER TABLE partner_skills FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY partner_skills_partner_isolation
        ON partner_skills
        USING (
            CASE
                WHEN coalesce(current_setting('app.partner_id', true), '') = '' THEN true
                ELSE partner_id = current_setting('app.partner_id', true)
            END
        )
        WITH CHECK (
            CASE
                WHEN coalesce(current_setting('app.partner_id', true), '') = '' THEN true
                ELSE partner_id = current_setting('app.partner_id', true)
            END
        );
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS partner_skills_partner_isolation ON partner_skills;")
    op.execute("DROP TRIGGER IF EXISTS trg_partner_skills_updated_at ON partner_skills;")
    op.execute("DROP FUNCTION IF EXISTS update_partner_skills_timestamp();")
    op.drop_index("ix_partner_skills_partner", table_name="partner_skills")
    op.drop_table("partner_skills")
