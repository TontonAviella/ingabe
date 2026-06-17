"""Cross-user RLS isolation for the 5 non-brain tenant tables.

Migration under test: c1d2e3f4a5b9_rls_maps_layers_styles_chat_pg.py

Closes the gap our pre-deploy audit surfaced: before c1d2e3f4a5b9 these tables
had ENABLE ROW LEVEL SECURITY off — a misrouted query from a partner channel
worker could read or mutate rows owned by a different tenant.

Five assertions, mirroring tests/brain/test_partner_isolation.py:

  1. User A's session cannot SELECT User B's user_mundiai_maps row.
  2. User A's session cannot SELECT User B's map_layers row.
  3. User A's session cannot SELECT User B's map_layer_styles row
     (policy joins through user_mundiai_maps).
  4. User A's session cannot SELECT User B's chat_completion_messages row
     (policy joins through user_mundiai_maps; sender_id is system identity,
     not row owner — using sender_id for isolation would be wrong).
  5. User A's session cannot SELECT User B's project_postgres_connections row.

Plus mutation checks: UPDATE / DELETE issued from User A's session against
User B's rows must affect zero rows (RLS filters the row away — Postgres
does not raise, it just sees nothing to mutate). This is the silent-failure
class we have to prove can't happen.

Also: empty-GUC sessions (migrations, admin scripts) can see everything via
the CASE WHEN bypass. The bypass exists by design — see f3a4b5c6d7e8 — so
we lock that branch under test too. If someone removes the bypass without
fixing every migration that runs as mundiuser, we'll find out here.
"""

import uuid

import asyncpg
import pytest
import pytest_asyncio

from src.database.pool import _build_postgres_url

pytestmark = pytest.mark.asyncio(loop_scope="module")


USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())

# Unique short ID prefix so parallel test runs don't collide on PKs.
# user_mundiai_maps.id is varchar(12), so we use 8 random hex chars + 2-char
# tag = 10 chars, leaving room for a trailing role suffix.
RUN_TAG = uuid.uuid4().hex[:6]


_RLS_ROLE = "rls_test_role"


