"""T1 — partner isolation gate for Rwanda Brain ingestion.

CI-blocking. Until this test is green, partner_internal data stays behind a
feature flag disabled in prod, and no second partner onboards.

Covers the five assertions that gate onboarding of customer N:

  1. Partner A (app.partner_id = P_A) cannot read any page tagged
     access_scope='partner_internal' with partner_id = P_B.
  2. Partner B (app.partner_id = P_B) cannot read any page tagged
     access_scope='partner_internal' with partner_id = P_A.
  3. access_scope='public' pages are readable by both partners regardless of
     partner_id.
  4. app.role='admin' can read all partner_internal rows across partners.
  5. sage_query_log rows written during a partner-A session carry
     acting_partner_id = P_A; writing a row claiming a different partner_id
     from a non-admin session fails RLS.

The policy under test lives in:
    alembic/versions/d4e5f6a7b8c9_brain_ingestion_schema.py
        :: CREATE POLICY partner_isolation_brain_pages ON brain_pages

Style mirrors src/services/test_brain_service.py (asyncpg + set_config GUC).
"""

import uuid

import asyncpg
import pytest

from src.database.pool import _build_postgres_url


# Two synthetic partners. Each gets its own asyncpg connection with
# app.partner_id set at session start. Admin gets a third connection with
# app.role='admin'.
PARTNER_A = str(uuid.uuid4())
PARTNER_B = str(uuid.uuid4())

USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())
USER_ADMIN = str(uuid.uuid4())

# Unique slug prefix so parallel test runs don't collide.
RUN_TAG = uuid.uuid4().hex[:8]


# Postgres superusers (and BYPASSRLS roles) skip RLS unconditionally, even
# with FORCE ROW LEVEL SECURITY. CI connects as a superuser, so every
# connection must SET ROLE to a non-superuser role or the policies are
# effectively disabled. Mirrors src/test_rls_policies.py fixture.
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


async def _open(user_id: str, partner_id: str | None, role: str | None = None):
    url = _build_postgres_url()
    c = await asyncpg.connect(url)
    # Downgrade from superuser to a non-bypass role so RLS actually applies.
    await c.execute(f"SET ROLE {_RLS_ROLE}")
    await c.execute("SELECT set_config('app.user_id', $1, false)", user_id)
    await c.execute(
        "SELECT set_config('app.partner_id', $1, false)", partner_id or ""
    )
    await c.execute("SELECT set_config('app.role', $1, false)", role or "")
    return c


async def _seed_page(
    conn,
    slug: str,
    *,
    access_scope: str,
    partner_id: str | None,
    owner_uuid: str,
):
    """Insert via admin-bypass (no app.user_id) so seeding itself is not
    subject to RLS during setup. We briefly drop to worker context."""
    # Preserve and restore current GUCs.
    old_user = await conn.fetchval("SELECT current_setting('app.user_id', true)")
    old_partner = await conn.fetchval(
        "SELECT current_setting('app.partner_id', true)"
    )
    await conn.execute("SELECT set_config('app.user_id', '', false)")
    await conn.execute("SELECT set_config('app.partner_id', '', false)")
    try:
        await conn.execute(
            """
            INSERT INTO brain_pages
                (slug, type, title, compiled_truth,
                 owner_uuid, access_scope, partner_id)
            VALUES ($1, 'field', $2, $3, $4::uuid, $5, $6::uuid)
            ON CONFLICT (slug) DO UPDATE
                SET access_scope = EXCLUDED.access_scope,
                    partner_id   = EXCLUDED.partner_id
            """,
            slug,
            f"seed {slug}",
            f"seeded for isolation test {slug}",
            owner_uuid,
            access_scope,
            partner_id,
        )
    finally:
        await conn.execute(
            "SELECT set_config('app.user_id', $1, false)", old_user or ""
        )
        await conn.execute(
            "SELECT set_config('app.partner_id', $1, false)", old_partner or ""
        )


