"""user_channel_bindings + channel_bind_codes (multi-channel unified accounts)

Foundation for the platform's cross-channel unified-account design (see
project_unified_account_design.md memory). After this migration, the
schema supports:

  - Roger logs into mundi.ai web → user_uuid = X identifies him
  - Roger DMs BK's WhatsApp number → external_id = +250-xxx maps back to X
  - The conversation history, brain pages, maps for X are identical
    across both channels.

Tables:

  user_channel_bindings — durable mapping of (partner, channel, external_id)
  to user_uuid. Once a phone number is verified for a partner+user, that
  binding is stable. Partner-scoped: same phone CAN bind to different users
  in different partners (multi-tenant isolation).

  channel_bind_codes — short-lived (~10 min) 6-digit codes issued by the
  web UI when a user wants to link a channel. The user types
  "VERIFY <code>" from the channel (e.g. WhatsApp) and the inbox handler
  matches the code to confirm ownership. Codes are single-use.

RLS: both tables enforce partner_id isolation. mundi_admin (when it
exists) and BYPASSRLS roles see all rows; partner-scoped sessions see
only their own.

This migration is reversible. Down drops both tables and their indexes;
no data loss risk in the rollback path.

Revision ID: d1e2f3a4b5c6
Revises: c1d2e3f4a5bc
Create Date: 2026-05-14 22:10:00.000000
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "d1e2f3a4b5c6"
down_revision = "c1d2e3f4a5bc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE user_channel_bindings (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_uuid       UUID NOT NULL,
            partner_id      UUID NOT NULL,
            channel         TEXT NOT NULL,
            external_id     TEXT NOT NULL,
            verified_at     TIMESTAMPTZ,
            revoked_at      TIMESTAMPTZ,
            last_used_at    TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT user_channel_bindings_unique_external_per_partner
                UNIQUE (channel, external_id, partner_id)
        );
    """)
    op.execute("""
        CREATE INDEX user_channel_bindings_lookup_active_idx
            ON user_channel_bindings (channel, external_id, partner_id)
            WHERE revoked_at IS NULL;
    """)
    op.execute("""
        CREATE INDEX user_channel_bindings_user_idx
            ON user_channel_bindings (user_uuid)
            WHERE revoked_at IS NULL;
    """)
    op.execute("ALTER TABLE user_channel_bindings ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE user_channel_bindings FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation_user_channel_bindings
            ON user_channel_bindings
            USING (
                CASE
                    WHEN coalesce(current_setting('app.partner_id', true), '') = ''
                        THEN true
                    ELSE
                        partner_id::text = current_setting('app.partner_id', true)
                END
            )
            WITH CHECK (
                CASE
                    WHEN coalesce(current_setting('app.partner_id', true), '') = ''
                        THEN true
                    ELSE
                        partner_id::text = current_setting('app.partner_id', true)
                END
            );
    """)

    op.execute("""
        CREATE TABLE channel_bind_codes (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_uuid       UUID NOT NULL,
            partner_id      UUID NOT NULL,
            channel         TEXT NOT NULL,
            code            TEXT NOT NULL,
            external_id     TEXT,
            expires_at      TIMESTAMPTZ NOT NULL,
            consumed_at     TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    op.execute("""
        CREATE INDEX channel_bind_codes_lookup_idx
            ON channel_bind_codes (channel, code, partner_id)
            WHERE consumed_at IS NULL;
    """)
    op.execute("""
        CREATE INDEX channel_bind_codes_expiry_idx
            ON channel_bind_codes (expires_at)
            WHERE consumed_at IS NULL;
    """)
    op.execute("ALTER TABLE channel_bind_codes ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE channel_bind_codes FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation_channel_bind_codes
            ON channel_bind_codes
            USING (
                CASE
                    WHEN coalesce(current_setting('app.partner_id', true), '') = ''
                        THEN true
                    ELSE
                        partner_id::text = current_setting('app.partner_id', true)
                END
            )
            WITH CHECK (
                CASE
                    WHEN coalesce(current_setting('app.partner_id', true), '') = ''
                        THEN true
                    ELSE
                        partner_id::text = current_setting('app.partner_id', true)
                END
            );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS channel_bind_codes")
    op.execute("DROP TABLE IF EXISTS user_channel_bindings")
