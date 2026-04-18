"""Normalizer — FetchedContent → brain_pages row via BrainService.

Keeps the write path consistent regardless of which fetcher produced the
content. One function because the logic is small; split per-source if it
grows.

Slug strategy: <source_id>:<sha1(url)[:16]>. Deterministic so re-fetches
upsert the same row. source_id prefix makes it obvious in logs which
fetcher owns a page.

Partner isolation: this function is the only path that writes
access_scope + partner_id on brain_pages. If a SourceConfig says
access_scope=partner_internal, partner_id MUST be set — we assert here
rather than relying on the DB CHECK constraint alone, so the error
surfaces with source context.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from typing import Optional

import asyncpg

from src.services.brain_ingestion.feature_flags import partner_internal_enabled
from src.services.brain_ingestion.models import FetchedContent
from src.services.brain_service import BrainService, PageInput, TimelineInput

logger = logging.getLogger(__name__)


def build_slug(source_id: str, url: str) -> str:
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    # brain_service._validate_slug restricts to [a-z0-9-], lower, <= 128.
    safe_src = re.sub(r"[^a-z0-9-]", "-", source_id.lower())[:60]
    return f"{safe_src}-{url_hash}"


async def write_page(
    conn: asyncpg.Connection,
    brain: BrainService,
    item: FetchedContent,
    owner_uuid: str,
) -> str:
    """Persist one FetchedContent as a brain_pages row. Returns the slug."""
    if item.access_scope == "partner_internal" and not item.partner_id:
        raise ValueError(
            f"partner_internal content from source={item.source_id} has no "
            "partner_id. Refusing to write — this would leak across tenants."
        )

    # Belt-and-suspenders: the scheduler skips partner_internal sources
    # when the flag is off, but write_page is also called from admin CLIs
    # and replay paths. Refusing at the write layer means no partner row
    # ever lands in the DB until the flag is explicitly enabled.
    if item.access_scope == "partner_internal" and not partner_internal_enabled():
        raise ValueError(
            f"partner_internal content from source={item.source_id} "
            "refused: BRAIN_PARTNER_INTERNAL_ENABLED is off."
        )

    slug = build_slug(item.source_id, str(item.url))
    truth = item.text or ""
    if not truth.strip():
        logger.info(
            "skip_empty_content",
            extra={"source_id": item.source_id, "url": str(item.url)},
        )
        return slug

    frontmatter = {
        "source_id": item.source_id,
        "source_url": str(item.url),
        "fetched_at": item.fetched_at.isoformat(),
        "tier": item.tier,
        "license": item.license,
        "language": item.language,
    }

    page_input = PageInput(
        type="source_document",
        title=item.title or f"[{item.source_id}] {str(item.url)[:80]}",
        compiled_truth=truth,
        frontmatter=frontmatter,
        content_hash=item.content_hash,
    )

    # One transaction across all three writes. Without this, a crash between
    # put_page and the UPDATE leaves access_scope/partner_id NULL, which RLS
    # treats as public — a partner_internal row would be readable by anyone
    # until the next re-fetch. That's the exact isolation guarantee the
    # brain_pages schema exists to enforce.
    async with conn.transaction():
        await brain.put_page(conn, slug, page_input, owner_uuid=owner_uuid)

        await conn.execute(
            """
            UPDATE brain_pages
            SET language     = COALESCE($2, language),
                license      = COALESCE($3, license),
                source_id    = $4,
                fetched_at   = $5,
                access_scope = $6,
                partner_id   = $7::uuid
            WHERE slug = $1
            """,
            slug,
            item.language,
            item.license,
            item.source_id,
            item.fetched_at,
            item.access_scope,
            item.partner_id,
        )

        await brain.add_timeline_entry(
            conn,
            slug,
            TimelineInput(
                date=item.fetched_at.date(),
                summary=f"Fetched from {item.source_id} ({item.tier})",
                source=item.source_id,
                detail=str(item.url),
            ),
            owner_uuid=owner_uuid,
        )

    return slug
