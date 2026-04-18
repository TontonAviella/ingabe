"""Brain service: Python port of gbrain BrainEngine for mundi.ai.

Ported from github.com/garrytan/gbrain (MIT, v0.9.2).
Uses asyncpg directly against the brain_* tables created by the Alembic migration.
All queries use brain_ prefixed table names and include owner_uuid for RLS.

Usage:
    brain = BrainService()
    page = await brain.put_page(conn, "field-gasabo-001", {...}, owner_uuid="...")
    results = await brain.search_keyword(conn, "banana field near Kigali")
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

import asyncpg

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SEARCH_LIMIT = 100
_DEFAULT_LIMIT = 20

# Agricultural page types for Rwanda insurance
PAGE_TYPES = {
    "field",
    "farmer",
    "district",
    "company",
    "insurance_worker",
    "claim",
    "policy",
    "season",
    "crop",
    "weather_station",
    "equipment",
    # Generic types from gbrain
    "person",
    "concept",
    "source",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Page:
    id: int
    slug: str
    type: str
    title: str
    compiled_truth: str
    timeline: str
    frontmatter: dict
    content_hash: Optional[str]
    owner_uuid: str
    viewer_uuids: list[str]
    editor_uuids: list[str]
    created_at: datetime
    updated_at: datetime


@dataclass
class PageInput:
    type: str
    title: str
    compiled_truth: str
    timeline: str = ""
    frontmatter: Optional[dict] = None
    content_hash: Optional[str] = None
    geom_geojson: Optional[str] = None  # GeoJSON geometry string


@dataclass
class SearchResult:
    slug: str
    page_id: int
    title: str
    type: str
    chunk_text: str
    chunk_source: str
    score: float


@dataclass
class TimelineInput:
    date: date
    summary: str
    source: str = ""
    detail: str = ""


@dataclass
class ChunkInput:
    chunk_index: int
    chunk_text: str
    chunk_source: str = "compiled_truth"
    embedding: Optional[list[float]] = None
    model: str = "nomic-embed-text"
    token_count: Optional[int] = None


@dataclass
class GraphNode:
    slug: str
    title: str
    type: str
    depth: int
    links: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp_limit(limit: Optional[int], default: int = _DEFAULT_LIMIT) -> int:
    if limit is None or limit <= 0:
        return default
    return min(limit, MAX_SEARCH_LIMIT)


def _validate_slug(slug: str) -> str:
    slug = slug.strip().lower()
    slug = re.sub(r"[^a-z0-9\-_]", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        raise ValueError("Invalid slug: empty after normalization")
    return slug


def _content_hash(page: PageInput) -> str:
    h = hashlib.sha256()
    h.update((page.compiled_truth or "").encode())
    h.update((page.timeline or "").encode())
    return h.hexdigest()[:16]


def _row_to_page(row: asyncpg.Record) -> Page:
    return Page(
        id=row["id"],
        slug=row["slug"],
        type=row["type"],
        title=row["title"],
        compiled_truth=row["compiled_truth"],
        timeline=row["timeline"],
        frontmatter=json.loads(row["frontmatter"]) if isinstance(row["frontmatter"], str) else (row["frontmatter"] or {}),
        content_hash=row.get("content_hash"),
        owner_uuid=str(row["owner_uuid"]),
        viewer_uuids=[str(u) for u in (row.get("viewer_uuids") or [])],
        editor_uuids=[str(u) for u in (row.get("editor_uuids") or [])],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# BrainService
# ---------------------------------------------------------------------------


class BrainService:
    """Python port of gbrain's PostgresEngine.

    Every method takes an asyncpg.Connection as first arg.
    The connection should already have app.user_id set for RLS.
    """

    # ── Pages CRUD ──────────────────────────────────────────────

    async def get_page(self, conn: asyncpg.Connection, slug: str) -> Optional[Page]:
        row = await conn.fetchrow(
            """
            SELECT id, slug, type, title, compiled_truth, timeline, frontmatter,
                   content_hash, owner_uuid, viewer_uuids, editor_uuids,
                   created_at, updated_at
            FROM brain_pages WHERE slug = $1
            """,
            slug,
        )
        return _row_to_page(row) if row else None

    async def put_page(
        self,
        conn: asyncpg.Connection,
        slug: str,
        page: PageInput,
        owner_uuid: str,
        viewer_uuids: Optional[list[str]] = None,
        editor_uuids: Optional[list[str]] = None,
    ) -> Page:
        slug = _validate_slug(slug)
        content_hash = page.content_hash or _content_hash(page)
        frontmatter = json.dumps(page.frontmatter or {})
        v_uuids = viewer_uuids or []
        e_uuids = editor_uuids or []

        if page.geom_geojson:
            row = await conn.fetchrow(
                """
                INSERT INTO brain_pages
                    (slug, type, title, compiled_truth, timeline, frontmatter,
                     content_hash, owner_uuid, viewer_uuids, editor_uuids, geom, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10,
                        ST_SetSRID(ST_GeomFromGeoJSON($11), 4326), now())
                ON CONFLICT (slug) DO UPDATE SET
                    type = EXCLUDED.type,
                    title = EXCLUDED.title,
                    compiled_truth = EXCLUDED.compiled_truth,
                    timeline = EXCLUDED.timeline,
                    frontmatter = EXCLUDED.frontmatter,
                    content_hash = EXCLUDED.content_hash,
                    geom = EXCLUDED.geom,
                    updated_at = now()
                RETURNING id, slug, type, title, compiled_truth, timeline, frontmatter,
                          content_hash, owner_uuid, viewer_uuids, editor_uuids,
                          created_at, updated_at
                """,
                slug, page.type, page.title, page.compiled_truth,
                page.timeline or "", frontmatter, content_hash,
                owner_uuid, v_uuids, e_uuids, page.geom_geojson,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO brain_pages
                    (slug, type, title, compiled_truth, timeline, frontmatter,
                     content_hash, owner_uuid, viewer_uuids, editor_uuids, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, now())
                ON CONFLICT (slug) DO UPDATE SET
                    type = EXCLUDED.type,
                    title = EXCLUDED.title,
                    compiled_truth = EXCLUDED.compiled_truth,
                    timeline = EXCLUDED.timeline,
                    frontmatter = EXCLUDED.frontmatter,
                    content_hash = EXCLUDED.content_hash,
                    updated_at = now()
                RETURNING id, slug, type, title, compiled_truth, timeline, frontmatter,
                          content_hash, owner_uuid, viewer_uuids, editor_uuids,
                          created_at, updated_at
                """,
                slug, page.type, page.title, page.compiled_truth,
                page.timeline or "", frontmatter, content_hash,
                owner_uuid, v_uuids, e_uuids,
            )

        return _row_to_page(row)

    async def delete_page(self, conn: asyncpg.Connection, slug: str) -> None:
        await conn.execute("DELETE FROM brain_pages WHERE slug = $1", slug)

    async def list_pages(
        self,
        conn: asyncpg.Connection,
        type: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Page]:
        if type and tag:
            rows = await conn.fetch(
                """
                SELECT p.* FROM brain_pages p
                JOIN brain_tags t ON t.page_id = p.id
                WHERE p.type = $1 AND t.tag = $2
                ORDER BY p.updated_at DESC LIMIT $3 OFFSET $4
                """,
                type, tag, limit, offset,
            )
        elif type:
            rows = await conn.fetch(
                """
                SELECT * FROM brain_pages WHERE type = $1
                ORDER BY updated_at DESC LIMIT $2 OFFSET $3
                """,
                type, limit, offset,
            )
        elif tag:
            rows = await conn.fetch(
                """
                SELECT p.* FROM brain_pages p
                JOIN brain_tags t ON t.page_id = p.id
                WHERE t.tag = $1
                ORDER BY p.updated_at DESC LIMIT $2 OFFSET $3
                """,
                tag, limit, offset,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM brain_pages
                ORDER BY updated_at DESC LIMIT $1 OFFSET $2
                """,
                limit, offset,
            )
        return [_row_to_page(r) for r in rows]

    async def resolve_slugs(self, conn: asyncpg.Connection, partial: str) -> list[str]:
        exact = await conn.fetch(
            "SELECT slug FROM brain_pages WHERE slug = $1", partial
        )
        if exact:
            return [exact[0]["slug"]]

        fuzzy = await conn.fetch(
            """
            SELECT slug, similarity(title, $1) AS sim
            FROM brain_pages
            WHERE title %% $1 OR slug ILIKE '%' || $1 || '%'
            ORDER BY sim DESC LIMIT 5
            """,
            partial,
        )
        return [r["slug"] for r in fuzzy]

    # ── Search ──────────────────────────────────────────────────

    async def search_keyword(
        self,
        conn: asyncpg.Connection,
        query: str,
        limit: Optional[int] = None,
        offset: int = 0,
        type: Optional[str] = None,
        exclude_slugs: Optional[list[str]] = None,
    ) -> list[SearchResult]:
        limit = _clamp_limit(limit)
        exclude = exclude_slugs or []

        rows = await conn.fetch(
            """
            WITH ranked_pages AS (
                SELECT p.id, p.slug, p.title, p.type,
                    ts_rank(p.search_vector, websearch_to_tsquery('english', $1)) AS score
                FROM brain_pages p
                WHERE p.search_vector @@ websearch_to_tsquery('english', $1)
                    AND ($4::text IS NULL OR p.type = $4)
                    AND p.slug != ALL($5::text[])
                ORDER BY score DESC
                LIMIT $2 OFFSET $3
            ),
            best_chunks AS (
                SELECT DISTINCT ON (rp.slug)
                    rp.slug, rp.id as page_id, rp.title, rp.type, rp.score,
                    cc.chunk_text, cc.chunk_source
                FROM ranked_pages rp
                LEFT JOIN brain_content_chunks cc ON cc.page_id = rp.id
                ORDER BY rp.slug, cc.chunk_index
            )
            SELECT slug, page_id, title, type,
                   coalesce(chunk_text, '') as chunk_text,
                   coalesce(chunk_source, 'compiled_truth') as chunk_source,
                   score
            FROM best_chunks
            ORDER BY score DESC
            """,
            query, limit, offset, type, exclude,
        )
        return [
            SearchResult(
                slug=r["slug"], page_id=r["page_id"], title=r["title"],
                type=r["type"], chunk_text=r["chunk_text"],
                chunk_source=r["chunk_source"], score=float(r["score"]),
            )
            for r in rows
        ]

    async def search_vector(
        self,
        conn: asyncpg.Connection,
        embedding: list[float],
        limit: Optional[int] = None,
        offset: int = 0,
        type: Optional[str] = None,
        exclude_slugs: Optional[list[str]] = None,
    ) -> list[SearchResult]:
        limit = _clamp_limit(limit)
        exclude = exclude_slugs or []
        vec_str = "[" + ",".join(str(v) for v in embedding) + "]"

        rows = await conn.fetch(
            """
            SELECT
                p.slug, p.id as page_id, p.title, p.type,
                cc.chunk_text, cc.chunk_source,
                1 - (cc.embedding <=> $1::vector) AS score
            FROM brain_content_chunks cc
            JOIN brain_pages p ON p.id = cc.page_id
            WHERE cc.embedding IS NOT NULL
                AND ($4::text IS NULL OR p.type = $4)
                AND p.slug != ALL($5::text[])
            ORDER BY cc.embedding <=> $1::vector
            LIMIT $2 OFFSET $3
            """,
            vec_str, limit, offset, type, exclude,
        )
        return [
            SearchResult(
                slug=r["slug"], page_id=r["page_id"], title=r["title"],
                type=r["type"], chunk_text=r["chunk_text"],
                chunk_source=r["chunk_source"], score=float(r["score"]),
            )
            for r in rows
        ]

    async def search_hybrid(
        self,
        conn: asyncpg.Connection,
        query: str,
        embedding: Optional[list[float]] = None,
        limit: Optional[int] = None,
        type: Optional[str] = None,
    ) -> list[SearchResult]:
        """Reciprocal Rank Fusion of keyword + vector search."""
        limit = _clamp_limit(limit)
        k = 60  # RRF constant

        keyword_results = await self.search_keyword(
            conn, query, limit=limit * 2, type=type
        )

        if embedding:
            vector_results = await self.search_vector(
                conn, embedding, limit=limit * 2, type=type
            )
        else:
            vector_results = []

        # RRF scoring
        scores: dict[str, float] = {}
        result_map: dict[str, SearchResult] = {}

        for rank, r in enumerate(keyword_results):
            scores[r.slug] = scores.get(r.slug, 0) + 1 / (k + rank + 1)
            result_map[r.slug] = r

        for rank, r in enumerate(vector_results):
            scores[r.slug] = scores.get(r.slug, 0) + 1 / (k + rank + 1)
            if r.slug not in result_map:
                result_map[r.slug] = r

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
        return [
            SearchResult(
                slug=slug, page_id=result_map[slug].page_id,
                title=result_map[slug].title, type=result_map[slug].type,
                chunk_text=result_map[slug].chunk_text,
                chunk_source=result_map[slug].chunk_source,
                score=score,
            )
            for slug, score in ranked
        ]

    # ── Chunks ──────────────────────────────────────────────────

    async def upsert_chunks(
        self, conn: asyncpg.Connection, slug: str, chunks: list[ChunkInput]
    ) -> None:
        page = await conn.fetchrow(
            "SELECT id FROM brain_pages WHERE slug = $1", slug
        )
        if not page:
            raise ValueError(f"Page not found: {slug}")
        page_id = page["id"]

        if not chunks:
            await conn.execute(
                "DELETE FROM brain_content_chunks WHERE page_id = $1", page_id
            )
            return

        new_indices = [c.chunk_index for c in chunks]
        await conn.execute(
            "DELETE FROM brain_content_chunks WHERE page_id = $1 AND chunk_index != ALL($2::int[])",
            page_id, new_indices,
        )

        for chunk in chunks:
            if chunk.embedding:
                vec_str = "[" + ",".join(str(v) for v in chunk.embedding) + "]"
                await conn.execute(
                    """
                    INSERT INTO brain_content_chunks
                        (page_id, chunk_index, chunk_text, chunk_source, embedding, model, token_count, embedded_at)
                    VALUES ($1, $2, $3, $4, $5::vector, $6, $7, now())
                    ON CONFLICT (page_id, chunk_index) DO UPDATE SET
                        chunk_text = EXCLUDED.chunk_text,
                        chunk_source = EXCLUDED.chunk_source,
                        embedding = CASE
                            WHEN EXCLUDED.chunk_text != brain_content_chunks.chunk_text THEN EXCLUDED.embedding
                            ELSE COALESCE(EXCLUDED.embedding, brain_content_chunks.embedding)
                        END,
                        model = COALESCE(EXCLUDED.model, brain_content_chunks.model),
                        token_count = EXCLUDED.token_count,
                        embedded_at = COALESCE(EXCLUDED.embedded_at, brain_content_chunks.embedded_at)
                    """,
                    page_id, chunk.chunk_index, chunk.chunk_text, chunk.chunk_source,
                    vec_str, chunk.model, chunk.token_count,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO brain_content_chunks
                        (page_id, chunk_index, chunk_text, chunk_source, model, token_count)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (page_id, chunk_index) DO UPDATE SET
                        chunk_text = EXCLUDED.chunk_text,
                        chunk_source = EXCLUDED.chunk_source,
                        model = COALESCE(EXCLUDED.model, brain_content_chunks.model),
                        token_count = EXCLUDED.token_count
                    """,
                    page_id, chunk.chunk_index, chunk.chunk_text, chunk.chunk_source,
                    chunk.model, chunk.token_count,
                )

    async def get_chunks(self, conn: asyncpg.Connection, slug: str) -> list[dict]:
        rows = await conn.fetch(
            """
            SELECT cc.* FROM brain_content_chunks cc
            JOIN brain_pages p ON p.id = cc.page_id
            WHERE p.slug = $1 ORDER BY cc.chunk_index
            """,
            slug,
        )
        return [dict(r) for r in rows]

    async def delete_chunks(self, conn: asyncpg.Connection, slug: str) -> None:
        await conn.execute(
            """
            DELETE FROM brain_content_chunks
            WHERE page_id = (SELECT id FROM brain_pages WHERE slug = $1)
            """,
            slug,
        )

    # ── Links ───────────────────────────────────────────────────

    async def add_link(
        self,
        conn: asyncpg.Connection,
        from_slug: str,
        to_slug: str,
        context: str = "",
        link_type: str = "",
    ) -> None:
        result = await conn.fetchrow(
            """
            INSERT INTO brain_links (from_page_id, to_page_id, link_type, context)
            SELECT f.id, t.id, $3, $4
            FROM brain_pages f, brain_pages t
            WHERE f.slug = $1 AND t.slug = $2
            ON CONFLICT (from_page_id, to_page_id) DO UPDATE SET
                link_type = EXCLUDED.link_type,
                context = EXCLUDED.context
            RETURNING id
            """,
            from_slug, to_slug, link_type, context,
        )
        if not result:
            raise ValueError(f"add_link failed: page '{from_slug}' or '{to_slug}' not found")

    async def remove_link(
        self, conn: asyncpg.Connection, from_slug: str, to_slug: str
    ) -> None:
        await conn.execute(
            """
            DELETE FROM brain_links
            WHERE from_page_id = (SELECT id FROM brain_pages WHERE slug = $1)
              AND to_page_id = (SELECT id FROM brain_pages WHERE slug = $2)
            """,
            from_slug, to_slug,
        )

    async def get_links(self, conn: asyncpg.Connection, slug: str) -> list[dict]:
        rows = await conn.fetch(
            """
            SELECT f.slug as from_slug, t.slug as to_slug, l.link_type, l.context
            FROM brain_links l
            JOIN brain_pages f ON f.id = l.from_page_id
            JOIN brain_pages t ON t.id = l.to_page_id
            WHERE f.slug = $1
            """,
            slug,
        )
        return [dict(r) for r in rows]

    async def get_backlinks(self, conn: asyncpg.Connection, slug: str) -> list[dict]:
        rows = await conn.fetch(
            """
            SELECT f.slug as from_slug, t.slug as to_slug, l.link_type, l.context
            FROM brain_links l
            JOIN brain_pages f ON f.id = l.from_page_id
            JOIN brain_pages t ON t.id = l.to_page_id
            WHERE t.slug = $1
            """,
            slug,
        )
        return [dict(r) for r in rows]

    async def traverse_graph(
        self, conn: asyncpg.Connection, slug: str, depth: int = 5
    ) -> list[GraphNode]:
        rows = await conn.fetch(
            """
            WITH RECURSIVE graph AS (
                SELECT p.id, p.slug, p.title, p.type, 0 as depth
                FROM brain_pages p WHERE p.slug = $1

                UNION

                SELECT p2.id, p2.slug, p2.title, p2.type, g.depth + 1
                FROM graph g
                JOIN brain_links l ON l.from_page_id = g.id
                JOIN brain_pages p2 ON p2.id = l.to_page_id
                WHERE g.depth < $2
            )
            SELECT DISTINCT g.slug, g.title, g.type, g.depth,
                coalesce(
                    (SELECT jsonb_agg(jsonb_build_object('to_slug', p3.slug, 'link_type', l2.link_type))
                     FROM brain_links l2
                     JOIN brain_pages p3 ON p3.id = l2.to_page_id
                     WHERE l2.from_page_id = g.id),
                    '[]'::jsonb
                ) as links
            FROM graph g
            ORDER BY g.depth, g.slug
            """,
            slug, depth,
        )
        return [
            GraphNode(
                slug=r["slug"], title=r["title"], type=r["type"],
                depth=r["depth"],
                links=json.loads(r["links"]) if isinstance(r["links"], str) else (r["links"] or []),
            )
            for r in rows
        ]

    # ── Tags ────────────────────────────────────────────────────

    async def add_tag(self, conn: asyncpg.Connection, slug: str, tag: str) -> None:
        page = await conn.fetchrow(
            "SELECT id FROM brain_pages WHERE slug = $1", slug
        )
        if not page:
            raise ValueError(f"add_tag failed: page '{slug}' not found")
        await conn.execute(
            """
            INSERT INTO brain_tags (page_id, tag) VALUES ($1, $2)
            ON CONFLICT (page_id, tag) DO NOTHING
            """,
            page["id"], tag,
        )

    async def remove_tag(self, conn: asyncpg.Connection, slug: str, tag: str) -> None:
        await conn.execute(
            """
            DELETE FROM brain_tags
            WHERE page_id = (SELECT id FROM brain_pages WHERE slug = $1) AND tag = $2
            """,
            slug, tag,
        )

    async def get_tags(self, conn: asyncpg.Connection, slug: str) -> list[str]:
        rows = await conn.fetch(
            """
            SELECT tag FROM brain_tags
            WHERE page_id = (SELECT id FROM brain_pages WHERE slug = $1)
            ORDER BY tag
            """,
            slug,
        )
        return [r["tag"] for r in rows]

    # ── Timeline ────────────────────────────────────────────────

    async def add_timeline_entry(
        self,
        conn: asyncpg.Connection,
        slug: str,
        entry: TimelineInput,
        owner_uuid: Optional[str] = None,
    ) -> int:
        result = await conn.fetchrow(
            """
            INSERT INTO brain_timeline_entries (page_id, date, source, summary, detail, owner_uuid)
            SELECT id, $2, $3, $4, $5, $6
            FROM brain_pages WHERE slug = $1
            RETURNING id
            """,
            slug, entry.date, entry.source, entry.summary, entry.detail, owner_uuid,
        )
        if not result:
            raise ValueError(f"add_timeline_entry failed: page '{slug}' not found")
        return result["id"]

    async def get_timeline(
        self,
        conn: asyncpg.Connection,
        slug: str,
        limit: int = 100,
        after: Optional[date] = None,
        before: Optional[date] = None,
    ) -> list[dict]:
        if after and before:
            rows = await conn.fetch(
                """
                SELECT te.* FROM brain_timeline_entries te
                JOIN brain_pages p ON p.id = te.page_id
                WHERE p.slug = $1 AND te.date >= $2 AND te.date <= $3
                ORDER BY te.date DESC LIMIT $4
                """,
                slug, after, before, limit,
            )
        elif after:
            rows = await conn.fetch(
                """
                SELECT te.* FROM brain_timeline_entries te
                JOIN brain_pages p ON p.id = te.page_id
                WHERE p.slug = $1 AND te.date >= $2
                ORDER BY te.date DESC LIMIT $3
                """,
                slug, after, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT te.* FROM brain_timeline_entries te
                JOIN brain_pages p ON p.id = te.page_id
                WHERE p.slug = $1
                ORDER BY te.date DESC LIMIT $2
                """,
                slug, limit,
            )
        return [dict(r) for r in rows]

    # ── Raw Data ────────────────────────────────────────────────

    async def put_raw_data(
        self, conn: asyncpg.Connection, slug: str, source: str, data: dict
    ) -> None:
        result = await conn.fetchrow(
            """
            INSERT INTO brain_raw_data (page_id, source, data)
            SELECT id, $2, $3::jsonb
            FROM brain_pages WHERE slug = $1
            ON CONFLICT (page_id, source) DO UPDATE SET
                data = EXCLUDED.data, fetched_at = now()
            RETURNING id
            """,
            slug, source, json.dumps(data),
        )
        if not result:
            raise ValueError(f"put_raw_data failed: page '{slug}' not found")

    async def get_raw_data(
        self, conn: asyncpg.Connection, slug: str, source: Optional[str] = None
    ) -> list[dict]:
        if source:
            rows = await conn.fetch(
                """
                SELECT rd.source, rd.data, rd.fetched_at FROM brain_raw_data rd
                JOIN brain_pages p ON p.id = rd.page_id
                WHERE p.slug = $1 AND rd.source = $2
                """,
                slug, source,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT rd.source, rd.data, rd.fetched_at FROM brain_raw_data rd
                JOIN brain_pages p ON p.id = rd.page_id
                WHERE p.slug = $1
                """,
                slug,
            )
        return [dict(r) for r in rows]

    # ── Versions ────────────────────────────────────────────────

    async def create_version(self, conn: asyncpg.Connection, slug: str) -> dict:
        row = await conn.fetchrow(
            """
            INSERT INTO brain_page_versions (page_id, compiled_truth, frontmatter)
            SELECT id, compiled_truth, frontmatter
            FROM brain_pages WHERE slug = $1
            RETURNING *
            """,
            slug,
        )
        if not row:
            raise ValueError(f"create_version failed: page '{slug}' not found")
        return dict(row)

    async def get_versions(self, conn: asyncpg.Connection, slug: str) -> list[dict]:
        rows = await conn.fetch(
            """
            SELECT pv.* FROM brain_page_versions pv
            JOIN brain_pages p ON p.id = pv.page_id
            WHERE p.slug = $1 ORDER BY pv.snapshot_at DESC
            """,
            slug,
        )
        return [dict(r) for r in rows]

    async def revert_to_version(
        self, conn: asyncpg.Connection, slug: str, version_id: int
    ) -> None:
        await conn.execute(
            """
            UPDATE brain_pages SET
                compiled_truth = pv.compiled_truth,
                frontmatter = pv.frontmatter,
                updated_at = now()
            FROM brain_page_versions pv
            WHERE brain_pages.slug = $1
              AND pv.id = $2
              AND pv.page_id = brain_pages.id
            """,
            slug, version_id,
        )

    # ── Stats + Health ──────────────────────────────────────────

    async def get_stats(self, conn: asyncpg.Connection) -> dict:
        row = await conn.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM brain_pages) as page_count,
                (SELECT count(*) FROM brain_content_chunks) as chunk_count,
                (SELECT count(*) FROM brain_content_chunks WHERE embedded_at IS NOT NULL) as embedded_count,
                (SELECT count(*) FROM brain_links) as link_count,
                (SELECT count(DISTINCT tag) FROM brain_tags) as tag_count,
                (SELECT count(*) FROM brain_timeline_entries) as timeline_entry_count
            """
        )
        types = await conn.fetch(
            "SELECT type, count(*)::int as count FROM brain_pages GROUP BY type ORDER BY count DESC"
        )
        return {
            "page_count": row["page_count"],
            "chunk_count": row["chunk_count"],
            "embedded_count": row["embedded_count"],
            "link_count": row["link_count"],
            "tag_count": row["tag_count"],
            "timeline_entry_count": row["timeline_entry_count"],
            "pages_by_type": {t["type"]: t["count"] for t in types},
        }

    async def get_health(self, conn: asyncpg.Connection) -> dict:
        row = await conn.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM brain_pages) as page_count,
                (SELECT count(*) FROM brain_content_chunks WHERE embedded_at IS NOT NULL)::float /
                    GREATEST((SELECT count(*) FROM brain_content_chunks), 1)::float as embed_coverage,
                (SELECT count(*) FROM brain_pages p
                 WHERE (p.compiled_truth != '' OR p.timeline != '')
                   AND NOT EXISTS (SELECT 1 FROM brain_content_chunks cc WHERE cc.page_id = p.id)
                ) as stale_pages,
                (SELECT count(*) FROM brain_pages p
                 WHERE NOT EXISTS (SELECT 1 FROM brain_links l WHERE l.to_page_id = p.id)
                   AND NOT EXISTS (SELECT 1 FROM brain_links l WHERE l.from_page_id = p.id)
                ) as orphan_pages,
                (SELECT count(*) FROM brain_content_chunks WHERE embedded_at IS NULL) as missing_embeddings
            """
        )
        return dict(row)

    # ── Ingest Log ──────────────────────────────────────────────

    async def log_ingest(
        self,
        conn: asyncpg.Connection,
        source_type: str,
        source_ref: str,
        pages_updated: list[str],
        summary: str,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO brain_ingest_log (source_type, source_ref, pages_updated, summary)
            VALUES ($1, $2, $3::jsonb, $4)
            """,
            source_type, source_ref, json.dumps(pages_updated), summary,
        )

    async def get_ingest_log(
        self, conn: asyncpg.Connection, limit: int = 50
    ) -> list[dict]:
        rows = await conn.fetch(
            "SELECT * FROM brain_ingest_log ORDER BY created_at DESC LIMIT $1", limit
        )
        return [dict(r) for r in rows]

    # ── Slug Management ─────────────────────────────────────────

    async def update_slug(
        self, conn: asyncpg.Connection, old_slug: str, new_slug: str
    ) -> None:
        new_slug = _validate_slug(new_slug)
        await conn.execute(
            "UPDATE brain_pages SET slug = $1, updated_at = now() WHERE slug = $2",
            new_slug, old_slug,
        )

    # ── Spatial Queries ─────────────────────────────────────────

    async def get_pages_in_bbox(
        self,
        conn: asyncpg.Connection,
        bbox: tuple[float, float, float, float],
        limit: int = 50,
        type: Optional[str] = None,
    ) -> list[Page]:
        """Get brain pages whose geometry intersects a bounding box.

        Args:
            bbox: (lon_min, lat_min, lon_max, lat_max)
        """
        lon_min, lat_min, lon_max, lat_max = bbox
        if type:
            rows = await conn.fetch(
                """
                SELECT * FROM brain_pages
                WHERE geom IS NOT NULL
                  AND ST_Intersects(geom, ST_MakeEnvelope($1, $2, $3, $4, 4326))
                  AND type = $5
                ORDER BY updated_at DESC LIMIT $6
                """,
                lon_min, lat_min, lon_max, lat_max, type, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM brain_pages
                WHERE geom IS NOT NULL
                  AND ST_Intersects(geom, ST_MakeEnvelope($1, $2, $3, $4, 4326))
                ORDER BY updated_at DESC LIMIT $5
                """,
                lon_min, lat_min, lon_max, lat_max, limit,
            )
        return [_row_to_page(r) for r in rows]

    # ── Pending Hooks ───────────────────────────────────────────

    async def enqueue_hook(
        self,
        conn: asyncpg.Connection,
        hook_type: str,
        payload: dict,
        max_attempts: int = 5,
    ) -> int:
        row = await conn.fetchrow(
            """
            INSERT INTO brain_pending_hooks (hook_type, payload, max_attempts)
            VALUES ($1, $2::jsonb, $3)
            RETURNING id
            """,
            hook_type, json.dumps(payload), max_attempts,
        )
        return row["id"]

    async def get_pending_hooks(
        self, conn: asyncpg.Connection, limit: int = 10
    ) -> list[dict]:
        rows = await conn.fetch(
            """
            SELECT * FROM brain_pending_hooks
            WHERE completed_at IS NULL
              AND attempts < max_attempts
              AND next_retry_at <= now()
            ORDER BY next_retry_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]

    async def complete_hook(self, conn: asyncpg.Connection, hook_id: int) -> None:
        await conn.execute(
            "UPDATE brain_pending_hooks SET completed_at = now() WHERE id = $1",
            hook_id,
        )

    async def fail_hook(
        self, conn: asyncpg.Connection, hook_id: int, error: str
    ) -> None:
        await conn.execute(
            """
            UPDATE brain_pending_hooks SET
                attempts = attempts + 1,
                last_error = $2,
                next_retry_at = now() + (interval '1 minute' * power(2, attempts))
            WHERE id = $1
            """,
            hook_id, error,
        )
