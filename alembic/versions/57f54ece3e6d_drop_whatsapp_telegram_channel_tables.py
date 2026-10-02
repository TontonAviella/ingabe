"""Drop the WhatsApp/Telegram channel tables.

The WhatsApp and Telegram integrations were removed (senders, inbound
/internal/inbox route, alert cron, render_map_snapshot delivery). These
tables only served them:

- user_channel_bindings, channel_bind_codes (d1e2f3a4b5c6)
- alert_subscriptions (b1c2d3e4f5a7)

Dropping them deletes any stored channel bindings and alert subscriptions.
Downgrade re-runs the original migrations' upgrade(), restoring the empty
tables, indexes and RLS policies exactly as they were created.

Revision ID: 57f54ece3e6d
Revises: e3f4a5b6c7d8
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision: str = "57f54ece3e6d"
down_revision: str = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None

_VERSIONS = Path(__file__).resolve().parent
_ORIGINALS = (
    "b1c2d3e4f5a7_alert_subscriptions.py",
    "d1e2f3a4b5c6_user_channel_bindings_and_bind_codes.py",
)


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS channel_bind_codes")
    op.execute("DROP TABLE IF EXISTS user_channel_bindings")
    op.execute("DROP TABLE IF EXISTS alert_subscriptions")


def downgrade() -> None:
    for filename in _ORIGINALS:
        spec = importlib.util.spec_from_file_location(f"_restore_{filename[:12]}", _VERSIONS / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.upgrade()
