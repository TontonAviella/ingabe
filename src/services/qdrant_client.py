"""Qdrant client + collection schema for Phase 2 Clay tile embedding similarity search.

Replaces the previous Milvus standalone deployment. Qdrant gives us a single Rust
binary, no etcd/minio dependencies, simpler ops, and better payload-filtered search
at the scale we operate at (~1M vectors).

Stores per-tile embeddings (1024-dim from Clay v1.5 cls_token) tagged with
(layer_id, partner_id, tile_x, tile_y, captured_at). Searches are filtered by
partner_id as a defense-in-depth gate alongside the existing RLS pattern on
map_layers, plus the tool layer adds a layer_id IN (visible_layers) filter to
be doubly sure.

The collection is created lazily on first ensure_clay_tiles_collection() call
and survives container restarts via the qdrant_data named volume.

Public API mirrors the old milvus_client.py 1:1 to keep call sites unchanged:
    get_qdrant_client()
    ensure_clay_tiles_collection()
    insert_tile_embeddings(layer_id, partner_id, owner_uuid, captured_at_epoch, zoom, rows)
    delete_layer_embeddings(layer_id)
    search_similar_tiles(query_embedding, visible_layer_ids, top_k, partner_id)
    get_layer_embedding_count(layer_id)
"""

import logging
import os
import uuid as _uuid
from typing import Optional

logger = logging.getLogger(__name__)

# Qdrant connection — lives on the docker network
_QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")
_QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
_QDRANT_GRPC_PORT = int(os.environ.get("QDRANT_GRPC_PORT", "6334"))
_QDRANT_PREFER_GRPC = os.environ.get("QDRANT_PREFER_GRPC", "true").lower() in ("1", "true", "yes")

# Collection name + schema constants. Kept for backwards compatibility with
# imports across the codebase that reference COLLECTION_CLAY_TILES.
COLLECTION_CLAY_TILES = "clay_tiles_v1"
EMBEDDING_DIM = 1024  # Clay v1.5 cls_token output dimension

_client = None
_collection_ready = False


def get_qdrant_client():
    """Connect to Qdrant (idempotent). Reuses one global client per process."""
    global _client
    if _client is not None:
        return _client
    from qdrant_client import QdrantClient

    _client = QdrantClient(
        host=_QDRANT_HOST,
        port=_QDRANT_PORT,
        grpc_port=_QDRANT_GRPC_PORT,
        prefer_grpc=_QDRANT_PREFER_GRPC,
        timeout=30,
    )
    return _client


# Backwards-compatible alias so existing call sites that imported
# `get_milvus_client` keep working through a one-line shim.
def get_milvus_client():
    return get_qdrant_client()


def ensure_clay_tiles_collection():
    """Create the clay_tiles_v1 collection if it doesn't exist. Idempotent."""
    global _collection_ready
    from qdrant_client.models import (
        Distance, VectorParams, PayloadSchemaType, HnswConfigDiff,
    )

    client = get_qdrant_client()
    if _collection_ready:
        return client

    existing = {c.name for c in client.get_collections().collections}
    if COLLECTION_CLAY_TILES in existing:
        _collection_ready = True
        return client

    client.create_collection(
        collection_name=COLLECTION_CLAY_TILES,
        vectors_config=VectorParams(
            size=EMBEDDING_DIM,
            distance=Distance.COSINE,
        ),
        hnsw_config=HnswConfigDiff(m=16, ef_construct=200),
    )

    # Payload indexes for the fields we filter by. Without these, Qdrant
    # falls back to brute-force filtering and similarity search collapses
    # to O(N) once we have >100K tiles.
    for field_name, schema in [
        ("layer_id", PayloadSchemaType.KEYWORD),
        ("partner_id", PayloadSchemaType.KEYWORD),
        ("owner_uuid", PayloadSchemaType.KEYWORD),
    ]:
        try:
            client.create_payload_index(
                collection_name=COLLECTION_CLAY_TILES,
                field_name=field_name,
                field_schema=schema,
            )
        except Exception as e:
            # Index may already exist on retry; safe to ignore.
            logger.debug("payload index for %s exists or failed: %s", field_name, e)

    _collection_ready = True
    logger.info(
        "Created Qdrant collection %s (size=%d, distance=COSINE, HNSW m=16)",
        COLLECTION_CLAY_TILES, EMBEDDING_DIM,
    )
    return client


def _tile_point_id(layer_id: str, tile_x: int, tile_y: int, zoom: int) -> str:
    """Deterministic UUID per (layer, tile) so re-embeds upsert cleanly.

    Qdrant requires point IDs to be UUIDs or unsigned ints. Hashing a stable
    string into a UUID5 means the same tile always gets the same ID, so a
    second embed pass overwrites instead of duplicating.
    """
    name = f"{layer_id}:{zoom}:{tile_x}:{tile_y}"
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, name))


