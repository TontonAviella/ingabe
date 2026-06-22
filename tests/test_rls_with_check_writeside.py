"""Write-side RLS isolation: WITH CHECK blocks cross-partner INSERT/UPDATE.

Migration under test: c1d2e3f4a5bc_rls_with_check_writeside.py

The gap closed here:

  c1d2e3f4a5b9 and c1d2e3f4a5bb created the tenant_isolation_* policies with
  USING only, no WITH CHECK. That gates row VISIBILITY (SELECT/DELETE/UPDATE
  row-find) but does NOT gate row PRODUCTION (INSERT, UPDATE-row-after-write).

  Concretely, before c1d2e3f4a5bc a partner A session could:

    INSERT INTO map_layers (layer_id, owner_uuid, name, type)
    VALUES ('spoof', '<partner_B_uuid>', 'rogue', 'vector');

  The row would land. RLS would hide it from A on subsequent reads — but
  partner B's session would see it (B's USING predicate matches owner_uuid =
  B's uuid). For BK Insurance, where rows on these tables drive payout
  calculations, a polluted namespace is a regulatory issue, not a UX issue.

  WITH CHECK enforces the same predicate on row data being written. An INSERT
  that would produce a row invisible to the current session is rejected with
  PostgresError:

    "new row violates row-level security policy for table ..."

We test eight policies. For each: an INSERT spoofing the other partner's
owner_uuid must raise; the legitimate INSERT (own uuid) must succeed and be
visible from the same session.

We also pin the bypass branch — admin (empty GUC) can write rows with any
owner_uuid. Without this, migrations / seed scripts / cron break.
"""

import json
import re
import uuid

import asyncpg
import pytest
import pytest_asyncio

from src.database.pool import _build_postgres_url

pytestmark = pytest.mark.asyncio(loop_scope="module")


USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())

RUN_TAG = uuid.uuid4().hex[:6]

_RLS_ROLE = "rls_test_role"
_SAFE_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _safe_rls_role() -> str:
    if not _SAFE_ROLE_RE.fullmatch(_RLS_ROLE):
        raise ValueError(f"Unsafe RLS role name: {_RLS_ROLE!r}")
    return _RLS_ROLE