@pytest.fixture(scope="session")
async def seeded_db():
    """Ensure migrations run, then seed one page per scope/partner combo."""
    from src.database.migrate import run_migrations
    await run_migrations()
    await _ensure_rls_role()

    admin = await _open(USER_ADMIN, None, role="admin")

    slugs = {
        "a_internal": f"isoA-internal-{RUN_TAG}",
        "b_internal": f"isoB-internal-{RUN_TAG}",
        "public": f"iso-public-{RUN_TAG}",
        "mundi_only": f"iso-mundi-{RUN_TAG}",
    }
    await _seed_page(
        admin,
        slugs["a_internal"],
        access_scope="partner_internal",
        partner_id=PARTNER_A,
        owner_uuid=USER_A,
    )
    await _seed_page(
        admin,
        slugs["b_internal"],
        access_scope="partner_internal",
        partner_id=PARTNER_B,
        owner_uuid=USER_B,
    )
    await _seed_page(
        admin,
        slugs["public"],
        access_scope="public",
        partner_id=None,
        owner_uuid=USER_ADMIN,
    )
    await _seed_page(
        admin,
        slugs["mundi_only"],
        access_scope="mundi_only",
        partner_id=None,
        owner_uuid=USER_ADMIN,
    )

    yield slugs

    await admin.execute(
        "DELETE FROM brain_pages WHERE slug = ANY($1::text[])",
        list(slugs.values()),
    )
    await admin.close()


@pytest.fixture
async def conn_a(seeded_db):
    c = await _open(USER_A, PARTNER_A)
    yield c
    await c.close()


@pytest.fixture
async def conn_b(seeded_db):
    c = await _open(USER_B, PARTNER_B)
    yield c
    await c.close()


@pytest.fixture
async def conn_admin(seeded_db):
    c = await _open(USER_ADMIN, None, role="admin")
    yield c
    await c.close()


# ---------------------------------------------------------------------------
# Assertion 1 — Partner A cannot read Partner B's partner_internal data
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_partner_a_blocked_from_b_internal(conn_a, seeded_db):
    """A querying B's partner_internal page returns zero rows (RLS, not 403)."""
    row = await conn_a.fetchrow(
        "SELECT slug FROM brain_pages WHERE slug = $1",
        seeded_db["b_internal"],
    )
    assert row is None, (
        "RLS LEAK: partner A session read partner B's partner_internal row. "
        f"Slug: {seeded_db['b_internal']}. This is the exact failure mode the "
        "isolation policy exists to prevent."
    )


# ---------------------------------------------------------------------------
# Assertion 2 — Partner B cannot read Partner A's partner_internal data
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_partner_b_blocked_from_a_internal(conn_b, seeded_db):
    """Symmetric check — isolation must hold both directions."""
    row = await conn_b.fetchrow(
        "SELECT slug FROM brain_pages WHERE slug = $1",
        seeded_db["a_internal"],
    )
    assert row is None, (
        "RLS LEAK: partner B session read partner A's partner_internal row."
    )


# ---------------------------------------------------------------------------
# Assertion 3 — Public pages visible to every partner session
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_public_visible_to_all_partners(conn_a, conn_b, seeded_db):
    """access_scope='public' rows must be visible regardless of partner_id."""
    for conn, who in ((conn_a, "A"), (conn_b, "B")):
        row = await conn.fetchrow(
            "SELECT slug FROM brain_pages WHERE slug = $1",
            seeded_db["public"],
        )
        assert row is not None, (
            f"Partner {who} session failed to read a public page. Public "
            "knowledge must traverse every partner tenant."
        )


# ---------------------------------------------------------------------------
# Assertion 4 — Admin role bypasses partner scoping
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_admin_reads_all_partner_internal(conn_admin, seeded_db):
    """app.role='admin' must see every partner_internal row.

    Admin access is the break-glass for ops/debugging. Usage is logged via
    sage_query_log.acting_role — this test only verifies the RLS bypass, not
    the audit trail (that's a separate test).
    """
    rows = await conn_admin.fetch(
        """
        SELECT slug FROM brain_pages
        WHERE slug = ANY($1::text[])
        """,
        [seeded_db["a_internal"], seeded_db["b_internal"]],
    )
    seen = {r["slug"] for r in rows}
    assert seeded_db["a_internal"] in seen, "admin failed to read A's row"
    assert seeded_db["b_internal"] in seen, "admin failed to read B's row"


