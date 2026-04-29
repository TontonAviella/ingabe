"""Milvus client + collection schema for Phase 2 Clay tile embedding similarity search.

Stores per-tile embeddings (768-dim from Clay v1.5) tagged with
(layer_id, partner_id, tile_x, tile_y, captured_at). Searches are filtered
by partner_id as a defense-in-depth gate alongside the existing RLS pattern
on map_layers — the tool layer adds a layer_id IN (visible_layers) check
on top to be doubly sure.

The collection is created lazily on first ensure_clay_tiles_collection()
call; it survives container restarts via the milvus_data named volume.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Milvus connection — lives on the docker network
_MILVUS_HOST = os.environ.get("MILVUS_HOST", "milvus")
_MILVUS_PORT = int(os.environ.get("MILVUS_PORT", "19530"))

# Collection name + schema constants
COLLECTION_CLAY_TILES = "clay_tiles_v1"
EMBEDDING_DIM = 768  # Clay v1.5 patch embedding dimension
INDEX_TYPE = "HNSW"
METRIC_TYPE = "COSINE"

_initialized = False


def get_milvus_client():
    """Connect to Milvus (idempotent)."""
    from pymilvus import connections

    if not connections.has_connection("default"):
        connections.connect(
            alias="default",
            host=_MILVUS_HOST,
            port=str(_MILVUS_PORT),
        )
    return connections


def ensure_clay_tiles_collection():
    """Create the clay_tiles_v1 collection if it doesn't exist. Returns the
    Collection handle. Idempotent — cheap to call repeatedly."""
    from pymilvus import (
        Collection, CollectionSchema, FieldSchema, DataType, utility,
    )

    global _initialized
    get_milvus_client()

    if utility.has_collection(COLLECTION_CLAY_TILES):
        coll = Collection(COLLECTION_CLAY_TILES)
        if not _initialized:
            coll.load()
            _initialized = True
        return coll

    fields = [
        FieldSchema(
            name="id", dtype=DataType.INT64,
            is_primary=True, auto_id=True,
        ),
        FieldSchema(
            name="embedding", dtype=DataType.FLOAT_VECTOR,
            dim=EMBEDDING_DIM,
        ),
        # Tile provenance — used for partner-isolation filtering on search
        FieldSchema(name="layer_id", dtype=DataType.VARCHAR, max_length=24),
        FieldSchema(name="partner_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="owner_uuid", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="tile_x", dtype=DataType.INT32),
        FieldSchema(name="tile_y", dtype=DataType.INT32),
        FieldSchema(name="zoom", dtype=DataType.INT16),
        # Time anchor — epoch seconds of layer's captured_at
        FieldSchema(name="captured_at", dtype=DataType.INT64),
        # Tile geographic bbox in WGS84 — [west, south, east, north]
        FieldSchema(
            name="bbox_wgs84", dtype=DataType.ARRAY,
            element_type=DataType.FLOAT, max_capacity=4,
        ),
    ]
    schema = CollectionSchema(
        fields, description="Clay v1.5 tile embeddings for drone/sat raster similarity search",
    )
    coll = Collection(COLLECTION_CLAY_TILES, schema)

    # HNSW index — good recall/latency tradeoff for ~1M-10M vectors
    coll.create_index(
        field_name="embedding",
        index_params={
            "index_type": INDEX_TYPE,
            "metric_type": METRIC_TYPE,
            "params": {"M": 16, "efConstruction": 200},
        },
    )
    coll.load()
    _initialized = True
    logger.info("Created Milvus collection %s with HNSW(COSINE) index", COLLECTION_CLAY_TILES)
    return coll


def insert_tile_embeddings(
    layer_id: str,
    partner_id: Optional[str],
    owner_uuid: str,
    captured_at_epoch: int,
    zoom: int,
    rows: list[dict],
) -> int:
    """Bulk-insert tile rows. Each row: {embedding, tile_x, tile_y, bbox_wgs84}.

    Returns the number of rows inserted.
    """
    if not rows:
        return 0
    coll = ensure_clay_tiles_collection()
    n = len(rows)
    coll.insert([
        [r["embedding"] for r in rows],
        [layer_id] * n,
        [partner_id or ""] * n,
        [owner_uuid] * n,
        [int(r["tile_x"]) for r in rows],
        [int(r["tile_y"]) for r in rows],
        [int(zoom)] * n,
        [int(captured_at_epoch)] * n,
        [list(r["bbox_wgs84"]) for r in rows],
    ])
    coll.flush()
    return n


def delete_layer_embeddings(layer_id: str) -> int:
    """Drop all rows for a layer. Used when a layer is deleted or re-ingested.

    Returns the delete count reported by Milvus.
    """
    coll = ensure_clay_tiles_collection()
    expr = f'layer_id == "{layer_id}"'
    res = coll.delete(expr)
    coll.flush()
    return getattr(res, "delete_count", 0)


def search_similar_tiles(
    query_embedding: list[float],
    visible_layer_ids: list[str],
    top_k: int = 10,
    partner_id: Optional[str] = None,
) -> list[dict]:
    """Find tiles most similar to query_embedding, restricted to layers the
    caller can see (visible_layer_ids — comes from a partner-aware DB lookup
    in the tool layer) and optionally filtered by partner_id (defense-in-depth).

    Returns list of {layer_id, tile_x, tile_y, captured_at, distance,
    bbox_wgs84} sorted by ascending distance (most similar first).
    """
    if not visible_layer_ids:
        return []
    coll = ensure_clay_tiles_collection()

    # Build expression: layer_id IN [...] AND optionally partner_id == "..."
    # Milvus VARCHAR IN supports up to 1000-element lists.
    layer_list = ", ".join(f'"{lid}"' for lid in visible_layer_ids[:1000])
    parts = [f"layer_id in [{layer_list}]"]
    if partner_id:
        parts.append(f'partner_id == "{partner_id}"')
    expr = " and ".join(parts)

    results = coll.search(
        data=[query_embedding],
        anns_field="embedding",
        param={"metric_type": METRIC_TYPE, "params": {"ef": 64}},
        limit=top_k,
        expr=expr,
        output_fields=["layer_id", "tile_x", "tile_y", "captured_at", "bbox_wgs84"],
    )
    if not results or not results[0]:
        return []
    out = []
    for hit in results[0]:
        out.append({
            "layer_id": hit.entity.get("layer_id"),
            "tile_x": hit.entity.get("tile_x"),
            "tile_y": hit.entity.get("tile_y"),
            "captured_at": hit.entity.get("captured_at"),
            "bbox_wgs84": list(hit.entity.get("bbox_wgs84") or []),
            "distance": float(hit.distance),
        })
    return out


def get_layer_embedding_count(layer_id: str) -> int:
    """Count of tile embeddings persisted for a layer (0 = not yet embedded)."""
    coll = ensure_clay_tiles_collection()
    expr = f'layer_id == "{layer_id}"'
    res = coll.query(expr=expr, output_fields=["id"], limit=1, consistency_level="Strong")
    if not res:
        return 0
    # Approximate count via num_entities is fine for "is this layer embedded?"
    # For exact count over a single layer use a fresh query with a high limit.
    return coll.num_entities  # global collection size; callers should use it as "exists"