def insert_tile_embeddings(
    layer_id: str,
    partner_id: Optional[str],
    owner_uuid: str,
    captured_at_epoch: int,
    zoom: int,
    rows: list[dict],
) -> int:
    """Bulk-upsert tile rows. Each row: {embedding, tile_x, tile_y, bbox_wgs84}.
    Returns the number of points written.
    """
    if not rows:
        return 0
    from qdrant_client.models import PointStruct

    ensure_clay_tiles_collection()
    client = get_qdrant_client()

    points = []
    for r in rows:
        tile_x = int(r["tile_x"])
        tile_y = int(r["tile_y"])
        points.append(
            PointStruct(
                id=_tile_point_id(layer_id, tile_x, tile_y, zoom),
                vector=list(r["embedding"]),
                payload={
                    "layer_id": layer_id,
                    "partner_id": partner_id or "",
                    "owner_uuid": owner_uuid,
                    "tile_x": tile_x,
                    "tile_y": tile_y,
                    "zoom": int(zoom),
                    "captured_at": int(captured_at_epoch),
                    "bbox_wgs84": [float(v) for v in r["bbox_wgs84"]],
                },
            )
        )

    # wait=True so the call returns only after the write is durable, matching
    # the old Milvus flush() behavior. At ingest scale (500-2000 tiles per
    # layer) this adds ~50ms; trading throughput for read-after-write safety.
    client.upsert(
        collection_name=COLLECTION_CLAY_TILES,
        points=points,
        wait=True,
    )
    return len(points)


def delete_layer_embeddings(layer_id: str) -> int:
    """Drop all rows for a layer. Used when a layer is deleted or re-ingested."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector

    ensure_clay_tiles_collection()
    client = get_qdrant_client()

    selector = FilterSelector(
        filter=Filter(
            must=[FieldCondition(key="layer_id", match=MatchValue(value=layer_id))]
        )
    )
    res = client.delete(
        collection_name=COLLECTION_CLAY_TILES,
        points_selector=selector,
        wait=True,
    )
    return getattr(res, "operation_id", 0) or 0


def search_similar_tiles(
    query_embedding: list[float],
    visible_layer_ids: list[str],
    top_k: int = 10,
    partner_id: Optional[str] = None,
) -> list[dict]:
    """Find tiles most similar to query_embedding, restricted to layers the
    caller can see and optionally filtered by partner_id (defense-in-depth).

    Returns list of {layer_id, tile_x, tile_y, captured_at, distance, bbox_wgs84}
    sorted by descending similarity (most similar first). Note: Qdrant returns
    cosine SIMILARITY (1.0 = identical) directly, not distance. We expose it
    under the key "distance" to keep the same shape callers already consume,
    but the value is similarity.
    """
    if not visible_layer_ids:
        return []
    from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny

    ensure_clay_tiles_collection()
    client = get_qdrant_client()

    must = [
        FieldCondition(
            key="layer_id",
            match=MatchAny(any=list(visible_layer_ids[:1000])),
        ),
    ]
    if partner_id:
        must.append(
            FieldCondition(
                key="partner_id",
                match=MatchValue(value=partner_id),
            )
        )

    # qdrant-client 1.16+ removed `client.search()` in favor of the universal
    # `client.query_points()` endpoint. Returns a QueryResponse with .points,
    # each entry is a ScoredPoint with .score (cosine similarity, 1.0 = identical),
    # .payload (dict), .id, .vector. Same semantic shape as old search hits.
    response = client.query_points(
        collection_name=COLLECTION_CLAY_TILES,
        query=list(query_embedding),
        query_filter=Filter(must=must),
        limit=top_k,
        with_payload=True,
        with_vectors=False,
    )

    out: list[dict] = []
    for h in response.points:
        p = h.payload or {}
        out.append(
            {
                "layer_id": p.get("layer_id"),
                "tile_x": p.get("tile_x"),
                "tile_y": p.get("tile_y"),
                "captured_at": p.get("captured_at"),
                "bbox_wgs84": list(p.get("bbox_wgs84") or []),
                "distance": float(h.score),  # cosine similarity, 1.0 = identical
            }
        )
    return out


def get_tiles_for_layer(layer_id: str, limit: int = 2000) -> list[dict]:
    """Return all tile points for a given layer with embeddings + payload.

    Used by find_similar_tiles to look up the source tile that contains the
    query point. Tiles per layer are ~500-2000 so a single scroll page is
    sufficient.
    """
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    ensure_clay_tiles_collection()
    client = get_qdrant_client()

    out: list[dict] = []
    next_offset = None
    fetched = 0
    while True:
        page, next_offset = client.scroll(
            collection_name=COLLECTION_CLAY_TILES,
            scroll_filter=Filter(
                must=[FieldCondition(key="layer_id", match=MatchValue(value=layer_id))]
            ),
            limit=min(256, limit - fetched),
            with_payload=True,
            with_vectors=True,
            offset=next_offset,
        )
        for pt in page:
            p = pt.payload or {}
            out.append(
                {
                    "tile_x": p.get("tile_x"),
                    "tile_y": p.get("tile_y"),
                    "bbox_wgs84": list(p.get("bbox_wgs84") or []),
                    "embedding": list(pt.vector or []),
                }
            )
        fetched = len(out)
        if not next_offset or fetched >= limit:
            break
    return out


def get_layer_embedding_count(layer_id: str) -> int:
    """Count of tile embeddings persisted for a layer (0 = not yet embedded)."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    ensure_clay_tiles_collection()
    client = get_qdrant_client()
    res = client.count(
        collection_name=COLLECTION_CLAY_TILES,
        count_filter=Filter(
            must=[FieldCondition(key="layer_id", match=MatchValue(value=layer_id))]
        ),
        exact=True,
    )
    return int(getattr(res, "count", 0))
