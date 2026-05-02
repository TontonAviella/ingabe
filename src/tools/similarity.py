"""Phase 2 Sage tool: find tiles visually similar to a query point in a
user-uploaded raster, across ALL the user's other rasters.

Powered by Clay v1.5 embeddings stored in Qdrant. The query is a (layer_id,
longitude, latitude) point. We look up the embedding of the tile that
contains the query point, then run cosine similarity search in Qdrant
restricted to layers the partner can see.

Use case: "find other fields in my flights that look like this damaged
patch" / "have we seen this stress pattern in any of my other flights?".
Returns list of {layer_id, layer_name, captured_at, similarity, tile_bbox}
sorted by descending similarity.
"""

import logging

from pydantic import BaseModel, Field

from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)


class FindSimilarTilesArgs(BaseModel):
    layer_id: str = Field(
        ...,
        description="The layer_id of the user-uploaded raster the query point lives in.",
    )
    longitude: float = Field(
        ...,
        description="Longitude in WGS84 decimal degrees of the query point.",
    )
    latitude: float = Field(
        ...,
        description="Latitude in WGS84 decimal degrees of the query point.",
    )
    top_k: int = Field(
        ...,
        description=(
            "How many similar tiles to return. Use 5-10 for a quick scan, up to 50 "
            "for a thorough audit. Pass 0 to use the default of 10."
        ),
    )


