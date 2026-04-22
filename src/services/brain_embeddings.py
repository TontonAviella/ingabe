"""Brain embedding pipeline: chunk text and generate vector embeddings.

Chunks brain page content (compiled_truth + timeline) into ~500-token pieces,
calls text-embedding-3-large via OpenAI-compatible API (OpenRouter, OpenAI, etc.),
and stores via BrainService.upsert_chunks().
"""

from __future__ import annotations

import logging
import os
import re
import textwrap
import time
from collections import Counter
from typing import Optional

import asyncpg
import numpy as np

from src.services.brain_service import BrainService, ChunkInput

logger = logging.getLogger(__name__)

_auth_failed_at: float = 0.0
_AUTH_BACKOFF_SECONDS = 600
_EXPAND_CACHE: dict[str, tuple[float, list[str]]] = {}
_EXPAND_CACHE_TTL = 300  # 5 minutes

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


_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+|\n{2,}')


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences on punctuation boundaries or double newlines."""
    parts = _SENTENCE_RE.split(text)
    return [s.strip() for s in parts if s.strip()]


def _bow_vectors(sentences: list[str]) -> np.ndarray:
    """Build bag-of-words matrix for sentences. Cheap local similarity signal."""
    vocab: dict[str, int] = {}
    for s in sentences:
        for w in s.lower().split():
            if w not in vocab:
                vocab[w] = len(vocab)
    if not vocab:
        return np.zeros((len(sentences), 1))
    mat = np.zeros((len(sentences), len(vocab)))
    for i, s in enumerate(sentences):
        counts = Counter(s.lower().split())
        for w, c in counts.items():
            if w in vocab:
                mat[i, vocab[w]] = c
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _cosine_similarities(vecs: np.ndarray) -> list[float]:
    """Cosine similarity between adjacent row vectors."""
    sims = []
    for i in range(len(vecs) - 1):
        sims.append(float(np.dot(vecs[i], vecs[i + 1])))
    return sims


def _find_boundaries(sims: list[float]) -> list[int]:
    """Find topic boundaries where similarity drops significantly.

    Uses local minima detection: a boundary exists where similarity drops
    below the mean minus one standard deviation (or at zero-similarity gaps).
    """
    if not sims:
        return []
    arr = np.array(sims)
    mean = float(arr.mean())
    std = float(arr.std())
    threshold = max(mean - std, 0.01)
    boundaries = []
    for i, s in enumerate(sims):
        if s <= threshold:
            boundaries.append(i + 1)
    return boundaries


def chunk_text(text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Semantic chunking: split on topic boundaries, fall back to size-based split.

    Uses bag-of-words cosine similarity between adjacent sentences to detect
    topic shifts (cheap local approximation of gbrain's Savitzky-Golay approach).
    Merges small segments to respect chunk_size targets.
    """
    if not text or not text.strip():
        return []

    char_size = chunk_size * 4

    if len(text) <= char_size:
        return [text.strip()]

    sentences = _split_sentences(text)

    # Fall back to size-based splitting for very few sentences
    if len(sentences) < 4:
        return _chunk_text_fixed(text, chunk_size, overlap)

    # Compute similarity between adjacent sentences
    vecs = _bow_vectors(sentences)
    sims = _cosine_similarities(vecs)
    boundaries = _find_boundaries(sims)
    if not boundaries:
        return _chunk_text_fixed(text, chunk_size, overlap)

    # Build segments from boundaries
    segments: list[list[str]] = []
    prev = 0
    for b in boundaries:
        if b > prev:
            segments.append(sentences[prev:b])
        prev = b
    if prev < len(sentences):
        segments.append(sentences[prev:])

    # Merge small segments to meet chunk_size target, split large ones
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for seg in segments:
        seg_text = " ".join(seg)
        seg_len = len(seg_text)

        if current_len + seg_len <= char_size:
            current.extend(seg)
            current_len += seg_len + 1
        else:
            if current:
                chunks.append(" ".join(current).strip())
            if seg_len <= char_size:
                current = list(seg)
                current_len = seg_len
            else:
                # Segment too large, split it with the fixed method
                for sub in _chunk_text_fixed(seg_text, chunk_size, overlap):
                    chunks.append(sub)
                current = []
                current_len = 0

    if current:
        chunks.append(" ".join(current).strip())

    return [c for c in chunks if c]


def _chunk_text_fixed(text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Fixed-size chunking with sentence boundary heuristic (original method)."""
    char_size = chunk_size * 4
    char_overlap = overlap * 4

    if len(text) <= char_size:
        return [text.strip()]

    chunks = []
    start = 0
    while start < len(text):
        end = start + char_size

        if end < len(text):
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


async def _get_embeddings(texts: list[str]) -> tuple[list[list[float]], str]:
    """Call embeddings API (OpenAI, OpenRouter, or compatible). Returns (embeddings, resolved_model)."""
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

    return [item.embedding for item in response.data], model


async def expand_query(query: str, n_variants: int = 3) -> list[str]:
    """Generate search query variants via LLM for multi-query expansion.

    Returns the original query plus up to n_variants rewrites.
    Falls back to [query] on any failure (LLM down, bad key, timeout).
    Results cached for 5 minutes to avoid repeat LLM calls for identical queries.
    """
    global _auth_failed_at

    cache_key = query.strip().lower()
    cached = _EXPAND_CACHE.get(cache_key)
    if cached:
        ts, variants = cached
        if (time.monotonic() - ts) < _EXPAND_CACHE_TTL:
            return variants

    from openai import AsyncOpenAI, AuthenticationError

    if _auth_failed_at and (time.monotonic() - _auth_failed_at) < _AUTH_BACKOFF_SECONDS:
        return [query]

    api_key, base_url, _ = _resolve_embed_config()
    if not api_key:
        return [query]

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    try:
        resp = await client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Generate {n_variants} alternative search queries for a knowledge base. "
                        "Each should capture a different angle or phrasing of the same intent. "
                        "Return ONLY the queries, one per line, no numbering or bullets."
                    ),
                },
                {"role": "user", "content": query},
            ],
            temperature=0.7,
            max_tokens=200,
        )
    except AuthenticationError:
        _auth_failed_at = time.monotonic()
        return [query]
    except Exception:
        logger.debug("Multi-query expansion failed, using original query")
        return [query]

    raw = (resp.choices[0].message.content or "").strip()
    variants = [line.strip() for line in raw.splitlines() if line.strip()]
    result = [query] + variants[:n_variants]
    _EXPAND_CACHE[cache_key] = (time.monotonic(), result)
    if len(_EXPAND_CACHE) > 256:
        oldest = min(_EXPAND_CACHE, key=lambda k: _EXPAND_CACHE[k][0])
        del _EXPAND_CACHE[oldest]
    return result


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
    embeddings, resolved_model = await _get_embeddings(chunks)

    # Build ChunkInput list
    chunk_inputs = []
    for i, (text, embedding) in enumerate(zip(chunks, embeddings)):
        chunk_inputs.append(
            ChunkInput(
                chunk_index=i,
                chunk_text=text,
                chunk_source="compiled_truth",
                embedding=embedding,
                model=resolved_model,
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
