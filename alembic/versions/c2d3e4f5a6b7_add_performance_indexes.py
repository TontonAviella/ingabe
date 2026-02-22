"""add performance indexes for production

Revision ID: c2d3e4f5a6b7
Revises: b1a2c3d4e5f6
Create Date: 2026-02-22 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, None] = "b1a2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Projects: fast lookup by owner
    op.create_index(
        "ix_projects_owner_uuid",
        "user_mundiai_projects",
        ["owner_uuid"],
    )

    # Maps: fast lookup by project and DAG traversal
    op.create_index(
        "ix_maps_project_id",
        "user_mundiai_maps",
        ["project_id"],
    )
    op.create_index(
        "ix_maps_parent_map_id",
        "user_mundiai_maps",
        ["parent_map_id"],
    )
    op.create_index(
        "ix_maps_owner_uuid",
        "user_mundiai_maps",
        ["owner_uuid"],
    )

    # Layers: fast lookup by owner
    op.create_index(
        "ix_layers_owner_uuid",
        "map_layers",
        ["owner_uuid"],
    )

    # Map-layer-styles: fast lookup by layer and style
    op.create_index(
        "ix_map_layer_styles_style_id",
        "map_layer_styles",
        ["style_id"],
    )

    # Layer styles: fast lookup by layer
    op.create_index(
        "ix_layer_styles_layer_id",
        "layer_styles",
        ["layer_id"],
    )

    # Chat messages: fast lookup by conversation and map
    op.create_index(
        "ix_chat_messages_conversation_id",
        "chat_completion_messages",
        ["conversation_id"],
    )
    op.create_index(
        "ix_chat_messages_map_id",
        "chat_completion_messages",
        ["map_id"],
    )

    # Conversations: fast lookup by project and owner
    op.create_index(
        "ix_conversations_project_id",
        "conversations",
        ["project_id"],
    )
    op.create_index(
        "ix_conversations_owner_uuid",
        "conversations",
        ["owner_uuid"],
    )

    # PostGIS connections: fast lookup by project
    op.create_index(
        "ix_postgres_connections_project_id",
        "project_postgres_connections",
        ["project_id"],
    )

    # PostGIS summaries: fast lookup by connection
    op.create_index(
        "ix_postgres_summaries_connection_id",
        "project_postgres_summary",
        ["connection_id"],
    )

    # Soft-delete partial indexes for common queries
    op.execute(
        "CREATE INDEX ix_projects_active ON user_mundiai_projects (id) WHERE soft_deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_maps_active ON user_mundiai_maps (id) WHERE soft_deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_conversations_active ON conversations (id) WHERE soft_deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_conversations_active")
    op.execute("DROP INDEX IF EXISTS ix_maps_active")
    op.execute("DROP INDEX IF EXISTS ix_projects_active")
    op.drop_index("ix_postgres_summaries_connection_id", "project_postgres_summary")
    op.drop_index("ix_postgres_connections_project_id", "project_postgres_connections")
    op.drop_index("ix_conversations_owner_uuid", "conversations")
    op.drop_index("ix_conversations_project_id", "conversations")
    op.drop_index("ix_chat_messages_map_id", "chat_completion_messages")
    op.drop_index("ix_chat_messages_conversation_id", "chat_completion_messages")
    op.drop_index("ix_layer_styles_layer_id", "layer_styles")
    op.drop_index("ix_map_layer_styles_style_id", "map_layer_styles")
    op.drop_index("ix_layers_owner_uuid", "map_layers")
    op.drop_index("ix_maps_owner_uuid", "user_mundiai_maps")
    op.drop_index("ix_maps_parent_map_id", "user_mundiai_maps")
    op.drop_index("ix_maps_project_id", "user_mundiai_maps")
    op.drop_index("ix_projects_owner_uuid", "user_mundiai_projects")
