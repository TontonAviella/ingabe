"""Brain embedding pipeline: chunk text and generate vector embeddings.

Chunks brain page content (compiled_truth + timeline) into ~500-token pieces,
calls an OpenAI-compatible embeddings endpoint (default: self-hosted Ollama
serving nomic-embed-text), and stores via BrainService.upsert_chunks().

Provider is configured via EMBEDDING_BASE_URL / EMBEDDING_API_KEY /
EMBEDDING_MODEL so embeddings don't share a credential with the chat LLM.
"""

from __future__ import annotations

import logging
import os
import textwrap
from typing import Literal, Optional

import asyncpg

from src.services.brain_service import BrainService, ChunkInput

logger = logging.getLogger(__name__)

# Model config. Overridable so dev can point at a different provider (e.g. OpenAI
# text-embedding-3-large) without code changes; prod runs nomic-embed-text via Ollama.
_EMBED_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
# Nomic-embed-text native dim; text-embedding-3-large is 1536. Kept for reference —
# not sent to the API (Ollama's /v1/embeddings rejects the `dimensions` kwarg).
_EMBED_DIMS = 768 if "nomic" in _EMBED_MODEL.lower() else 1536
# Models trained for asymmetric retrieval (nomic, e5, bge) need a task prefix.
_NEEDS_RETRIEVAL_PREFIX = any(tag in _EMBED_MODEL.lower() for tag in ("nomic", "e5", "bge"))
_CHUNK_SIZE = 500  # tokens (~2000 chars)
_CHUNK_OVERLAP = 50  # tokens overlap between chunks


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


async def _get_embeddings(
    texts: list[str],
    *,
    kind: Literal["document", "query"] = "document",
) -> list[list[float]]:
    """Call the configured embeddings endpoint. Returns one vector per input.

    `kind` selects the retrieval role for asymmetric models (nomic/e5/bge),
    which prepend "search_document: " or "search_query: " to each input. Using
    the wrong prefix (or none) silently tanks retrieval quality. For symmetric
    models (OpenAI) the prefix is skipped.
    """
    from openai import AsyncOpenAI

    base_url = (
        os.environ.get("EMBEDDING_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    )
    api_key = os.environ.get("EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    if not api_key:
        raise RuntimeError(
            "EMBEDDING_API_KEY (or OPENAI_API_KEY) not set, cannot generate embeddings"
        )

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    if _NEEDS_RETRIEVAL_PREFIX:
        prefix = "search_query: " if kind == "query" else "search_document: "
        payload = [prefix + t for t in texts]
    else:
        payload = texts

    response = await client.embeddings.create(
        model=_EMBED_MODEL,
        input=payload,
    )

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
                model=_EMBED_MODEL,
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
