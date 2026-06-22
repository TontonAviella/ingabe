"""Cross-user RLS isolation for brain_service write-side tables.

Migration under test: c1d2e3f4a5bb_brain_writeside_isolation.py

Closes the three brain_service write-side exposures surfaced in the pre-deploy
audit (see migration docstring):

  1. brain_pending_hooks had `USING true` (no-op).
     Payloads carry layer metadata — cross-partner read leaks paths/map_ids.
  2. brain_ingest_log had `USING true` (no-op).
     Cross-partner reads leak which partners are ingesting what.
  3. brain_entity_refs.tenant_isolation was `page_id IN (SELECT id FROM brain_pages)`
     with no owner filter. Any authenticated user could see references to any
     other user's pages.

Six assertions across the three tables:

  1. User A's brain_pending_hooks INSERT auto-populates owner_uuid=A; user B
     cannot SELECT it.
  2. User A's brain_ingest_log INSERT auto-populates owner_uuid=A; user B
     cannot SELECT it.
  3. User A's brain_entity_refs (page owned by A) is invisible to user B.
     This is the RESTRICTIVE policy — partner_isolation is PERMISSIVE and
     would otherwise OR-grant via access_scope=NULL.
  4. Mutation isolation: User A's UPDATE/DELETE against user B's brain_pending_hooks
     row affects zero rows.
  5. Admin (empty GUC) sees all rows — preserves migration/cron bypass.
  6. Legacy NULL-owner rows are only visible to admin (empty GUC), never to
     user A or user B.

Why this matters: brain_service queues background ingestion via these tables.
Without isolation, a partner's queued hook payload (e.g., a layer S3 path) is
readable by any other authenticated user.
"""

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


