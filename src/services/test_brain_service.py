"""Tests for BrainService — Python port of gbrain BrainEngine.

11 critical tests covering CRUD, search, timeline, graph, versioning,
hooks, and RLS isolation.

Requires: running PostgreSQL with brain_* tables (alembic upgrade head).
"""

import uuid
from datetime import date, datetime

import asyncpg
import pytest
import pytest_asyncio

from src.database.pool import _build_postgres_url
from src.services.brain_service import (
    BrainService,
    ChunkInput,
    Page,
    PageInput,
    SearchResult,
    TimelineInput,
    _validate_slug,
)
from src.services.brain_embeddings import chunk_text
from src.services.brain_hook_processor import (
    process_pending_hooks,
    _extract_feature_name,
    _infer_page_type,
    _build_feature_truth,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.asyncio(loop_scope="module")

# Deterministic so pytest-xdist workers share owner across the shared
# `test-field-001` slug they upsert. Random uuid4 at module import caused
# the first worker to win ownership and subsequent workers to fail RLS
# on tag/timeline INSERTs (page belongs to a different owner_uuid).
TEST_OWNER = "00000000-0000-0000-0000-000000000111"
TEST_OWNER_B = "00000000-0000-0000-0000-000000000112"


_MIGRATIONS_DONE = False


@pytest_asyncio.fixture(loop_scope="module")
async def brain_conn():
    """Per-test asyncpg connection with brain tables + RLS context.

    Function-scoped (not session-scoped) because pytest-xdist with parallel
    workers AND multiple tests sharing one asyncpg connection produces
    "another operation is in progress" errors — asyncpg connections are
    not concurrency-safe. Each test gets a fresh connection.

    Migrations run once per worker via the module-level flag. asyncpg is
    cheap to connect (~10-50ms) so per-test cost is acceptable.
    """
    global _MIGRATIONS_DONE
    if not _MIGRATIONS_DONE:
        from src.database.migrate import run_migrations
        await run_migrations()
        _MIGRATIONS_DONE = True

    url = _build_postgres_url()
    c = await asyncpg.connect(url)
    await c.execute("SELECT set_config('app.user_id', $1, false)", TEST_OWNER)

    # Seed one page for tests that need an existing page. Use ON CONFLICT
    # via put_page (which upserts) so re-seeding from prior tests is safe.
    svc = BrainService()
    await svc.put_page(
        c,
        "test-field-001",
        PageInput(
            type="field",
            title="Gasabo Test Field",
            compiled_truth="A 2-hectare banana field in Gasabo district, Kigali.",
        ),
        owner_uuid=TEST_OWNER,
    )

    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
def conn(brain_conn):
    return brain_conn


@pytest.fixture
def brain():
    return BrainService()


# ---------------------------------------------------------------------------
# 1. put_page + get_page roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_put_and_get_page(conn, brain):
    """Create a page and read it back — all fields must match."""
    page = await brain.put_page(
        conn,
        "test-farmer-alice",
        PageInput(
            type="farmer",
            title="Alice Uwimana",
            compiled_truth="Smallholder farmer in Rwamagana, grows maize and beans.",
            frontmatter={"district": "Rwamagana", "crops": ["maize", "beans"]},
        ),
        owner_uuid=TEST_OWNER,
    )

    assert isinstance(page, Page)
    assert page.slug == "test-farmer-alice"
    assert page.type == "farmer"
    assert page.title == "Alice Uwimana"

    fetched = await brain.get_page(conn, "test-farmer-alice")
    assert fetched is not None
    assert fetched.slug == page.slug
    assert fetched.compiled_truth == page.compiled_truth
    assert fetched.frontmatter["district"] == "Rwamagana"


# ---------------------------------------------------------------------------
# 2. put_page upsert (update existing page)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_put_page_upsert(conn, brain):
    """put_page on an existing slug updates rather than duplicates."""
    await brain.put_page(
        conn,
        "test-field-001",
        PageInput(
            type="field",
            title="Gasabo Test Field (Updated)",
            compiled_truth="A 3-hectare banana field in Gasabo district, expanded.",
        ),
        owner_uuid=TEST_OWNER,
    )

    page = await brain.get_page(conn, "test-field-001")
    assert page is not None
    assert "3-hectare" in page.compiled_truth
    assert page.title == "Gasabo Test Field (Updated)"


# ---------------------------------------------------------------------------
# 3. delete_page
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_delete_page(conn, brain):
    """Delete a page and confirm it's gone."""
    await brain.put_page(
        conn,
        "test-delete-me",
        PageInput(type="field", title="Delete Me", compiled_truth="Temporary."),
        owner_uuid=TEST_OWNER,
    )
    assert await brain.get_page(conn, "test-delete-me") is not None

    await brain.delete_page(conn, "test-delete-me")
    assert await brain.get_page(conn, "test-delete-me") is None


# ---------------------------------------------------------------------------
# 4. list_pages with type filter
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_list_pages_type_filter(conn, brain):
    """list_pages filters by type correctly."""
    await brain.put_page(
        conn,
        "test-district-kigali",
        PageInput(type="district", title="Kigali", compiled_truth="Capital city."),
        owner_uuid=TEST_OWNER,
    )

    fields = await brain.list_pages(conn, type="field")
    districts = await brain.list_pages(conn, type="district")

    field_types = {p.type for p in fields}
    district_types = {p.type for p in districts}

    assert field_types <= {"field"}
    assert district_types <= {"district"}
    assert any(p.slug == "test-district-kigali" for p in districts)


# ---------------------------------------------------------------------------
# 5. keyword search (tsvector)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_search_keyword(conn, brain):
    """Keyword search finds pages by compiled_truth content."""
    await brain.put_page(
        conn,
        "test-search-banana",
        PageInput(
            type="crop",
            title="Banana Variety EAH",
            compiled_truth="East African Highland banana is the staple crop in Rwanda highlands.",
        ),
        owner_uuid=TEST_OWNER,
    )

    results = await brain.search_keyword(conn, "banana highland Rwanda")
    assert len(results) > 0
    slugs = [r.slug for r in results]
    assert "test-search-banana" in slugs


# ---------------------------------------------------------------------------
# 6. Timeline entries
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_timeline_crud(conn, brain):
    """Add timeline entries and retrieve them sorted by date."""
    await brain.add_timeline_entry(
        conn,
        "test-field-001",
        TimelineInput(
            date=date(2026, 3, 1),
            summary="Field planted with Season A maize",
            source="field_visit",
        ),
        owner_uuid=TEST_OWNER,
    )
    await brain.add_timeline_entry(
        conn,
        "test-field-001",
        TimelineInput(
            date=date(2026, 4, 10),
            summary="NDVI dropped below 0.3 — possible drought stress",
            source="satellite",
        ),
        owner_uuid=TEST_OWNER,
    )

    timeline = await brain.get_timeline(conn, "test-field-001")
    assert len(timeline) >= 2
    # Should be ordered by date desc
    dates = [e["date"] for e in timeline]
    assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# 7. Tags
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_tags(conn, brain):
    """Add, list, and remove tags from a page."""
    await brain.add_tag(conn, "test-field-001", "insurance-monitored")
    await brain.add_tag(conn, "test-field-001", "season-a-2026")

    tags = await brain.get_tags(conn, "test-field-001")
    assert "insurance-monitored" in tags
    assert "season-a-2026" in tags

    await brain.remove_tag(conn, "test-field-001", "season-a-2026")
    tags = await brain.get_tags(conn, "test-field-001")
    assert "season-a-2026" not in tags
    assert "insurance-monitored" in tags


# ---------------------------------------------------------------------------
# 8. Links (entity graph)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_links_and_graph(conn, brain):
    """Create links between pages and traverse the graph."""
    await brain.put_page(
        conn,
        "test-graph-farmer",
        PageInput(type="farmer", title="Graph Farmer", compiled_truth="Farmer for graph test."),
        owner_uuid=TEST_OWNER,
    )
    await brain.put_page(
        conn,
        "test-graph-field",
        PageInput(type="field", title="Graph Field", compiled_truth="Field for graph test."),
        owner_uuid=TEST_OWNER,
    )

    await brain.add_link(conn, "test-graph-farmer", "test-graph-field", link_type="owns", context="Primary field")

    links = await brain.get_links(conn, "test-graph-farmer")
    assert len(links) >= 1
    assert any(l["to_slug"] == "test-graph-field" for l in links)

    backlinks = await brain.get_backlinks(conn, "test-graph-field")
    assert any(l["from_slug"] == "test-graph-farmer" for l in backlinks)

    # Traverse graph from farmer — should reach field at depth 1
    graph = await brain.traverse_graph(conn, "test-graph-farmer", depth=2)
    assert len(graph) >= 2
    slugs = {n.slug for n in graph}
    assert "test-graph-farmer" in slugs
    assert "test-graph-field" in slugs


# ---------------------------------------------------------------------------
# 9. Versioning (snapshot + revert)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_versioning(conn, brain):
    """Create a version snapshot, modify page, revert to snapshot."""
    await brain.put_page(
        conn,
        "test-version-page",
        PageInput(type="field", title="Version Test", compiled_truth="Version 1 content."),
        owner_uuid=TEST_OWNER,
    )

    # Create snapshot
    await brain.create_version(conn, "test-version-page")

    # Modify page
    await brain.put_page(
        conn,
        "test-version-page",
        PageInput(type="field", title="Version Test", compiled_truth="Version 2 content."),
        owner_uuid=TEST_OWNER,
    )
    modified = await brain.get_page(conn, "test-version-page")
    assert "Version 2" in modified.compiled_truth

    # Get versions
    versions = await brain.get_versions(conn, "test-version-page")
    assert len(versions) >= 1

    # Revert
    await brain.revert_to_version(conn, "test-version-page", versions[0]["id"])
    reverted = await brain.get_page(conn, "test-version-page")
    assert "Version 1" in reverted.compiled_truth


# ---------------------------------------------------------------------------
# 10. Hook queue (enqueue + complete + fail with backoff)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_hook_queue(conn, brain):
    """Enqueue a hook, fetch pending, complete it. Then test failure backoff."""
    # Use unique hook types to avoid collision with other tests
    tag = str(uuid.uuid4())[:8]
    await brain.enqueue_hook(conn, f"raster_test_{tag}", {"layer_id": "L_test_001"})
    await brain.enqueue_hook(conn, f"vector_test_{tag}", {"layer_id": "L_test_002"})

    pending = await brain.get_pending_hooks(conn, limit=50)
    assert len(pending) >= 2

    raster_hook = next(h for h in pending if h["hook_type"] == f"raster_test_{tag}")
    vector_hook = next(h for h in pending if h["hook_type"] == f"vector_test_{tag}")

    # Complete raster hook
    await brain.complete_hook(conn, raster_hook["id"])

    # Fail vector hook — should increment attempts and set next_retry_at
    await brain.fail_hook(conn, vector_hook["id"], "Test error")

    # Verify raster is done
    updated_raster = await conn.fetchrow(
        "SELECT completed_at FROM brain_pending_hooks WHERE id = $1",
        raster_hook["id"],
    )
    assert updated_raster["completed_at"] is not None

    # Verify vector has exponential backoff
    updated_vector = await conn.fetchrow(
        "SELECT attempts, last_error, next_retry_at FROM brain_pending_hooks WHERE id = $1",
        vector_hook["id"],
    )
    assert updated_vector["attempts"] == 1
    assert updated_vector["last_error"] == "Test error"
    assert updated_vector["next_retry_at"] > datetime.now(updated_vector["next_retry_at"].tzinfo)


# ---------------------------------------------------------------------------
# 11. Slug validation
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_slug_validation(conn, brain):
    """Slug normalization: lowercase, strip special chars, collapse dashes."""
    assert _validate_slug("Hello World!") == "hello-world"
    assert _validate_slug("  --test--slug--  ") == "test-slug"
    assert _validate_slug("Field_Gasabo_001") == "field_gasabo_001"

    with pytest.raises(ValueError):
        _validate_slug("   ")

    # Verify put_page normalizes the slug
    page = await brain.put_page(
        conn,
        "UPPER Case Slug!",
        PageInput(type="field", title="Slug Test", compiled_truth="Testing."),
        owner_uuid=TEST_OWNER,
    )
    assert page.slug == "upper-case-slug"


# ---------------------------------------------------------------------------
# Phase 2: Spatial queries
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_spatial_pages_in_bbox(conn, brain):
    """Pages with geometry are found by bounding box query."""
    # Create a page with geometry in Kigali (lat ~-1.95, lon ~29.87)
    kigali_geom = '{"type":"Point","coordinates":[29.87,-1.95]}'
    await brain.put_page(
        conn,
        "test-spatial-kigali",
        PageInput(
            type="field",
            title="Kigali Spatial Field",
            compiled_truth="A test field in Kigali for spatial query.",
            geom_geojson=kigali_geom,
        ),
        owner_uuid=TEST_OWNER,
    )

    # Create a page in Butare (lat ~-2.60, lon ~29.74) — outside Kigali bbox
    butare_geom = '{"type":"Point","coordinates":[29.74,-2.60]}'
    await brain.put_page(
        conn,
        "test-spatial-butare",
        PageInput(
            type="field",
            title="Butare Spatial Field",
            compiled_truth="A test field in Butare.",
            geom_geojson=butare_geom,
        ),
        owner_uuid=TEST_OWNER,
    )

    # Query Kigali bbox — should find Kigali, not Butare
    kigali_bbox = (29.8, -2.0, 29.95, -1.9)
    pages = await brain.get_pages_in_bbox(conn, kigali_bbox)
    slugs = [p.slug for p in pages]
    assert "test-spatial-kigali" in slugs
    assert "test-spatial-butare" not in slugs

    # Query wider Rwanda bbox — should find both
    rwanda_bbox = (28.0, -3.0, 31.0, -1.0)
    pages = await brain.get_pages_in_bbox(conn, rwanda_bbox)
    slugs = [p.slug for p in pages]
    assert "test-spatial-kigali" in slugs
    assert "test-spatial-butare" in slugs


# ---------------------------------------------------------------------------
# Phase 2: Chunking
# ---------------------------------------------------------------------------


def test_chunk_text_basic():
    """chunk_text splits long text into overlapping pieces."""
    short = "Short text."
    assert chunk_text(short) == ["Short text."]

    # Create a text that's ~3 chunks worth (~6000 chars)
    long_text = "This is a test sentence. " * 300  # ~7500 chars
    chunks = chunk_text(long_text, chunk_size=500, overlap=50)
    assert len(chunks) >= 3

    # Each chunk should be non-empty
    for c in chunks:
        assert len(c) > 0

    # Empty text returns empty list
    assert chunk_text("") == []
    assert chunk_text("   ") == []


@pytest.mark.postgres
@pytest.mark.skip(
    reason="Requires Ollama (BRAIN_EMBEDDINGS_PROVIDER=ollama, nomic-embed-text). "
    "CI compose stack doesn't include the ollama service. /cso 2026-05-06."
)
async def test_upsert_chunks_with_embedding(conn, brain):
    """Chunks with embeddings can be stored and retrieved."""
    await brain.put_page(
        conn,
        "test-embed-page",
        PageInput(type="field", title="Embed Test", compiled_truth="Test content for embedding."),
        owner_uuid=TEST_OWNER,
    )

    # Create fake embeddings (1536 dims)
    fake_embedding = [0.01 * i for i in range(1536)]

    chunks = [
        ChunkInput(
            chunk_index=0,
            chunk_text="First chunk of content.",
            embedding=fake_embedding,
            token_count=6,
        ),
        ChunkInput(
            chunk_index=1,
            chunk_text="Second chunk of content.",
            embedding=fake_embedding,
            token_count=6,
        ),
    ]

    await brain.upsert_chunks(conn, "test-embed-page", chunks)

    stored = await brain.get_chunks(conn, "test-embed-page")
    assert len(stored) == 2
    assert stored[0]["chunk_text"] == "First chunk of content."
    assert stored[0]["embedded_at"] is not None
    assert stored[1]["chunk_index"] == 1


@pytest.mark.postgres
@pytest.mark.skip(
    reason="Requires Ollama (BRAIN_EMBEDDINGS_PROVIDER=ollama). CI compose "
    "doesn't include ollama service. /cso 2026-05-06."
)
async def test_vector_search_with_embeddings(conn, brain):
    """Vector search finds pages by embedding similarity."""
    # The page and chunks from test above should still exist
    # Search with the same fake embedding — should match
    fake_embedding = [0.01 * i for i in range(1536)]

    results = await brain.search_vector(conn, fake_embedding, limit=5)
    # Should find the test-embed-page
    assert len(results) > 0
    slugs = [r.slug for r in results]
    assert "test-embed-page" in slugs
    assert results[0].score > 0.5  # High similarity to identical vector


# ---------------------------------------------------------------------------
# Phase 2: Hook processor helpers
# ---------------------------------------------------------------------------


def test_extract_feature_name():
    """Feature name extraction from properties."""
    assert _extract_feature_name({"name": "My Field"}, 0, "Layer") == "My Field"
    assert _extract_feature_name({"Name": "Named"}, 0, "L") == "Named"
    assert _extract_feature_name({}, 0, "Test Layer") == "Test Layer #1"
    assert _extract_feature_name({"area": 100}, 5, "Fields") == "Fields #6"


def test_infer_page_type():
    """Page type inference from properties and geometry."""
    assert _infer_page_type({"type": "farmer"}, "Point") == "farmer"
    assert _infer_page_type({}, "Polygon") == "field"
    assert _infer_page_type({}, "Point") == "farmer"
    assert _infer_page_type({}, None) == "field"


def test_build_feature_truth():
    """Feature truth generation from properties."""
    truth = _build_feature_truth(
        {"area_ha": 2.5, "crop": "maize"}, "Gasabo Fields", "field"
    )
    assert "Gasabo Fields" in truth
    assert "area_ha" in truth
    assert "maize" in truth


@pytest.mark.postgres
@pytest.mark.skip(
    reason="Hook processor's raster handler reads the COG from S3 (uploads/test/"
    "raster.tif). Test seeds the map_layers row but never uploads the actual "
    "COG bytes to MinIO, so the handler silently fails to create the page. "
    "Test needs S3 fixture + real raster bytes, OR mock the COG read. "
    "/cso 2026-05-06."
)
async def test_hook_processor_raster(conn, brain):
    """Hook processor creates a brain page from a raster_upload hook."""
    # First create a fake layer in map_layers
    await conn.execute(
        """
        INSERT INTO map_layers
        (layer_id, owner_uuid, name, type, metadata, bounds, s3_key, source_map_id)
        VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (layer_id) DO NOTHING
        """,
        "L_tst_rst_01",
        TEST_OWNER,
        "Test Raster",
        "raster",
        '{"band_count": 1, "original_srid": 32736}',
        [29.8, -2.0, 29.95, -1.9],
        "uploads/test/raster.tif",
        "M_test_001",
    )

    # Enqueue a raster hook
    hook_id = await brain.enqueue_hook(conn, "raster_upload", {
        "layer_id": "L_tst_rst_01",
        "layer_name": "Test Raster",
        "user_id": TEST_OWNER,
        "bounds": [29.8, -2.0, 29.95, -1.9],
    })

    # Process hooks
    result = await process_pending_hooks(conn, brain, limit=10)
    assert result["processed"] >= 1

    # Verify brain page was created (slug is lowercased by _validate_slug)
    page = await brain.get_page(conn, "raster-l_tst_rst_01")
    assert page is not None
    assert "Test Raster" in page.title
    assert "raster" in page.compiled_truth.lower()

    # Verify hook was marked complete
    hook = await conn.fetchrow(
        "SELECT completed_at FROM brain_pending_hooks WHERE id = $1", hook_id
    )
    assert hook["completed_at"] is not None