async def _ensure_rls_role() -> None:
    """Same NOLOGIN downgrade role as tests/brain/test_partner_isolation.py.

    If the connecting role has BYPASSRLS (dev environments where mundiuser
    is still superuser pre-a0b1c2d3e4f5), we SET ROLE to this restricted role
    inside _open() so RLS actually applies. After mundiuser is downgraded
    NOSUPERUSER NOBYPASSRLS in prod, this becomes a no-op.
    """
    url = _build_postgres_url()
    c = await asyncpg.connect(url)
    try:
        await c.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_RLS_ROLE}') THEN
                    CREATE ROLE {_RLS_ROLE} NOLOGIN;
                END IF;
            END $$
        """)
        await c.execute(f"GRANT USAGE ON SCHEMA public TO {_RLS_ROLE}")
        await c.execute(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {_RLS_ROLE}")
        await c.execute(f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {_RLS_ROLE}")
    finally:
        await c.close()


async def _open(user_id: str | None):
    """Connect and set app.user_id to the given UUID (or empty for admin)."""
    url = _build_postgres_url()
    c = await asyncpg.connect(url)
    bypasses = await c.fetchval(
        "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    if bypasses:
        await c.execute(f"SET ROLE {_RLS_ROLE}")
    await c.execute("SELECT set_config('app.user_id', $1, false)", user_id or "")
    return c


def _id(prefix: str) -> str:
    """Generate a short varchar(12) ID with a per-run unique suffix."""
    return f"{prefix}{RUN_TAG}{uuid.uuid4().hex[:2]}"[:12]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def seeded():
    """Seed two parallel tenants worth of data through an admin connection.

    Admin context = empty app.user_id GUC = CASE WHEN bypass branch fires.
    Each user gets: project → map → layer → style → chat message → pg conn.
    """
    from src.database.migrate import run_migrations
    await run_migrations()
    await _ensure_rls_role()

    admin = await _open(None)  # empty GUC = migration/admin bypass

    ids = {
        "proj_a": _id("pA"),
        "proj_b": _id("pB"),
        "map_a": _id("mA"),
        "map_b": _id("mB"),
        "layer_a": _id("lA"),
        "layer_b": _id("lB"),
        "style_a": _id("sA"),
        "style_b": _id("sB"),
        "pgc_a": _id("cA"),
        "pgc_b": _id("cB"),
    }

    # ---- Projects (parent of maps, pg_connections) -----------------------
    for proj_id, owner in (
        (ids["proj_a"], USER_A),
        (ids["proj_b"], USER_B),
    ):
        await admin.execute(
            """
            INSERT INTO user_mundiai_projects (id, owner_uuid, editor_uuids, viewer_uuids, title)
            VALUES ($1, $2::uuid, ARRAY[]::uuid[], ARRAY[]::uuid[], $3)
            """,
            proj_id, owner, f"rls-iso project {proj_id}",
        )

    # ---- Maps -----------------------------------------------------------
    for map_id, proj_id, owner in (
        (ids["map_a"], ids["proj_a"], USER_A),
        (ids["map_b"], ids["proj_b"], USER_B),
    ):
        await admin.execute(
            """
            INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title)
            VALUES ($1, $2, $3::uuid, $4)
            """,
            map_id, proj_id, owner, f"rls-iso map {map_id}",
        )

    # ---- Layers ---------------------------------------------------------
    for layer_id, owner in (
        (ids["layer_a"], USER_A),
        (ids["layer_b"], USER_B),
    ):
        await admin.execute(
            """
            INSERT INTO map_layers (layer_id, owner_uuid, name, type)
            VALUES ($1, $2::uuid, $3, 'vector')
            """,
            layer_id, owner, f"rls-iso layer {layer_id}",
        )

    # ---- Layer styles (referenced by map_layer_styles.style_id) ---------
    for style_id, layer_id, owner in (
        (ids["style_a"], ids["layer_a"], USER_A),
        (ids["style_b"], ids["layer_b"], USER_B),
    ):
        await admin.execute(
            """
            INSERT INTO layer_styles (style_id, layer_id, style_json, created_by)
            VALUES ($1, $2, '{}'::jsonb, $3::uuid)
            """,
            style_id, layer_id, owner,
        )

    # ---- map_layer_styles (RLS via map_id → user_mundiai_maps owner) ----
    for map_id, layer_id, style_id in (
        (ids["map_a"], ids["layer_a"], ids["style_a"]),
        (ids["map_b"], ids["layer_b"], ids["style_b"]),
    ):
        await admin.execute(
            """
            INSERT INTO map_layer_styles (map_id, layer_id, style_id)
            VALUES ($1, $2, $3)
            """,
            map_id, layer_id, style_id,
        )

    # ---- chat_completion_messages (RLS via map_id) ----------------------
    # sender_id is the LLM/system identity, NOT the owner. We pick the user
    # uuid as sender_id only because it's a convenient valid uuid; the RLS
    # policy looks at map_id only.
    for map_id, sender in (
        (ids["map_a"], USER_A),
        (ids["map_b"], USER_B),
    ):
        await admin.execute(
            """
            INSERT INTO chat_completion_messages
                (map_id, sender_id, message_json)
            VALUES ($1, $2::uuid, $3::jsonb)
            """,
            map_id, sender, '{"role": "user", "content": "isolation seed"}',
        )

    # ---- project_postgres_connections -----------------------------------
    for pgc_id, proj_id, owner in (
        (ids["pgc_a"], ids["proj_a"], USER_A),
        (ids["pgc_b"], ids["proj_b"], USER_B),
    ):
        await admin.execute(
            """
            INSERT INTO project_postgres_connections
                (id, project_id, user_id, connection_uri, connection_name)
            VALUES ($1, $2, $3::uuid, $4, $5)
            """,
            pgc_id, proj_id, owner,
            f"postgresql://example/{pgc_id}", f"rls-iso conn {pgc_id}",
        )

    yield ids

    # ---- Teardown (admin, empty GUC) ------------------------------------
    await admin.execute(
        "DELETE FROM chat_completion_messages WHERE map_id = ANY($1::text[])",
        [ids["map_a"], ids["map_b"]],
    )
    await admin.execute(
        "DELETE FROM map_layer_styles WHERE map_id = ANY($1::text[])",
        [ids["map_a"], ids["map_b"]],
    )
    await admin.execute(
        "DELETE FROM layer_styles WHERE style_id = ANY($1::text[])",
        [ids["style_a"], ids["style_b"]],
    )
    await admin.execute(
        "DELETE FROM project_postgres_connections WHERE id = ANY($1::text[])",
        [ids["pgc_a"], ids["pgc_b"]],
    )
    await admin.execute(
        "DELETE FROM map_layers WHERE layer_id = ANY($1::text[])",
        [ids["layer_a"], ids["layer_b"]],
    )
    await admin.execute(
        "DELETE FROM user_mundiai_maps WHERE id = ANY($1::text[])",
        [ids["map_a"], ids["map_b"]],
    )
    await admin.execute(
        "DELETE FROM user_mundiai_projects WHERE id = ANY($1::text[])",
        [ids["proj_a"], ids["proj_b"]],
    )
    await admin.close()


@pytest_asyncio.fixture(loop_scope="module")
async def conn_a(seeded):
    c = await _open(USER_A)
    yield c
    await c.close()


@pytest_asyncio.fixture(loop_scope="module")
async def conn_b(seeded):
    c = await _open(USER_B)
    yield c
    await c.close()


@pytest_asyncio.fixture(loop_scope="module")
async def conn_admin(seeded):
    c = await _open(None)
    yield c
    await c.close()


# ---------------------------------------------------------------------------
# SELECT isolation — each of the 5 tables
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_maps_select_isolation(conn_a, conn_b, seeded):
    row_a_sees_b = await conn_a.fetchrow(
        "SELECT id FROM user_mundiai_maps WHERE id = $1", seeded["map_b"],
    )
    assert row_a_sees_b is None, "RLS LEAK: user A read user B's map"

    row_b_sees_a = await conn_b.fetchrow(
        "SELECT id FROM user_mundiai_maps WHERE id = $1", seeded["map_a"],
    )
    assert row_b_sees_a is None, "RLS LEAK: user B read user A's map"

    own_a = await conn_a.fetchrow(
        "SELECT id FROM user_mundiai_maps WHERE id = $1", seeded["map_a"],
    )
    assert own_a is not None, "user A can't read their own map — policy too tight"


@pytest.mark.postgres
async def test_map_layers_select_isolation(conn_a, conn_b, seeded):
    row_a_sees_b = await conn_a.fetchrow(
        "SELECT layer_id FROM map_layers WHERE layer_id = $1", seeded["layer_b"],
    )
    assert row_a_sees_b is None, "RLS LEAK: user A read user B's layer"

    row_b_sees_a = await conn_b.fetchrow(
        "SELECT layer_id FROM map_layers WHERE layer_id = $1", seeded["layer_a"],
    )
    assert row_b_sees_a is None, "RLS LEAK: user B read user A's layer"

    own_a = await conn_a.fetchrow(
        "SELECT layer_id FROM map_layers WHERE layer_id = $1", seeded["layer_a"],
    )
    assert own_a is not None


@pytest.mark.postgres
async def test_map_layer_styles_select_isolation(conn_a, conn_b, seeded):
    """RLS joins through user_mundiai_maps — if the maps policy is bypassed
    here, this test catches it."""
    row_a_sees_b = await conn_a.fetchrow(
        "SELECT style_id FROM map_layer_styles WHERE map_id = $1",
        seeded["map_b"],
    )
    assert row_a_sees_b is None, "RLS LEAK: user A read user B's map_layer_style"

    row_b_sees_a = await conn_b.fetchrow(
        "SELECT style_id FROM map_layer_styles WHERE map_id = $1",
        seeded["map_a"],
    )
    assert row_b_sees_a is None, "RLS LEAK: user B read user A's map_layer_style"

    own_a = await conn_a.fetchrow(
        "SELECT style_id FROM map_layer_styles WHERE map_id = $1",
        seeded["map_a"],
    )
    assert own_a is not None


@pytest.mark.postgres
async def test_chat_messages_select_isolation(conn_a, conn_b, seeded):
    """Critical: sender_id can be the LLM's UUID, not the row owner. Policy
    must isolate via map_id → user_mundiai_maps.owner_uuid."""
    row_a_sees_b = await conn_a.fetchrow(
        "SELECT id FROM chat_completion_messages WHERE map_id = $1",
        seeded["map_b"],
    )
    assert row_a_sees_b is None, "RLS LEAK: user A read user B's chat message"

    row_b_sees_a = await conn_b.fetchrow(
        "SELECT id FROM chat_completion_messages WHERE map_id = $1",
        seeded["map_a"],
    )
    assert row_b_sees_a is None, "RLS LEAK: user B read user A's chat message"

    own_a = await conn_a.fetchrow(
        "SELECT id FROM chat_completion_messages WHERE map_id = $1",
        seeded["map_a"],
    )
    assert own_a is not None


@pytest.mark.postgres
async def test_pg_connections_select_isolation(conn_a, conn_b, seeded):
    """project_postgres_connections holds partner DB credentials — the most
    sensitive of the five tables. A leak here exposes a third party's DB."""
    row_a_sees_b = await conn_a.fetchrow(
        "SELECT id FROM project_postgres_connections WHERE id = $1",
        seeded["pgc_b"],
    )
    assert row_a_sees_b is None, (
        "CRITICAL RLS LEAK: user A read user B's postgres connection URI"
    )

    row_b_sees_a = await conn_b.fetchrow(
        "SELECT id FROM project_postgres_connections WHERE id = $1",
        seeded["pgc_a"],
    )
    assert row_b_sees_a is None, (
        "CRITICAL RLS LEAK: user B read user A's postgres connection URI"
    )

    own_a = await conn_a.fetchrow(
        "SELECT id FROM project_postgres_connections WHERE id = $1",
        seeded["pgc_a"],
    )
    assert own_a is not None


