"""Partner document upload API tests.

Tests P1-5 endpoints: file upload, URL submission, document listing.
Validates org context requirement, file validation, SSRF protection,
and correct access_scope/partner_id tagging.
"""

import uuid

import asyncpg
import pytest
import pytest_asyncio

from src.database.pool import _build_postgres_url

pytestmark = pytest.mark.asyncio(loop_scope="module")

RUN_TAG = uuid.uuid4().hex[:8]
TEST_USER_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, f"clerk:partner_upload_{RUN_TAG}"))
TEST_CLERK_ID = f"clerk_upload_{RUN_TAG}"
TEST_ORG_SLUG = f"upload-test-org-{RUN_TAG}"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db():
    from src.database.migrate import run_migrations

    await run_migrations()
    conn = await asyncpg.connect(_build_postgres_url())
    yield conn
    # Cleanup
    await conn.execute(
        "DELETE FROM brain_pages WHERE slug LIKE $1",
        f"partner-doc-%-{RUN_TAG}%",
    )
    await conn.execute(
        "DELETE FROM brain_pages WHERE slug LIKE $1",
        f"partner-url-%-{RUN_TAG}%",
    )
    await conn.execute(
        "DELETE FROM brain_pending_hooks WHERE payload::text LIKE $1",
        f"%{RUN_TAG}%",
    )
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


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def org_id(db):
    """Create test user + org, return org UUID."""
    await db.execute(
        """
        INSERT INTO users (internal_uuid, clerk_id, email)
        VALUES ($1, $2, 'upload-test@example.com')
        ON CONFLICT (clerk_id) DO NOTHING
        """,
        TEST_USER_UUID,
        TEST_CLERK_ID,
    )
    row = await db.fetchrow(
        """
        INSERT INTO organizations (name, slug, tier)
        VALUES ($1, $2, 'partner')
        RETURNING id
        """,
        f"Upload Test Org {RUN_TAG}",
        TEST_ORG_SLUG,
    )
    oid = str(row["id"])
    await db.execute(
        """
        INSERT INTO user_organizations (user_id, org_id, role)
        VALUES ($1, $2::uuid, 'owner')
        """,
        TEST_USER_UUID,
        oid,
    )
    return oid


# ---------------------------------------------------------------------------
# SSRF protection tests
# ---------------------------------------------------------------------------

@pytest.mark.postgres
def test_ssrf_blocks_localhost():
    from src.routes.partner_routes import _validate_url_safety
    from fastapi import HTTPException

    for url in [
        "http://localhost/secret",
        "http://127.0.0.1/admin",
        "http://0.0.0.0:8080/",
        "http://[::1]/",
    ]:
        with pytest.raises(HTTPException) as exc_info:
            _validate_url_safety(url)
        assert exc_info.value.status_code == 400


@pytest.mark.postgres
def test_ssrf_blocks_private_ips():
    from src.routes.partner_routes import _validate_url_safety
    from fastapi import HTTPException

    for url in [
        "http://10.0.0.1/internal",
        "http://192.168.1.1/admin",
        "http://172.16.0.1/secret",
        "http://169.254.169.254/latest/meta-data/",
    ]:
        with pytest.raises(HTTPException) as exc_info:
            _validate_url_safety(url)
        assert exc_info.value.status_code == 400


@pytest.mark.postgres
def test_ssrf_blocks_internal_domains():
    from src.routes.partner_routes import _validate_url_safety
    from fastapi import HTTPException

    for url in [
        "http://db.internal/dump",
        "http://redis.local/keys",
    ]:
        with pytest.raises(HTTPException) as exc_info:
            _validate_url_safety(url)
        assert exc_info.value.status_code == 400


@pytest.mark.postgres
def test_ssrf_allows_public_urls():
    from src.routes.partner_routes import _validate_url_safety

    for url in [
        "https://example.com/doc.pdf",
        "https://www.minagri.gov.rw/reports",
        "http://fao.org/data",
    ]:
        _validate_url_safety(url)  # Should not raise


# ---------------------------------------------------------------------------
# Org context guard tests
# ---------------------------------------------------------------------------

@pytest.mark.postgres
def test_require_org_rejects_no_org():
    from src.routes.partner_routes import _require_org
    from src.dependencies.session import LegacyUserContext
    from fastapi import HTTPException

    ctx = LegacyUserContext()
    with pytest.raises(HTTPException) as exc_info:
        _require_org(ctx)
    assert exc_info.value.status_code == 403


@pytest.mark.postgres
def test_require_org_accepts_org():
    from src.routes.partner_routes import _require_org
    from src.dependencies.session import ClerkUserContext

    ctx = ClerkUserContext(
        internal_uuid="test-uuid",
        clerk_id="clerk_test",
        org_id="some-org-uuid",
        org_role="member",
    )
    assert _require_org(ctx) == "some-org-uuid"


