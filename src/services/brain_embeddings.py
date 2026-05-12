"""Brain embedding pipeline: chunk text and generate vector embeddings.

Chunks brain page content (compiled_truth + timeline) into ~500-token pieces,
calls Ollama's nomic-embed-text (local, sovereign, 768-dim) by default, and
stores via BrainService.upsert_chunks().

A previous version used OpenAI text-embedding-3-large via OpenAI-compatible API.
That broke in prod because OPENAI_API_KEY started pointing at OpenRouter (which
does not host embedding models), producing a 401 storm. Local Ollama avoids
that class of bug entirely and keeps partner_internal brain pages from leaving
the box. Override path remains for any future cloud swap.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
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

# Model config — defaults to local Ollama nomic-embed-text (768-dim, English-strong,
# Apache 2.0). Override via env if you ever need to swap to a cloud provider.
_DEFAULT_PROVIDER = "ollama"  # "ollama" | "openai"
_OLLAMA_DEFAULT_MODEL = "nomic-embed-text"
_OLLAMA_DEFAULT_DIMS = 768
_OPENAI_DEFAULT_MODEL = "text-embedding-3-large"
_OPENAI_DEFAULT_DIMS = 1536
_CHUNK_SIZE = 500  # tokens (~2000 chars)
_CHUNK_OVERLAP = 50  # tokens overlap between chunks


def _resolve_embed_config() -> dict:
    """Return {provider, base_url, api_key, model, dims} for the embeddings client.

    Provider selection:
      1. BRAIN_EMBEDDINGS_PROVIDER env ("ollama" or "openai") wins if set.
      2. Otherwise default to "ollama" (local nomic-embed-text).

    For Ollama: base_url defaults to http://ollama:11434/api/embeddings (the
    docker-internal hostname). Model + dims default to nomic-embed-text/768.

    For OpenAI: requires BRAIN_EMBEDDINGS_API_KEY (must be a real OpenAI key —
    we deliberately do NOT fall back to OPENAI_API_KEY since that points at
    OpenRouter in prod and OpenRouter has no embeddings endpoint).
    """
    provider = (
        os.environ.get("BRAIN_EMBEDDINGS_PROVIDER", "").strip().lower()
        or _DEFAULT_PROVIDER
    )

    if provider == "openai":
        return {
            "provider": "openai",
            "base_url": (
                os.environ.get("BRAIN_EMBEDDINGS_BASE_URL", "").strip()
                or "https://api.openai.com/v1"
            ),
            "api_key": os.environ.get("BRAIN_EMBEDDINGS_API_KEY", ""),
            "model": (
                os.environ.get("BRAIN_EMBEDDINGS_MODEL", "").strip()
                or _OPENAI_DEFAULT_MODEL
            ),
            "dims": int(
                os.environ.get("BRAIN_EMBEDDINGS_DIMS", "").strip()
                or _OPENAI_DEFAULT_DIMS
            ),
        }

    # Default: local Ollama nomic-embed-text
    base = (
        os.environ.get("BRAIN_EMBEDDINGS_BASE_URL", "").strip()
        or os.environ.get("OLLAMA_BASE_URL", "").strip()
        or "http://ollama:11434"
    )
    # Allow OLLAMA_BASE_URL to come in as either "host:port" or "host:port/v1"
    # (the OpenAI-compat suffix). Strip /v1 — we want the native /api/embeddings
    # endpoint here, which doesn't follow OpenAI's REST shape.
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return {
        "provider": "ollama",
        "base_url": base.rstrip("/"),
        "api_key": "",
        "model": (
            os.environ.get("BRAIN_EMBEDDINGS_MODEL", "").strip()
            or _OLLAMA_DEFAULT_MODEL
        ),
        "dims": int(
            os.environ.get("BRAIN_EMBEDDINGS_DIMS", "").strip()
            or _OLLAMA_DEFAULT_DIMS
        ),
    }


def get_embed_dims() -> int:
    """Public accessor for the active embedding dimension. Used by callers who
    need to size pgvector columns or sanity-check vectors before write."""
    return _resolve_embed_config()["dims"]


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


def _ollama_embed_one(base_url: str, model: str, text: str, timeout: int = 60) -> list[float]:
    """Call Ollama /api/embeddings for a single string. Synchronous: Ollama
    serializes inference per model anyway, so async wouldn't buy throughput.
    """
    payload = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request(
        f"{base_url}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())
    emb = body.get("embedding")
    if not isinstance(emb, list) or not emb:
        raise RuntimeError(f"Ollama returned no embedding (model={model}, body keys={list(body.keys())})")
    return [float(x) for x in emb]


async def _get_embeddings(texts: list[str]) -> tuple[list[list[float]], str]:
    """Generate embeddings for a batch of texts.

    Default provider is local Ollama (nomic-embed-text, 768-dim). Override to
    OpenAI by setting BRAIN_EMBEDDINGS_PROVIDER=openai +
    BRAIN_EMBEDDINGS_API_KEY=<real-OpenAI-key>.

    Returns (embeddings, resolved_model_name).
    """
    global _auth_failed_at

    # Hard-disable flag: short-circuits all provider calls. Set this if you
    # ever need to silence the embedding path (e.g. provider outage, debugging).
    if os.environ.get("BRAIN_EMBEDDINGS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        raise RuntimeError("Embeddings disabled via BRAIN_EMBEDDINGS_DISABLED env")

    if _auth_failed_at and (time.monotonic() - _auth_failed_at) < _AUTH_BACKOFF_SECONDS:
        raise RuntimeError(
            "Embeddings disabled: auth failed recently, retrying in %ds"
            % int(_AUTH_BACKOFF_SECONDS - (time.monotonic() - _auth_failed_at))
        )

    cfg = _resolve_embed_config()

    if cfg["provider"] == "ollama":
        # Ollama's /api/embeddings is one-text-per-call. Loop in a thread pool
        # so we don't block the asyncio event loop on the (CPU-bound,
        # single-stream) inference. nomic-embed-text on 8 vCPU CPU is ~50ms
        # per text for chunks of our size, so a 20-chunk page takes ~1s.
        import asyncio

        loop = asyncio.get_event_loop()

        async def _embed_one(t: str) -> list[float]:
            return await loop.run_in_executor(
                None, _ollama_embed_one, cfg["base_url"], cfg["model"], t
            )

        embeddings = await asyncio.gather(*(_embed_one(t) for t in texts))

        # Sanity-check the dim — if Ollama is serving a different model than
        # we expect, fail loud rather than silently writing wrong-dim vectors
        # into pgvector (where a dim mismatch would error on insert anyway).
        if embeddings and len(embeddings[0]) != cfg["dims"]:
            raise RuntimeError(
                f"Ollama embedding dim mismatch: expected {cfg['dims']}, got {len(embeddings[0])} "
                f"(model={cfg['model']}, base_url={cfg['base_url']})"
            )
        return list(embeddings), cfg["model"]

    # OpenAI provider path (kept for the eventual cloud-tier swap)
    from openai import AsyncOpenAI, AuthenticationError

    if not cfg["api_key"]:
        raise RuntimeError(
            "OpenAI embeddings selected but BRAIN_EMBEDDINGS_API_KEY is empty. "
            "Set a real OpenAI key, or remove BRAIN_EMBEDDINGS_PROVIDER to fall "
            "back to the local Ollama default."
        )

    client = AsyncOpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    try:
        response = await client.embeddings.create(
            model=cfg["model"], input=texts, dimensions=cfg["dims"],
        )
    except AuthenticationError:
        _auth_failed_at = time.monotonic()
        logger.error(
            "Embeddings auth failed against %s, disabling for %ds.",
            cfg["base_url"], _AUTH_BACKOFF_SECONDS,
        )
        raise

    return [item.embedding for item in response.data], cfg["model"]


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

    # expand_query is a CHAT call, not an embedding call. Use the same LLM
    # client config the rest of Sage uses (OPENAI_API_KEY/OPENAI_BASE_URL),
    # which currently routes via OpenRouter or local Ollama. The embedding
    # provider is independent and may be a different service.
    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    chat_model = os.environ.get("BRAIN_QUERY_EXPANSION_MODEL", "").strip() or os.environ.get(
        "OPENAI_MODEL", "gpt-4.1-nano"
    )
    if not api_key:
        return [query]

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    try:
        resp = await client.chat.completions.create(
            model=chat_model,
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
    if os.environ.get("BRAIN_EMBEDDINGS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return {"embedded": 0, "skipped": 0, "errors": 0, "disabled_by_env": True}
    if _auth_failed_at and (time.monotonic() - _auth_failed_at) < _AUTH_BACKOFF_SECONDS:
        return {"embedded": 0, "skipped": 0, "errors": 0, "auth_disabled": True}

    # Find pages that have content but no chunks with embeddings.
    # Match brain_service.get_page's partner-aware filter so this SELECT only
    # returns rows the same connection can actually resolve. Without this,
    # partner_internal pages slip through to embed_page() where get_page
    # filters them out and we log "page not found" forever (the rows never
    # get embeddings, so the next tick finds them again — infinite WARN
    # loop). The maintenance + hook-processor connections run with empty
    # app.partner_id, so this excludes partner_internal rows from them and
    # leaves those rows for embedding inside the partner's own session.
    rows = await conn.fetch(
        """
        SELECT p.slug FROM brain_pages p
        WHERE (p.compiled_truth != '' OR p.timeline != '')
          AND (
              p.access_scope IS NULL
              OR p.access_scope = 'public'
              OR (p.access_scope = 'partner_internal'
                  AND p.partner_id IS NOT NULL
                  AND p.partner_id::text = coalesce(current_setting('app.partner_id', true), ''))
          )
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
