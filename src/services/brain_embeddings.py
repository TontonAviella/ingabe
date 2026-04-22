"""Brain embedding pipeline: chunk text and generate vector embeddings.

Chunks brain page content (compiled_truth + timeline) into ~500-token pieces,
calls text-embedding-3-large via OpenAI-compatible API (OpenRouter, OpenAI, etc.),
and stores via BrainService.upsert_chunks().
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import asyncpg

from src.services.brain_service import BrainService, ChunkInput

logger = logging.getLogger(__name__)

_auth_failed_at: float = 0.0
_AUTH_BACKOFF_SECONDS = 600

# Model config
_OPENAI_EMBED_MODEL = "text-embedding-3-large"
_EMBED_DIMS = 1536
_CHUNK_SIZE = 500  # tokens (~2000 chars)
_CHUNK_OVERLAP = 50  # tokens overlap between chunks


def _resolve_embed_config() -> tuple[str, str, str]:
    """Return (api_key, base_url, model) for the embeddings client.

    Supports two configurations:
      1. Dedicated: BRAIN_EMBEDDINGS_API_KEY + BRAIN_EMBEDDINGS_BASE_URL
      2. Direct OpenAI: OPENAI_API_KEY + api.openai.com/v1 (default)
    OPENAI_BASE_URL is intentionally NOT used — it points to OpenRouter
    in prod, which cannot serve embedding models.
    """
    api_key = (
        os.environ.get("BRAIN_EMBEDDINGS_API_KEY")
        or os.environ.get("OPENAI_API_KEY", "")
    )
    base_url = (
        os.environ.get("BRAIN_EMBEDDINGS_BASE_URL")
        or "https://api.openai.com/v1"
    )
    model = _OPENAI_EMBED_MODEL
    if "openrouter.ai" in base_url:
        model = f"openai/{_OPENAI_EMBED_MODEL}"
    return api_key, base_url, model


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def chunk_text(text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Split text into chunks of approximately chunk_size tokens with overlap."""
    if not text or not text.strip():
        return []

    char_size = chunk_size * 4
    char_overlap = overlap * 4

    if len(text) <= char_size:
        return [text.strip()]

    chunks = []
    start = 0
    while start < len(text):
        end = start + char_size

        # Try to break at sentence boundary
        if end < len(text):
            # Look for sentence end within last 20% of chunk
            search_start = max(start, end - char_size // 5)
            last_period = text.rfind(". ", search_start, end)
            last_newline = text.rfind("\n", search_start, end)
            break_at = max(last_period, last_newline)
            if break_at > search_start:
                end = break_at + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        start = end - char_overlap
        if start >= len(text):
            break

    return chunks


async def _get_embeddings(texts: list[str]) -> list[list[float]]:
    """Call embeddings API (OpenAI, OpenRouter, or compatible). Returns list of embedding vectors."""
    global _auth_failed_at
    from openai import AsyncOpenAI, AuthenticationError

    if _auth_failed_at and (time.monotonic() - _auth_failed_at) < _AUTH_BACKOFF_SECONDS:
        raise RuntimeError("Embeddings disabled: auth failed recently, retrying in %ds" % int(
            _AUTH_BACKOFF_SECONDS - (time.monotonic() - _auth_failed_at)))

    api_key, base_url, model = _resolve_embed_config()
    if not api_key:
        raise RuntimeError("No API key for embeddings (set BRAIN_EMBEDDINGS_API_KEY or OPENAI_API_KEY)")

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    try:
        response = await client.embeddings.create(
            model=model,
            input=texts,
            dimensions=_EMBED_DIMS,
        )
    except AuthenticationError:
        _auth_failed_at = time.monotonic()
        logger.error(
            "Embeddings auth failed against %s, disabling for %ds.",
            base_url, _AUTH_BACKOFF_SECONDS,
        )
        raise

    return [item.embedding for item in response.data]


async def embed_page(
    conn: asyncpg.Connection,
    brain: BrainService,
    slug: str,
) -> int:
    """Chunk and embed a single brain page. Returns number of chunks created."""
    page = await brain.get_page(conn, slug)
    if not page:
        logger.warning("embed_page: page '%s' not found", slug)
        return 0

    # Build text to embed: compiled_truth + timeline
    parts = []
    if page.compiled_truth:
        parts.append(page.compiled_truth)
    if page.timeline:
        parts.append(page.timeline)

    # Also pull timeline entries
    timeline_entries = await brain.get_timeline(conn, slug, limit=50)
    for entry in timeline_entries:
        parts.append(f"{entry['date']}: {entry['summary']}")

    full_text = "\n\n".join(parts)
    if not full_text.strip():
        return 0

    chunks = chunk_text(full_text)
    if not chunks:
        return 0

    # Get embeddings in one batch
    embeddings = await _get_embeddings(chunks)

    # Build ChunkInput list
    chunk_inputs = []
    for i, (text, embedding) in enumerate(zip(chunks, embeddings)):
        chunk_inputs.append(
            ChunkInput(
                chunk_index=i,
                chunk_text=text,
                chunk_source="compiled_truth",
                embedding=embedding,
                model=_OPENAI_EMBED_MODEL,
                token_count=_estimate_tokens(text),
            )
        )

    await brain.upsert_chunks(conn, slug, chunk_inputs)
    logger.info("Embedded page '%s': %d chunks", slug, len(chunk_inputs))
    return len(chunk_inputs)


async def embed_all_stale(
    conn: asyncpg.Connection,
    brain: BrainService,
    limit: int = 50,
) -> dict:
    """Find pages with content but no embeddings, and embed them.

    Returns: {embedded: int, skipped: int, errors: int}
    """
    if _auth_failed_at and (time.monotonic() - _auth_failed_at) < _AUTH_BACKOFF_SECONDS:
        return {"embedded": 0, "skipped": 0, "errors": 0, "auth_disabled": True}

    # Find pages that have content but no chunks with embeddings
    rows = await conn.fetch(
        """
        SELECT p.slug FROM brain_pages p
        WHERE (p.compiled_truth != '' OR p.timeline != '')
          AND NOT EXISTS (
              SELECT 1 FROM brain_content_chunks cc
              WHERE cc.page_id = p.id AND cc.embedded_at IS NOT NULL
          )
        ORDER BY p.updated_at DESC
        LIMIT $1
        """,
        limit,
    )

    embedded = 0
    skipped = 0
    errors = 0

    for row in rows:
        if _auth_failed_at and (time.monotonic() - _auth_failed_at) < _AUTH_BACKOFF_SECONDS:
            break
        try:
            n = await embed_page(conn, brain, row["slug"])
            if n > 0:
                embedded += 1
            else:
                skipped += 1
        except Exception:
            logger.exception("Failed to embed page '%s'", row["slug"])
            errors += 1

    return {"embedded": embedded, "skipped": skipped, "errors": errors}