async def find_similar_tiles(
    args: FindSimilarTilesArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Find tiles visually similar to a point in a user's raster, across all of that user's other rasters. Powered by Clay v1.5 visual embeddings in Qdrant. Use this when the user asks 'find other fields that look like this stressed patch', 'have we seen this damage pattern in any other flight?', 'show me similar areas across my orthophotos', or any cross-flight visual similarity question. Returns top-K similar tiles ranked by cosine similarity (1.0 = identical, lower = less similar). For exact-match analysis at a single point use read_pixel_at; for whole-field health verdict use interpret_raster_health. Only works on rgb_visual rasters that have been embedded (orthophotos auto-embed after COG conversion completes)."""
    from src.structures import get_async_read_connection
    from src.services.qdrant_client import (
        ensure_clay_tiles_collection, search_similar_tiles,
        get_tiles_for_layer,
    )

    top_k = args.top_k if args.top_k > 0 else 10
    top_k = max(1, min(int(top_k), 50))

    # 1. Verify layer ownership + bounds + that embeddings exist.
    async with get_async_read_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT layer_id, name, type, bounds, owner_uuid, created_on
            FROM map_layers
            WHERE layer_id = $1
            """,
            args.layer_id,
        )
    if not row:
        return {"error": f"Layer {args.layer_id} not found."}
    if str(row["owner_uuid"]) != str(meta.user_uuid):
        return {"error": f"Layer {args.layer_id} is not owned by you."}
    if row["type"] != "raster":
        return {"error": f"Layer {args.layer_id} is type '{row['type']}', not a raster."}

    # Bounds-check the query point
    bounds = list(row["bounds"]) if row["bounds"] else None
    if bounds and len(bounds) == 4:
        west, south, east, north = bounds
        if not (west <= args.longitude <= east and south <= args.latitude <= north):
            return {
                "error": "point_outside_bounds",
                "message": (
                    f"Query point ({args.longitude:.4f}, {args.latitude:.4f}) is "
                    f"outside the layer's bounding box [{west:.4f},{south:.4f} → "
                    f"{east:.4f},{north:.4f}]."
                ),
            }

    # 2. Find the embedding of the tile that contains the query point.
    ensure_clay_tiles_collection()

    # Pull all tiles for the source layer; pick the one whose bbox covers the
    # query point. Tiles are typically <500/layer so this is cheap.
    src_tiles = get_tiles_for_layer(args.layer_id, limit=2000)
    if not src_tiles:
        return {
            "error": "no_embeddings",
            "message": (
                f"Layer {args.layer_id} has no Clay embeddings yet. Embeddings "
                f"are generated automatically after COG conversion completes "
                f"(usually <1 minute after upload). Try again shortly, or use "
                f"the layer's name to verify it's an RGB orthophoto — only "
                f"rgb_visual rasters are embedded in V1."
            ),
        }

    # Find the tile whose bbox contains (longitude, latitude)
    query_emb = None
    query_tile = None
    for t in src_tiles:
        bbox = list(t.get("bbox_wgs84") or [])
        if len(bbox) != 4:
            continue
        w, s, e, n = bbox
        if w <= args.longitude <= e and s <= args.latitude <= n:
            query_emb = list(t["embedding"])
            query_tile = {
                "tile_x": t["tile_x"], "tile_y": t["tile_y"], "bbox_wgs84": bbox,
            }
            break

    if query_emb is None:
        # Fallback: use the spatially-closest tile by bbox-center distance
        import math
        best = None
        best_dist = float("inf")
        for t in src_tiles:
            bbox = list(t.get("bbox_wgs84") or [])
            if len(bbox) != 4:
                continue
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2
            d = math.hypot(cx - args.longitude, cy - args.latitude)
            if d < best_dist:
                best_dist = d
                best = t
        if best is None:
            return {"error": "no_query_tile_found"}
        query_emb = list(best["embedding"])
        query_tile = {
            "tile_x": best["tile_x"], "tile_y": best["tile_y"],
            "bbox_wgs84": list(best["bbox_wgs84"]),
            "fallback_distance_deg": round(best_dist, 6),
        }

    # 3. Find layers the user can see (defense-in-depth alongside RLS).
    async with get_async_read_connection() as conn:
        owned = await conn.fetch(
            """
            SELECT layer_id, name, created_on
            FROM map_layers
            WHERE owner_uuid = $1
              AND type = 'raster'
            """,
            meta.user_uuid,
        )
    visible_ids = [r["layer_id"] for r in owned]
    layer_meta_by_id = {
        r["layer_id"]: {"name": r["name"], "created_on": r["created_on"]}
        for r in owned
    }

    # 4. Search Qdrant for top-K similar tiles
    raw_hits = search_similar_tiles(
        query_embedding=query_emb,
        visible_layer_ids=visible_ids,
        top_k=top_k + 1,  # +1 because the source tile itself will be the top hit
    )

    # 5. Format response: drop the self-match, attach layer name + capture date
    hits = []
    for h in raw_hits:
        # Skip the exact source tile (would always be the top hit)
        if (
            h["layer_id"] == args.layer_id
            and h["tile_x"] == query_tile["tile_x"]
            and h["tile_y"] == query_tile["tile_y"]
        ):
            continue
        info = layer_meta_by_id.get(h["layer_id"], {})
        bbox = h["bbox_wgs84"]
        center_lon = (bbox[0] + bbox[2]) / 2 if len(bbox) == 4 else None
        center_lat = (bbox[1] + bbox[3]) / 2 if len(bbox) == 4 else None
        hits.append({
            "layer_id": h["layer_id"],
            "layer_name": info.get("name"),
            "captured_at": info.get("created_on").isoformat() if info.get("created_on") else None,
            "similarity": round(float(h["distance"]), 4),  # COSINE: 1.0 = identical
            "tile_bbox_wgs84": bbox,
            "center_lon": round(center_lon, 6) if center_lon is not None else None,
            "center_lat": round(center_lat, 6) if center_lat is not None else None,
            "tile_x": h["tile_x"],
            "tile_y": h["tile_y"],
        })
        if len(hits) >= top_k:
            break

    return {
        "query": {
            "layer_id": args.layer_id,
            "layer_name": row["name"],
            "longitude": args.longitude,
            "latitude": args.latitude,
            "tile": query_tile,
        },
        "similar_tiles": hits,
        "metric": "cosine_similarity",
        "metric_note": "1.0 = identical visual content; lower = less similar. Same-flight nearby tiles typically score 0.65-0.85; cross-flight matches above 0.7 indicate genuinely similar features.",
    }