async def _ensure_rls_role() -> None:
    url = _build_postgres_url()
    c = await asyncpg.connect(url)
    try:
        role = _safe_rls_role()
        exists = await c.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
        if not exists:
            await c.execute(f"CREATE ROLE {role} NOLOGIN")
        await c.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        await c.execute(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {role}")
        await c.execute(f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {role}")
    finally:
        await c.close()


async def _open(user_id: str | None):
    url = _build_postgres_url()
    c = await asyncpg.connect(url)
    bypasses = await c.fetchval(
        "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    if bypasses:
        await c.execute(f"SET ROLE {_safe_rls_role()}")
    await c.execute("SELECT set_config('app.user_id', $1, false)", user_id or "")
    return c


def _id(prefix: str) -> str:
    return f"{prefix}{RUN_TAG}{uuid.uuid4().hex[:2]}"[:12]


def _slug(prefix: str) -> str:
    return f"{prefix}-{RUN_TAG}-{uuid.uuid4().hex[:6]}"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def seeded():
    """Seed parent rows for A and B as admin so write-side tests have something
    to spoof against."""
    from src.database.migrate import run_migrations
    await run_migrations()
    await _ensure_rls_role()

    admin = await _open(None)

    ids: dict = {
        "proj_a": _id("pA"),
        "proj_b": _id("pB"),
        "map_a":  _id("mA"),
        "map_b":  _id("mB"),
        "page_a_slug": _slug("pgA"),
        "page_b_slug": _slug("pgB"),
        "page_a": None,  # filled in below — brain_pages.id is integer
        "page_b": None,
        "entity_id": None,  # brain_entities.id — for brain_entity_refs FK
    }

    for proj_id, owner in (
        (ids["proj_a"], USER_A),
        (ids["proj_b"], USER_B),
    ):
        await admin.execute(
            """
            INSERT INTO user_mundiai_projects (id, owner_uuid, editor_uuids, viewer_uuids, title)
            VALUES ($1, $2::uuid, ARRAY[]::uuid[], ARRAY[]::uuid[], $3)
            """,
            proj_id, owner, f"wcheck project {proj_id}",
        )

    for map_id, proj_id, owner in (
        (ids["map_a"], ids["proj_a"], USER_A),
        (ids["map_b"], ids["proj_b"], USER_B),
    ):
        await admin.execute(
            """
            INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title)
            VALUES ($1, $2, $3::uuid, $4)
            """,
            map_id, proj_id, owner, f"wcheck map {map_id}",
        )

    # brain_pages.id is INTEGER serial; capture it for entity_refs FK.
    ids["page_a"] = await admin.fetchval(
        """
        INSERT INTO brain_pages (slug, type, title, owner_uuid, viewer_uuids, editor_uuids)
        VALUES ($1, 'note', $2, $3::uuid, ARRAY[]::uuid[], ARRAY[]::uuid[])
        RETURNING id
        """,
        ids["page_a_slug"], f"wcheck page A {RUN_TAG}", USER_A,
    )
    ids["page_b"] = await admin.fetchval(
        """
        INSERT INTO brain_pages (slug, type, title, owner_uuid, viewer_uuids, editor_uuids)
        VALUES ($1, 'note', $2, $3::uuid, ARRAY[]::uuid[], ARRAY[]::uuid[])
        RETURNING id
        """,
        ids["page_b_slug"], f"wcheck page B {RUN_TAG}", USER_B,
    )

    # brain_entity_refs requires entity_id FK to brain_entities; seed one entity
    # so the spoof test can produce a referentially-valid row that should still
    # be rejected by RLS WITH CHECK rather than by FK violation.
    ids["entity_id"] = await admin.fetchval(
        """
        INSERT INTO brain_entities (canonical_name, entity_type)
        VALUES ($1, 'admin_area')
        RETURNING id
        """,
        f"wcheck-entity-{RUN_TAG}",
    )

    yield ids

    await admin.execute(
        "DELETE FROM brain_entities WHERE id = $1", ids["entity_id"]
    )
    await admin.execute(
        "DELETE FROM brain_pages WHERE id = ANY($1::int[])",
        [ids["page_a"], ids["page_b"]],
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
async def conn_admin(seeded):
    c = await _open(None)
    yield c
    await c.close()


# ---------------------------------------------------------------------------
# user_mundiai_maps — spoof owner_uuid
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_maps_insert_spoof_owner_rejected(conn_a, seeded):
    """User A tries to write a map attributed to User B — must raise."""
    rogue_id = _id("rg")
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await conn_a.execute(
            """
            INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title)
            VALUES ($1, $2, $3::uuid, $4)
            """,
            rogue_id, seeded["proj_b"], USER_B, "pwned-by-A",
        )


@pytest.mark.postgres
async def test_maps_insert_own_owner_succeeds(conn_a, seeded, conn_admin):
    rogue_id = _id("ok")
    try:
        await conn_a.execute(
            """
            INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title)
            VALUES ($1, $2, $3::uuid, $4)
            """,
            rogue_id, seeded["proj_a"], USER_A, "legit-by-A",
        )
        # visible to A
        row = await conn_a.fetchrow(
            "SELECT id FROM user_mundiai_maps WHERE id = $1", rogue_id
        )
        assert row is not None
    finally:
        await conn_admin.execute(
            "DELETE FROM user_mundiai_maps WHERE id = $1", rogue_id
        )


# ---------------------------------------------------------------------------
# map_layers — spoof owner_uuid
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_map_layers_insert_spoof_owner_rejected(conn_a):
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await conn_a.execute(
            """
            INSERT INTO map_layers (layer_id, owner_uuid, name, type)
            VALUES ($1, $2::uuid, $3, 'vector')
            """,
            _id("lr"), USER_B, "rogue-layer",
        )


# ---------------------------------------------------------------------------
# map_layer_styles — spoof via map_id pointing at another partner's map
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_map_layer_styles_insert_other_map_rejected(conn_a, seeded, conn_admin):
    """User A creates a real layer + style (as admin to dodge FKs), then tries
    to attach the style to User B's map. WITH CHECK should reject because
    map_id ∉ A's maps."""
    layer_id = _id("la")
    style_id = _id("sa")
    try:
        await conn_admin.execute(
            """
            INSERT INTO map_layers (layer_id, owner_uuid, name, type)
            VALUES ($1, $2::uuid, $3, 'vector')
            """,
            layer_id, USER_A, "wcheck layer",
        )
        await conn_admin.execute(
            """
            INSERT INTO layer_styles (style_id, layer_id, style_json, created_by)
            VALUES ($1, $2, '{}'::jsonb, $3::uuid)
            """,
            style_id, layer_id, USER_A,
        )

        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn_a.execute(
                """
                INSERT INTO map_layer_styles (map_id, layer_id, style_id)
                VALUES ($1, $2, $3)
                """,
                seeded["map_b"], layer_id, style_id,
            )
    finally:
        await conn_admin.execute(
            "DELETE FROM map_layer_styles WHERE layer_id = $1", layer_id
        )
        await conn_admin.execute(
            "DELETE FROM layer_styles WHERE style_id = $1", style_id
        )
        await conn_admin.execute(
            "DELETE FROM map_layers WHERE layer_id = $1", layer_id
        )


# ---------------------------------------------------------------------------
# chat_completion_messages — spoof via map_id
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_chat_messages_insert_other_map_rejected(conn_a, seeded):
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await conn_a.execute(
            """
            INSERT INTO chat_completion_messages
                (map_id, sender_id, message_json)
            VALUES ($1, $2::uuid, $3::jsonb)
            """,
            seeded["map_b"], USER_A,
            '{"role": "user", "content": "pwn"}',
        )


# ---------------------------------------------------------------------------
# project_postgres_connections — spoof user_id (credential pollution path)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_pg_connections_insert_spoof_user_rejected(conn_a, seeded):
    """The credential-injection path. If WITH CHECK is missing here, partner A
    can plant a malicious connection URI into partner B's namespace, which B's
    UI would then offer as a saved connection."""
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await conn_a.execute(
            """
            INSERT INTO project_postgres_connections
                (id, project_id, user_id, connection_uri, connection_name)
            VALUES ($1, $2, $3::uuid, $4, $5)
            """,
            _id("rc"), seeded["proj_b"], USER_B,
            "postgresql://attacker:pwn@evil.example/db",
            "rogue-conn-A-to-B",
        )


# ---------------------------------------------------------------------------
# UPDATE that would re-attribute a row to another partner — also blocked
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_maps_update_moving_to_other_project_rejected(conn_a, seeded, conn_admin):
    """A owns map_a (in proj_a). A attempts to UPDATE the map's project_id to
    proj_b AND owner_uuid to B — i.e. fully transfer the row out of A's reach.

    The maps policy lets project owners manage any map in their project, so
    flipping ONLY owner_uuid while project_id stays = proj_a is legitimate
    (A still controls the row via project ownership). The real write-side
    breach we're closing is moving the row OUT of A's namespace entirely.
    WITH CHECK must reject this because the resulting row satisfies neither
    branch (owner_uuid != A AND project_id is a project A doesn't own).

    (Without WITH CHECK, this UPDATE would succeed and silently transfer the
    row to B's namespace, since RLS USING only filters which rows match the
    WHERE clause — it doesn't constrain what the row becomes after the write.)
    """
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await conn_a.execute(
            """
            UPDATE user_mundiai_maps
               SET owner_uuid = $1::uuid, project_id = $2
             WHERE id = $3
            """,
            USER_B, seeded["proj_b"], seeded["map_a"],
        )

    # confirm map_a is still owned by A and still in proj_a
    row = await conn_admin.fetchrow(
        "SELECT owner_uuid::text AS o, project_id AS p FROM user_mundiai_maps WHERE id = $1",
        seeded["map_a"],
    )
    assert row["o"] == USER_A, "WITH CHECK didn't actually block the ownership flip"
    assert row["p"] == seeded["proj_a"], "WITH CHECK didn't actually block the project flip"


# ---------------------------------------------------------------------------
# brain_pending_hooks — spoof owner_uuid
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_brain_pending_hooks_insert_spoof_owner_rejected(conn_a):
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await conn_a.execute(
            """
            INSERT INTO brain_pending_hooks (hook_type, payload, owner_uuid)
            VALUES ($1, $2::jsonb, $3::uuid)
            """,
            "rogue-hook", json.dumps({"event": "rogue"}), USER_B,
        )


# ---------------------------------------------------------------------------
# brain_ingest_log — spoof owner_uuid
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_brain_ingest_log_insert_spoof_owner_rejected(conn_a):
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await conn_a.execute(
            """
            INSERT INTO brain_ingest_log (source_type, source_ref, owner_uuid)
            VALUES ($1, $2, $3::uuid)
            """,
            "rogue-source", "rogue-ref", USER_B,
        )


# ---------------------------------------------------------------------------
# brain_entity_refs — spoof page_id pointing at another partner's page
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_brain_entity_refs_insert_other_page_rejected(conn_a, seeded):
    """RESTRICTIVE policy: refs to a page A doesn't own/edit/view must reject.

    The brain_entity_refs row is referentially valid (entity_id is a real
    brain_entities row, page_id is a real brain_pages row) — the rejection is
    pure RLS WITH CHECK on the RESTRICTIVE policy.
    """
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await conn_a.execute(
            """
            INSERT INTO brain_entity_refs (entity_id, page_id, mention_text)
            VALUES ($1, $2, $3)
            """,
            seeded["entity_id"], seeded["page_b"], "pwn-mention",
        )


# ---------------------------------------------------------------------------
# Admin bypass — empty GUC can write rows with any owner_uuid
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_admin_can_write_any_owner(conn_admin, seeded):
    """Migrations, seeders, and cron run under empty app.user_id GUC. They
    must be able to write rows on behalf of any user, otherwise alembic
    upgrades stop working the moment they touch RLS-protected tables."""
    rogue_id = _id("ad")
    try:
        await conn_admin.execute(
            """
            INSERT INTO map_layers (layer_id, owner_uuid, name, type)
            VALUES ($1, $2::uuid, $3, 'vector')
            """,
            rogue_id, USER_B, "admin-wrote-for-B",
        )
        owner = await conn_admin.fetchval(
            "SELECT owner_uuid::text FROM map_layers WHERE layer_id = $1",
            rogue_id,
        )
        assert owner == USER_B
    finally:
        await conn_admin.execute(
            "DELETE FROM map_layers WHERE layer_id = $1", rogue_id
        )
