"""alert_subscriptions table for Phase 3 proactive Sage cron-fired alerts

Revision ID: b1c2d3e4f5a7
Revises: c9d0e1f2a3b4
Create Date: 2026-05-12

Phase 3 of the OpenClaw-pattern-stealing roadmap. A cron worker
(`src/cron/sage_alerts.py`) reads this table and, for each due row, renders
a snapshot of the configured map+bbox and publishes a payload on the
'mundi:render_snapshot' Redis channel. WhatsApp/Telegram senders already
consume that channel, so this row is the *only* new state: the rendering
and delivery pipeline is unchanged.

Why no LLM in the loop: weekly/daily reports for insurance partners don't
need natural-language reasoning. A fixed cron + parameterised caption is
deterministic, auditable, and cheap. Sage's interactive runtime is for
ad-hoc partner questions; this table is for the recurring reports those
partners said they wanted on a schedule.
"""

from alembic import op
import sqlalchemy as sa

revision: str = "b1c2d3e4f5a7"
down_revision: str = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("partner_id", sa.Text(), nullable=False),
        sa.Column("map_id", sa.Text(), nullable=False),
        sa.Column(
            "bbox",
            sa.Text(),
            nullable=False,
            comment="'west,south,east,north' in WGS84",
        ),
        sa.Column("width", sa.Integer(), nullable=False, server_default="1024"),
        sa.Column("height", sa.Integer(), nullable=False, server_default="600"),
        sa.Column(
            "delivery_channel",
            sa.Text(),
            nullable=False,
            comment="telegram | whatsapp | email",
        ),
        sa.Column(
            "recipient",
            sa.Text(),
            nullable=False,
            comment="chat_id, E.164 phone (no +), or email address",
        ),
        sa.Column(
            "caption_template",
            sa.Text(),
            nullable=False,
            server_default="Sage alert {fire_ts_utc}",
        ),
        sa.Column(
            "cron_expr",
            sa.Text(),
            nullable=False,
            comment="5-field cron (min hour dom mon dow); UTC. e.g. '0 6 * * 1' = Mon 06:00 UTC",
        ),
        sa.Column("last_fired_at", sa.DateTime(timezone=True)),
        sa.Column(
            "next_fire_at",
            sa.DateTime(timezone=True),
            comment="Cached next fire time so the cron worker can do a cheap range query",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default="true",
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
        sa.CheckConstraint(
            "delivery_channel IN ('telegram', 'whatsapp', 'email')",
            name="ck_alert_subscriptions_channel",
        ),
    )

    op.create_index(
        "ix_alert_subscriptions_due",
        "alert_subscriptions",
        ["enabled", "next_fire_at"],
    )
    op.create_index(
        "ix_alert_subscriptions_partner",
        "alert_subscriptions",
        ["partner_id"],
    )

    op.execute("""
        CREATE OR REPLACE FUNCTION update_alert_subscriptions_timestamp()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_alert_subscriptions_updated_at
        BEFORE UPDATE ON alert_subscriptions
        FOR EACH ROW EXECUTE FUNCTION update_alert_subscriptions_timestamp();
    """)

    op.execute("ALTER TABLE alert_subscriptions ENABLE ROW LEVEL SECURITY;")
    # FORCE so the table-owner role (mundiuser) is also subject to RLS — without
    # this, owner-bypass renders the policy decorative. Normalized to the
    # CASE WHEN coalesce(...) = '' THEN true pattern used across the codebase
    # (e.g. c1d2e3f4a5b9) so empty-GUC admin context is handled uniformly.
    op.execute("ALTER TABLE alert_subscriptions FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY alert_subscriptions_partner_isolation
        ON alert_subscriptions
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
    op.execute("DROP POLICY IF EXISTS alert_subscriptions_partner_isolation ON alert_subscriptions;")
    op.execute("DROP TRIGGER IF EXISTS trg_alert_subscriptions_updated_at ON alert_subscriptions;")
    op.execute("DROP FUNCTION IF EXISTS update_alert_subscriptions_timestamp();")
    op.drop_index("ix_alert_subscriptions_partner", table_name="alert_subscriptions")
    op.drop_index("ix_alert_subscriptions_due", table_name="alert_subscriptions")
    op.drop_table("alert_subscriptions")