# ---------------------------------------------------------------------------
# Mutation isolation — UPDATE / DELETE against the other user's rows must
# silently no-op (zero rows affected), not raise.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_update_other_users_map_affects_zero_rows(conn_a, conn_admin, seeded):
    """User A's UPDATE against User B's map must mutate nothing. RLS filters
    the row out *before* the UPDATE matches — Postgres reports 'UPDATE 0'.
    """
    status = await conn_a.execute(
        "UPDATE user_mundiai_maps SET title = $1 WHERE id = $2",
        "PWNED-BY-USER-A", seeded["map_b"],
    )
    assert status == "UPDATE 0", f"expected UPDATE 0, got {status}"

    # Re-read via admin to confirm the title was NOT changed.
    title = await conn_admin.fetchval(
        "SELECT title FROM user_mundiai_maps WHERE id = $1", seeded["map_b"],
    )
    assert title != "PWNED-BY-USER-A", "RLS LEAK: user A mutated user B's map"


@pytest.mark.postgres
async def test_delete_other_users_layer_affects_zero_rows(conn_a, conn_admin, seeded):
    status = await conn_a.execute(
        "DELETE FROM map_layers WHERE layer_id = $1", seeded["layer_b"],
    )
    assert status == "DELETE 0", f"expected DELETE 0, got {status}"

    still_there = await conn_admin.fetchval(
        "SELECT layer_id FROM map_layers WHERE layer_id = $1",
        seeded["layer_b"],
    )
    assert still_there == seeded["layer_b"], (
        "RLS LEAK: user A deleted user B's layer"
    )


