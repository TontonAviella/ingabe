"""Organization context resolution tests.

Validates P1-1 (organizations tables) and P1-2 (Clerk org → internal UUID
resolution). These tests hit the real database via asyncpg.
"""

import uuid

import asyncpg
import pytest
import pytest_asyncio

from src.database.pool import _build_postgres_url

pytestmark = pytest.mark.asyncio(loop_scope="module")

RUN_TAG = uuid.uuid4().hex[:8]
TEST_USER_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, f"clerk:test_org_{RUN_TAG}"))
TEST_CLERK_ORG = f"org_test_{RUN_TAG}"
TEST_ORG_SLUG = f"test-org-{RUN_TAG}"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db():
    from src.database.migrate import run_migrations

    await run_migrations()
    conn = await asyncpg.connect(_build_postgres_url())
    yield conn
    await conn.execute(
        "DELETE FROM user_organizations WHERE user_id = $1", TEST_USER_UUID
    )
    await conn.execute(
        "DELETE FROM organizations WHERE slug = $1", TEST_ORG_SLUG
    )
    await conn.execute(
        "DELETE FROM users WHERE internal_uuid = $1", TEST_USER_UUID
    )
    await conn.close()


@pytest.mark.postgres
async def test_create_organization(db):
    row = await db.fetchrow(
        """
        INSERT INTO organizations (name, slug, tier, clerk_org_id)
        VALUES ($1, $2, 'partner', $3)
        RETURNING id, name, slug, tier, clerk_org_id
        """,
        f"Test Org {RUN_TAG}",
        TEST_ORG_SLUG,
        TEST_CLERK_ORG,
    )
    assert row is not None
    assert row["slug"] == TEST_ORG_SLUG
    assert row["tier"] == "partner"
    assert row["clerk_org_id"] == TEST_CLERK_ORG
    assert row["id"] is not None


@pytest.mark.postgres
async def test_resolve_clerk_org_to_internal_uuid(db):
    """The core P1-2 lookup: Clerk org_id → internal UUID."""
    internal_id = await db.fetchval(
        "SELECT id FROM organizations WHERE clerk_org_id = $1",
        TEST_CLERK_ORG,
    )
    assert internal_id is not None

    missing = await db.fetchval(
        "SELECT id FROM organizations WHERE clerk_org_id = $1",
        "org_nonexistent",
    )
    assert missing is None


@pytest.mark.postgres
async def test_user_organization_membership(db):
    await db.execute(
        """
        INSERT INTO users (internal_uuid, clerk_id, email)
        VALUES ($1, $2, 'test@example.com')
        ON CONFLICT (clerk_id) DO NOTHING
        """,
        TEST_USER_UUID,
        f"clerk_test_{RUN_TAG}",
    )

    org_id = await db.fetchval(
        "SELECT id FROM organizations WHERE slug = $1", TEST_ORG_SLUG
    )

    await db.execute(
        """
        INSERT INTO user_organizations (user_id, org_id, role)
        VALUES ($1, $2, 'owner')
        """,
        TEST_USER_UUID,
        org_id,
    )

    membership = await db.fetchrow(
        """
        SELECT uo.role, o.name, o.slug
        FROM user_organizations uo
        JOIN organizations o ON o.id = uo.org_id
        WHERE uo.user_id = $1 AND uo.org_id = $2
        """,
        TEST_USER_UUID,
        org_id,
    )
    assert membership is not None
    assert membership["role"] == "owner"
    assert membership["slug"] == TEST_ORG_SLUG


@pytest.mark.postgres
async def test_tier_check_constraint(db):
    """Only partner/internal/trial allowed."""
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            """
            INSERT INTO organizations (name, slug, tier)
            VALUES ('bad', $1, 'enterprise')
            """,
            f"bad-tier-{RUN_TAG}",
        )


@pytest.mark.postgres
async def test_role_check_constraint(db):
    """Only owner/admin/member allowed."""
    org_id = await db.fetchval(
        "SELECT id FROM organizations WHERE slug = $1", TEST_ORG_SLUG
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            """
            INSERT INTO user_organizations (user_id, org_id, role)
            VALUES ($1, $2, 'superadmin')
            """,
            TEST_USER_UUID,
            org_id,
        )


@pytest.mark.postgres
async def test_slug_uniqueness(db):
    """Duplicate slug must fail."""
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            """
            INSERT INTO organizations (name, slug, tier)
            VALUES ('dupe', $1, 'partner')
            """,
            TEST_ORG_SLUG,
        )


@pytest.mark.postgres
async def test_cascade_delete_org_removes_memberships(db):
    """Deleting an org cascades to user_organizations."""
    temp_slug = f"cascade-test-{RUN_TAG}"
    org_id = await db.fetchval(
        """
        INSERT INTO organizations (name, slug, tier)
        VALUES ('Cascade Test', $1, 'trial')
        RETURNING id
        """,
        temp_slug,
    )
    await db.execute(
        """
        INSERT INTO user_organizations (user_id, org_id, role)
        VALUES ($1, $2, 'member')
        """,
        TEST_USER_UUID,
        org_id,
    )
    await db.execute("DELETE FROM organizations WHERE id = $1", org_id)
    remaining = await db.fetchval(
        "SELECT count(*) FROM user_organizations WHERE org_id = $1", org_id
    )
    assert remaining == 0
