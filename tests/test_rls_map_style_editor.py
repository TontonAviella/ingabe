"""Regression coverage for editor writes to map_layer_styles."""

import uuid

import asyncpg
import pytest

from src.database.pool import _build_postgres_url
from src.database.migrate import run_migrations


_RLS_ROLE = "rls_test_role"


async def _open(user_id: str | None):
    conn = await asyncpg.connect(_build_postgres_url())
    bypasses = await conn.fetchval(
        "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    if bypasses:
        await conn.execute(f"SET ROLE {_RLS_ROLE}")
    await conn.execute("SELECT set_config('app.user_id', $1, false)", user_id or "")
    return conn


async def _ensure_rls_role() -> None:
    conn = await asyncpg.connect(_build_postgres_url())
    try:
        await conn.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_RLS_ROLE}') THEN
                    CREATE ROLE {_RLS_ROLE} NOLOGIN;
                END IF;
            END $$
        """)
        await conn.execute(f"GRANT USAGE ON SCHEMA public TO {_RLS_ROLE}")
        await conn.execute(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {_RLS_ROLE}")
        await conn.execute(f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {_RLS_ROLE}")
    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_editor_can_attach_style_only_to_editable_map() -> None:
    await run_migrations()
    await _ensure_rls_role()

    tag = uuid.uuid4().hex[:7]
    owner = str(uuid.uuid4())
    foreign_owner = str(uuid.uuid4())
    editor = str(uuid.uuid4())
    project_id = f"pe{tag}"[:12]
    foreign_project_id = f"pf{tag}"[:12]
    editable_map_id = f"me{tag}"[:12]
    foreign_map_id = f"mf{tag}"[:12]
    layer_id = f"le{tag}"[:12]
    style_id = f"se{tag}"[:12]

    admin = await _open(None)
    editor_conn = None
    try:
        await admin.execute(
            """
            INSERT INTO user_mundiai_projects
                (id, owner_uuid, editor_uuids, viewer_uuids, title)
            VALUES ($1, $2::uuid, ARRAY[$3::uuid], ARRAY[]::uuid[], 'editor policy')
            """,
            project_id,
            owner,
            editor,
        )
        await admin.execute(
            """
            INSERT INTO user_mundiai_projects
                (id, owner_uuid, editor_uuids, viewer_uuids, title)
            VALUES ($1, $2::uuid, ARRAY[]::uuid[], ARRAY[]::uuid[], 'foreign policy')
            """,
            foreign_project_id,
            foreign_owner,
        )
        await admin.executemany(
            """
            INSERT INTO user_mundiai_maps
                (id, project_id, owner_uuid, title)
            VALUES ($1, $2, $3::uuid, 'editor policy')
            """,
            [
                (editable_map_id, project_id, owner),
                (foreign_map_id, foreign_project_id, foreign_owner),
            ],
        )

        editor_conn = await _open(editor)
        await editor_conn.execute(
            """
            INSERT INTO map_layers (layer_id, owner_uuid, name, type)
            VALUES ($1, $2::uuid, 'editor layer', 'raster')
            """,
            layer_id,
            editor,
        )
        await editor_conn.execute(
            """
            INSERT INTO layer_styles (style_id, layer_id, style_json, created_by)
            VALUES ($1, $2, '[]'::jsonb, $3::uuid)
            """,
            style_id,
            layer_id,
            editor,
        )
        await editor_conn.execute(
            "INSERT INTO map_layer_styles (map_id, layer_id, style_id) VALUES ($1, $2, $3)",
            editable_map_id,
            layer_id,
            style_id,
        )

        with pytest.raises(asyncpg.PostgresError):
            await editor_conn.execute(
                "INSERT INTO map_layer_styles (map_id, layer_id, style_id) VALUES ($1, $2, $3)",
                foreign_map_id,
                layer_id,
                style_id,
            )
    finally:
        if editor_conn is not None:
            await editor_conn.close()
        await admin.execute(
            "DELETE FROM map_layer_styles WHERE map_id = ANY($1::text[])",
            [editable_map_id, foreign_map_id],
        )
        await admin.execute("DELETE FROM layer_styles WHERE style_id = $1", style_id)
        await admin.execute("DELETE FROM map_layers WHERE layer_id = $1", layer_id)
        await admin.execute(
            "DELETE FROM user_mundiai_maps WHERE id = ANY($1::text[])",
            [editable_map_id, foreign_map_id],
        )
        await admin.execute(
            "DELETE FROM user_mundiai_projects WHERE id = ANY($1::text[])",
            [project_id, foreign_project_id],
        )
        await admin.close()