async def _ensure_rls_role() -> None:
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
    url = _build_postgres_url()
    c = await asyncpg.connect(url)
    bypasses = await c.fetchval(
        "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    if bypasses:
        await c.execute(f"SET ROLE {_RLS_ROLE}")
    await c.execute("SELECT set_config('app.user_id', $1, false)", user_id or "")
    return c


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def seeded():
    """Seed two parallel users' brain rows via admin (empty GUC).

    Each user gets: brain_pages row (owner_uuid=them) + brain_entities row
    (shared, access_scope=NULL) + brain_entity_refs row joining them +
    brain_pending_hooks row + brain_ingest_log row. Plus a legacy NULL-owner
    row in each of pending_hooks / ingest_log to lock the empty-default path.
    """
    from src.database.migrate import run_migrations
    await run_migrations()
    await _ensure_rls_role()

    admin = await _open(None)

    # ---- brain_pages: one per user, owner-scoped --------------------------
    slug_a = f"rls-iso-pg-a-{RUN_TAG}"
    slug_b = f"rls-iso-pg-b-{RUN_TAG}"
    page_a = await admin.fetchval(
        """
        INSERT INTO brain_pages (slug, type, title, owner_uuid)
        VALUES ($1, 'note', 'A page', $2::uuid)
        RETURNING id
        """,
        slug_a, USER_A,
    )
    page_b = await admin.fetchval(
        """
        INSERT INTO brain_pages (slug, type, title, owner_uuid)
        VALUES ($1, 'note', 'B page', $2::uuid)
        RETURNING id
        """,
        slug_b, USER_B,
    )

    # ---- brain_entities: one shared (public scope so both can ref it) -----
    ent_id = await admin.fetchval(
        """
        INSERT INTO brain_entities (canonical_name, entity_type, access_scope)
        VALUES ($1, 'institution', 'public')
        RETURNING id
        """,
        f"rls-iso-ent-{RUN_TAG}",
    )

    # ---- brain_entity_refs: one per page (so each user owns one ref) ------
    ref_a = await admin.fetchval(
        """
        INSERT INTO brain_entity_refs (entity_id, page_id, mention_text)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        ent_id, page_a, f"mention-a-{RUN_TAG}",
    )
    ref_b = await admin.fetchval(
        """
        INSERT INTO brain_entity_refs (entity_id, page_id, mention_text)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        ent_id, page_b, f"mention-b-{RUN_TAG}",
    )

    # ---- brain_pending_hooks: user A and user B + a legacy NULL row -------
    # Use connections with the user's GUC so the DEFAULT expression populates
    # owner_uuid. This is the "real" code path the migration is protecting.
    conn_a = await _open(USER_A)
    conn_b = await _open(USER_B)
    try:
        hook_a = await conn_a.fetchval(
            """
            INSERT INTO brain_pending_hooks (hook_type, payload)
            VALUES ('test-a', '{"tag": "a"}'::jsonb)
            RETURNING id
            """,
        )
        hook_b = await conn_b.fetchval(
            """
            INSERT INTO brain_pending_hooks (hook_type, payload)
            VALUES ('test-b', '{"tag": "b"}'::jsonb)
            RETURNING id
            """,
        )

        log_a = await conn_a.fetchval(
            """
            INSERT INTO brain_ingest_log (source_type, source_ref, summary)
            VALUES ('test', $1, 'A log')
            RETURNING id
            """,
            f"ref-a-{RUN_TAG}",
        )
        log_b = await conn_b.fetchval(
            """
            INSERT INTO brain_ingest_log (source_type, source_ref, summary)
            VALUES ('test', $1, 'B log')
            RETURNING id
            """,
            f"ref-b-{RUN_TAG}",
        )
    finally:
        await conn_a.close()
        await conn_b.close()

    # ---- Legacy NULL-owner rows (pre-migration data) ---------------------
    # Inserted via admin (empty GUC) → DEFAULT yields NULL.
    legacy_hook = await admin.fetchval(
        """
        INSERT INTO brain_pending_hooks (hook_type, payload, owner_uuid)
        VALUES ('test-legacy', $1::jsonb, NULL)
        RETURNING id
        """,
        '{"tag": "legacy"}',
    )
    legacy_log = await admin.fetchval(
        """
        INSERT INTO brain_ingest_log (source_type, source_ref, summary, owner_uuid)
        VALUES ('test', $1, 'legacy', NULL)
        RETURNING id
        """,
        f"ref-legacy-{RUN_TAG}",
    )

    ids = {
        "page_a": page_a, "page_b": page_b,
        "ent": ent_id,
        "ref_a": ref_a, "ref_b": ref_b,
        "hook_a": hook_a, "hook_b": hook_b,
        "log_a": log_a, "log_b": log_b,
        "legacy_hook": legacy_hook, "legacy_log": legacy_log,
    }

    # ---- Verify the DEFAULT populated owner_uuid (sanity) ----------------
    populated_a = await admin.fetchval(
        "SELECT owner_uuid::text FROM brain_pending_hooks WHERE id = $1", hook_a,
    )
    assert populated_a == USER_A, (
        f"DEFAULT NULLIF(current_setting...) failed: hook_a owner={populated_a} "
        f"expected={USER_A}. The DEFAULT expression isn't firing — every "
        f"isolation test downstream is meaningless without this."
    )

    yield ids

    # ---- Teardown -------------------------------------------------------
    await admin.execute(
        "DELETE FROM brain_entity_refs WHERE id = ANY($1::int[])",
        [ref_a, ref_b],
    )
    await admin.execute(
        "DELETE FROM brain_entities WHERE id = $1", ent_id,
    )
    await admin.execute(
        "DELETE FROM brain_pages WHERE id = ANY($1::int[])",
        [page_a, page_b],
    )
    await admin.execute(
        "DELETE FROM brain_pending_hooks WHERE id = ANY($1::int[])",
        [hook_a, hook_b, legacy_hook],
    )
    await admin.execute(
        "DELETE FROM brain_ingest_log WHERE id = ANY($1::int[])",
        [log_a, log_b, legacy_log],
    )
    await admin.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def conn_a(seeded):
    c = await _open(USER_A)
    yield c
    await c.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def conn_b(seeded):
    c = await _open(USER_B)
    yield c
    await c.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def conn_admin(seeded):
    c = await _open(None)
    yield c
    await c.close()


# ---------------------------------------------------------------------------
# SELECT isolation
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_pending_hooks_select_isolation(conn_a, conn_b, seeded):
    row_a_sees_b = await conn_a.fetchrow(
        "SELECT id FROM brain_pending_hooks WHERE id = $1", seeded["hook_b"],
    )
    assert row_a_sees_b is None, (
        "RLS LEAK: user A read user B's brain_pending_hooks row "
        "(payload would leak layer paths / map_ids)"
    )

    row_b_sees_a = await conn_b.fetchrow(
        "SELECT id FROM brain_pending_hooks WHERE id = $1", seeded["hook_a"],
    )
    assert row_b_sees_a is None

    own = await conn_a.fetchrow(
        "SELECT id FROM brain_pending_hooks WHERE id = $1", seeded["hook_a"],
    )
    assert own is not None, "user A can't read their own hook — DEFAULT or policy broken"


@pytest.mark.postgres
async def test_ingest_log_select_isolation(conn_a, conn_b, seeded):
    row_a_sees_b = await conn_a.fetchrow(
        "SELECT id FROM brain_ingest_log WHERE id = $1", seeded["log_b"],
    )
    assert row_a_sees_b is None, (
        "RLS LEAK: user A read user B's brain_ingest_log row "
        "(leaks which partners are ingesting what)"
    )

    row_b_sees_a = await conn_b.fetchrow(
        "SELECT id FROM brain_ingest_log WHERE id = $1", seeded["log_a"],
    )
    assert row_b_sees_a is None

    own = await conn_a.fetchrow(
        "SELECT id FROM brain_ingest_log WHERE id = $1", seeded["log_a"],
    )
    assert own is not None


@pytest.mark.postgres
async def test_entity_refs_select_isolation(conn_a, conn_b, seeded):
    """RESTRICTIVE policy test: partner_isolation_brain_entity_refs is
    PERMISSIVE and grants access when access_scope IS NULL. Without the
    RESTRICTIVE tenant_isolation policy AND'd in, user B would see user A's
    refs to user A's private pages."""
    row_a_sees_b = await conn_a.fetchrow(
        "SELECT id FROM brain_entity_refs WHERE id = $1", seeded["ref_b"],
    )
    assert row_a_sees_b is None, (
        "RLS LEAK: user A read user B's brain_entity_refs row. The "
        "tenant_isolation policy is probably PERMISSIVE again — it must be "
        "RESTRICTIVE to AND with the access_scope-based partner_isolation."
    )

    row_b_sees_a = await conn_b.fetchrow(
        "SELECT id FROM brain_entity_refs WHERE id = $1", seeded["ref_a"],
    )
    assert row_b_sees_a is None

    own = await conn_a.fetchrow(
        "SELECT id FROM brain_entity_refs WHERE id = $1", seeded["ref_a"],
    )
    assert own is not None


# ---------------------------------------------------------------------------
# Mutation isolation
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_update_other_users_pending_hook_affects_zero_rows(
    conn_a, conn_admin, seeded,
):
    status = await conn_a.execute(
        "UPDATE brain_pending_hooks SET hook_type = $1 WHERE id = $2",
        "PWNED-BY-A", seeded["hook_b"],
    )
    assert status == "UPDATE 0", f"expected UPDATE 0, got {status}"

    hook_type = await conn_admin.fetchval(
        "SELECT hook_type FROM brain_pending_hooks WHERE id = $1",
        seeded["hook_b"],
    )
    assert hook_type != "PWNED-BY-A", (
        "RLS LEAK: user A mutated user B's brain_pending_hooks row"
    )


@pytest.mark.postgres
async def test_delete_other_users_ingest_log_affects_zero_rows(
    conn_a, conn_admin, seeded,
):
    status = await conn_a.execute(
        "DELETE FROM brain_ingest_log WHERE id = $1", seeded["log_b"],
    )
    assert status == "DELETE 0", f"expected DELETE 0, got {status}"

    still_there = await conn_admin.fetchval(
        "SELECT id FROM brain_ingest_log WHERE id = $1", seeded["log_b"],
    )
    assert still_there == seeded["log_b"], (
        "RLS LEAK: user A deleted user B's brain_ingest_log row"
    )


# ---------------------------------------------------------------------------
# Admin bypass — empty GUC sees everything
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_admin_empty_guc_sees_all_brain_writeside(conn_admin, seeded):
    """Empty app.user_id is the migration/cron bypass. brain_service's
    background workers (queue drain, ingest hooks) run with empty GUC too,
    so this is load-bearing for ops."""
    hooks = await conn_admin.fetch(
        "SELECT id FROM brain_pending_hooks WHERE id = ANY($1::int[])",
        [seeded["hook_a"], seeded["hook_b"], seeded["legacy_hook"]],
    )
    seen = {r["id"] for r in hooks}
    assert seen == {seeded["hook_a"], seeded["hook_b"], seeded["legacy_hook"]}, (
        f"admin (empty GUC) lost visibility on brain_pending_hooks: saw {seen}"
    )

    logs = await conn_admin.fetch(
        "SELECT id FROM brain_ingest_log WHERE id = ANY($1::int[])",
        [seeded["log_a"], seeded["log_b"], seeded["legacy_log"]],
    )
    seen_logs = {r["id"] for r in logs}
    assert seen_logs == {seeded["log_a"], seeded["log_b"], seeded["legacy_log"]}


# ---------------------------------------------------------------------------
# Legacy NULL-owner rows — only admin sees them
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_legacy_null_owner_invisible_to_users(conn_a, conn_b, seeded):
    """Pre-migration rows have owner_uuid IS NULL. The policy compares
    `owner_uuid::text = current_setting(...)` — NULL::text is NULL, so the
    comparison yields NULL (falsy under WHERE). Net effect: legacy rows are
    quarantined to admin context, which is the right safety default."""
    legacy_a = await conn_a.fetchrow(
        "SELECT id FROM brain_pending_hooks WHERE id = $1",
        seeded["legacy_hook"],
    )
    assert legacy_a is None, (
        "legacy NULL-owner brain_pending_hooks visible to user A — every "
        "pre-migration row would be world-readable"
    )

    legacy_b = await conn_b.fetchrow(
        "SELECT id FROM brain_ingest_log WHERE id = $1", seeded["legacy_log"],
    )
    assert legacy_b is None


# ---------------------------------------------------------------------------
# Cross-table count check
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_brain_writeside_count_isolation(conn_a, conn_b, seeded):
    """Each user sees their own row but never the other's, across all three
    brain write-side tables."""
    pairs = [
        ("brain_pending_hooks", "id = ANY($1::int[])",
         [seeded["hook_a"], seeded["hook_b"]]),
        ("brain_ingest_log", "id = ANY($1::int[])",
         [seeded["log_a"], seeded["log_b"]]),
        ("brain_entity_refs", "id = ANY($1::int[])",
         [seeded["ref_a"], seeded["ref_b"]]),
    ]
    for table, where, params in pairs:
        n_a = await conn_a.fetchval(
            f"SELECT count(*) FROM {table} WHERE {where}", params,
        )
        n_b = await conn_b.fetchval(
            f"SELECT count(*) FROM {table} WHERE {where}", params,
        )
        assert n_a == 1, (
            f"{table}: user A saw {n_a} rows, expected 1 (their own). "
            f"2 = cross-tenant leak."
        )
        assert n_b == 1, f"{table}: user B saw {n_b} rows, expected 1."