# ---------------------------------------------------------------------------
# Assertion 5 — sage_query_log carries the acting partner identity
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_sage_query_log_attribution(conn_a):
    """Any row written from partner A's session must record acting_partner_id
    = P_A. A non-admin session attempting to claim a different partner_id
    must be rejected by RLS.
    """
    query_uuid = str(uuid.uuid4())

    await conn_a.execute(
        """
        INSERT INTO sage_query_log
            (query_uuid, query_text, retrieved_page_ids,
             acting_user_uuid, acting_partner_id, acting_role, latency_ms)
        VALUES ($1::uuid, $2, $3::int[], $4::uuid, $5::uuid, $6, $7)
        """,
        query_uuid,
        "isolation test query",
        [],
        USER_A,
        PARTNER_A,
        "partner_user",
        12,
    )

    row = await conn_a.fetchrow(
        "SELECT acting_partner_id FROM sage_query_log WHERE query_uuid = $1::uuid",
        query_uuid,
    )
    assert row is not None
    assert str(row["acting_partner_id"]) == PARTNER_A, (
        "sage_query_log attribution mismatch — retrieval audit trail is the "
        "primary forensic record for partner isolation. Drift here means we "
        "can't prove to partner A that their queries weren't misattributed."
    )

    # Attempt to forge attribution — insert claiming partner B from A's
    # non-admin session. RLS INSERT policy on sage_query_log must block.
    forged_uuid = str(uuid.uuid4())
    with pytest.raises(asyncpg.PostgresError):
        await conn_a.execute(
            """
            INSERT INTO sage_query_log
                (query_uuid, query_text, retrieved_page_ids,
                 acting_user_uuid, acting_partner_id, acting_role, latency_ms)
            VALUES ($1::uuid, $2, $3::int[], $4::uuid, $5::uuid, $6, $7)
            """,
            forged_uuid,
            "forged",
            [],
            USER_A,
            PARTNER_B,  # attempting to impersonate partner B
            "partner_user",
            1,
        )


# ---------------------------------------------------------------------------
# Fuzz guard — 100 adversarial slug queries from partner A against B's data
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_adversarial_queries_never_leak(conn_a, seeded_db):
    """Run 100 varied read patterns from A's session looking for any leak of
    B's partner_internal row. Catches policy-bypass regressions introduced by
    future RLS edits (e.g. someone adds a permissive policy that ORs with
    partner isolation).
    """
    patterns = [
        "SELECT slug FROM brain_pages WHERE slug = $1",
        "SELECT slug FROM brain_pages WHERE slug ILIKE $1",
        "SELECT slug FROM brain_pages WHERE slug ~ $1",
        "SELECT slug FROM brain_pages WHERE title ILIKE '%' || $1 || '%'",
        "SELECT count(*) FROM brain_pages WHERE slug = $1",
        "SELECT slug FROM brain_pages WHERE partner_id::text = "
        "(SELECT partner_id::text FROM brain_pages WHERE slug = $1)",
    ]

    target = seeded_db["b_internal"]
    leaks = 0
    probes = 0
    for i in range(100):
        pat = patterns[i % len(patterns)]
        probes += 1
        rows = await conn_a.fetch(pat, target)
        # A leak is any row or count>0 referencing B's slug.
        for r in rows:
            vals = list(r.values())
            if any(v == target for v in vals):
                leaks += 1
            if any(isinstance(v, int) and v > 0 for v in vals) and "count" in pat:
                leaks += 1

    assert probes == 100
    assert leaks == 0, (
        f"{leaks}/100 adversarial queries leaked partner B's slug to partner "
        "A's session. RLS policy regression — block merge."
    )