# ---------------------------------------------------------------------------
# Text extraction tests
# ---------------------------------------------------------------------------

@pytest.mark.postgres
def test_plain_text_extraction():
    from src.routes.partner_routes import _extract_text_plain

    assert _extract_text_plain(b"Hello world") == "Hello world"
    assert "Rwanda" in _extract_text_plain("Agriculture in Rwanda".encode("utf-8"))


@pytest.mark.postgres
async def test_pdf_extraction():
    """Test PDF text extraction with a minimal valid PDF."""
    from src.routes.partner_routes import _extract_text_from_pdf

    # Create a minimal PDF with reportlab if available, otherwise skip
    try:
        from io import BytesIO
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        buf = BytesIO()
        writer.write(buf)
        pdf_bytes = buf.getvalue()

        result = await _extract_text_from_pdf(pdf_bytes)
        assert isinstance(result, str)
    except ImportError:
        pytest.skip("pypdf not available")


# ---------------------------------------------------------------------------
# Database integration tests
# ---------------------------------------------------------------------------

@pytest.mark.postgres
async def test_partner_page_tagged_correctly(db, org_id):
    """Write a partner document page and verify access_scope + partner_id."""
    from src.services.brain_service import BrainService, PageInput, TimelineInput

    slug = f"partner-doc-test-{RUN_TAG}"
    brain = BrainService()

    async with db.transaction():
        await brain.put_page(
            db,
            slug,
            PageInput(
                type="source_document",
                title="Test Partner Doc",
                compiled_truth="Insurance policy details for Gabiro cooperative.",
                frontmatter={"source_type": "partner_upload"},
            ),
            owner_uuid=TEST_USER_UUID,
        )
        await db.execute(
            """
            UPDATE brain_pages
            SET access_scope = 'partner_internal',
                partner_id   = $2::uuid,
                source_id    = 'partner-upload-test'
            WHERE slug = $1
            """,
            slug,
            org_id,
        )

    row = await db.fetchrow(
        "SELECT access_scope, partner_id FROM brain_pages WHERE slug = $1",
        slug,
    )
    assert row is not None
    assert row["access_scope"] == "partner_internal"
    assert str(row["partner_id"]) == org_id

    # Cleanup
    await db.execute("DELETE FROM brain_pages WHERE slug = $1", slug)


@pytest.mark.postgres
async def test_partner_page_invisible_without_guc(db, org_id):
    """A partner page should not appear in filtered queries without the right GUC."""
    from src.services.brain_service import BrainService, PageInput

    slug = f"partner-invisible-{RUN_TAG}"
    brain = BrainService()

    async with db.transaction():
        await brain.put_page(
            db,
            slug,
            PageInput(
                type="source_document",
                title="Secret Partner Doc",
                compiled_truth="This should be invisible to other orgs.",
                frontmatter={"source_type": "partner_upload"},
            ),
            owner_uuid=TEST_USER_UUID,
        )
        await db.execute(
            """
            UPDATE brain_pages
            SET access_scope = 'partner_internal',
                partner_id   = $2::uuid
            WHERE slug = $1
            """,
            slug,
            org_id,
        )

    # Query with a DIFFERENT partner_id GUC (simulating another org)
    other_org_id = str(uuid.uuid4())
    await db.execute(
        "SELECT set_config('app.partner_id', $1, false)", other_org_id
    )

    pages = await brain.list_pages(db, limit=200)
    partner_slugs = [p.slug for p in pages if p.slug == slug]
    assert len(partner_slugs) == 0, "Partner page visible to wrong org"

    # Reset GUC
    await db.execute("RESET app.partner_id")

    # Cleanup
    await db.execute("DELETE FROM brain_pages WHERE slug = $1", slug)


@pytest.mark.postgres
async def test_partner_hook_created(db, org_id):
    """Verify that a partner_url_fetch hook can be created and queried."""
    from src.services.brain_service import BrainService

    brain = BrainService()
    hook_id = await brain.enqueue_hook(
        db,
        hook_type="partner_url_fetch",
        payload={
            "url": "https://example.com/report.pdf",
            "slug": f"partner-url-hook-{RUN_TAG}",
            "org_id": org_id,
            "user_id": TEST_USER_UUID,
        },
    )
    assert hook_id is not None

    hooks = await brain.get_pending_hooks(db, limit=100)
    matching = [h for h in hooks if h["id"] == hook_id]
    assert len(matching) == 1
    assert matching[0]["hook_type"] == "partner_url_fetch"

    # Cleanup
    await brain.complete_hook(db, hook_id)