@pytest.mark.postgres
async def test_delete_other_users_pg_connection_affects_zero_rows(
    conn_a, conn_admin, seeded,
):
    """The credential-leak path. If this fails, partner B's DB URI is
    deletable (and presumably overwritable) from a partner A session."""
    status = await conn_a.execute(
        "DELETE FROM project_postgres_connections WHERE id = $1",
        seeded["pgc_b"],
    )
    assert status == "DELETE 0", f"expected DELETE 0, got {status}"

    still_there = await conn_admin.fetchval(
        "SELECT id FROM project_postgres_connections WHERE id = $1",
        seeded["pgc_b"],
    )
    assert still_there == seeded["pgc_b"], (
        "CRITICAL RLS LEAK: user A deleted user B's postgres connection"
    )


# ---------------------------------------------------------------------------
# Admin/migration bypass — empty GUC sees everything (by design)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_empty_guc_bypass_sees_both_users(conn_admin, seeded):
    """Empty app.user_id is the migration/admin escape hatch. It MUST see
    both users' rows or background jobs (alembic, cron) break.

    If you ever want to remove this bypass, you must first audit every
    long-lived connection that opens without setting app.user_id and either
    set it explicitly or use a dedicated admin role.
    """
    rows = await conn_admin.fetch(
        "SELECT id FROM user_mundiai_maps WHERE id = ANY($1::text[])",
        [seeded["map_a"], seeded["map_b"]],
    )
    seen = {r["id"] for r in rows}
    assert seeded["map_a"] in seen and seeded["map_b"] in seen, (
        "admin (empty GUC) lost visibility — the CASE WHEN bypass branch is "
        "broken. Migrations/cron will start failing."
    )


# ---------------------------------------------------------------------------
# Cross-table consistency — counts via two sessions must differ on tenant
# data and agree on tenant-isolated absences.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_count_only_own_tenant_data(conn_a, conn_b, seeded):
    """Concrete leak-detector: each user sees their own row but never the
    other tenant's, across all 5 tables in one shot."""
    tables_and_filters = [
        ("user_mundiai_maps", "id = ANY($1::text[])",
         [seeded["map_a"], seeded["map_b"]]),
        ("map_layers", "layer_id = ANY($1::text[])",
         [seeded["layer_a"], seeded["layer_b"]]),
        ("map_layer_styles", "map_id = ANY($1::text[])",
         [seeded["map_a"], seeded["map_b"]]),
        ("chat_completion_messages", "map_id = ANY($1::text[])",
         [seeded["map_a"], seeded["map_b"]]),
        ("project_postgres_connections", "id = ANY($1::text[])",
         [seeded["pgc_a"], seeded["pgc_b"]]),
    ]
    for table, where, params in tables_and_filters:
        n_a = await conn_a.fetchval(
            f"SELECT count(*) FROM {table} WHERE {where}", params,
        )
        n_b = await conn_b.fetchval(
            f"SELECT count(*) FROM {table} WHERE {where}", params,
        )
        assert n_a == 1, (
            f"{table}: user A saw {n_a} rows from both-user filter, expected 1 "
            f"(their own). A count of 2 means cross-tenant leak."
        )
        assert n_b == 1, (
            f"{table}: user B saw {n_b} rows from both-user filter, expected 1."
        )
