"""Clay v1.5 batch-only embedding service for Phase 2 similarity search.

Triggered by COG-conversion completion (postgres_routes._background_generate_cog).
For each layer:
  1. Load Clay v1.5 (~6 GB RAM, ~10 s)
  2. Tile the COG into 256x256 RGB patches
  3. Run the encoder over batched tiles to get 1024-dim embeddings
  4. Insert into Milvus tagged with (layer_id, partner_id, tile_x, tile_y)
  5. Free the model (RAM returns to baseline)

V1 scope: rgb_visual orthophotos only. Drone 4-band-with-packed-indices
exports (raster_type rgb_with_packed_indices) are skipped — bands 2/3 are
derived NDVI/NDRE indices, not raw wavelengths Clay can ingest.
"""

import logging
import os
import time
from datetime import datetime
from typing import Optional

# Container appuser has no $HOME write access. Redirect every cache path
# the Clay/torch/huggingface stack tries to write to.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
os.environ.setdefault("HF_HOME", "/tmp/hf-cache")
os.environ.setdefault("HF_HUB_CACHE", "/tmp/hf-cache")
os.environ.setdefault("TRANSFORMERS_CACHE", "/tmp/hf-cache")
os.environ.setdefault("TORCH_HOME", "/tmp/torch-cache")

logger = logging.getLogger(__name__)

# Configuration via env (overrideable per-deploy)
_CHECKPOINT_PATH = os.environ.get(
    "CLAY_CHECKPOINT_PATH", "/app/data/clay-checkpoints/clay-v1.5.ckpt",
)
_CLAY_METADATA_PATH = os.environ.get(
    "CLAY_METADATA_PATH", "/app/clay-source/configs/metadata.yaml",
)
_TILE_SIZE = int(os.environ.get("CLAY_TILE_SIZE", "256"))
_MAX_TILES_PER_LAYER = int(os.environ.get("CLAY_MAX_TILES", "500"))
_BATCH_SIZE = int(os.environ.get("CLAY_BATCH_SIZE", "8"))

# LINZ sensor profile = 3-band RGB at submeter, closest match to drone
_DRONE_SENSOR = "linz"


# Lazy-loaded singletons. The model is heavy (~5 GB on disk, ~6 GB RAM); we
# load it inside embed_layer() and free it after to keep mundi-app slim.
_CACHED_METADATA: Optional[dict] = None


def _load_clay_metadata() -> dict:
    global _CACHED_METADATA
    if _CACHED_METADATA is not None:
        return _CACHED_METADATA
    import yaml
    with open(_CLAY_METADATA_PATH, "r") as f:
        _CACHED_METADATA = yaml.safe_load(f)
    return _CACHED_METADATA


def _build_drone_inputs(
    chips: "torch.Tensor",  # noqa: F821
    captured_at: datetime,
    center_lat: float,
    center_lon: float,
):
    """Build the (datacube) dict Clay's encoder expects.

    Returns wavelengths, timestamps_norm, latlons matching the chip batch.
    """
    import torch

    meta = _load_clay_metadata()
    sensor_meta = meta[_DRONE_SENSOR]
    band_order = sensor_meta["band_order"]
    # Wavelengths in nm (Clay metadata stores micrometers; multiply by 1000).
    wavelengths = torch.tensor(
        [[sensor_meta["bands"]["wavelength"][b] * 1000 for b in band_order]],
        dtype=torch.float32,
    ).repeat(chips.shape[0], 1)

    # Per-band normalization
    means = torch.tensor(
        [sensor_meta["bands"]["mean"][b] for b in band_order], dtype=torch.float32,
    ).view(1, len(band_order), 1, 1)
    stds = torch.tensor(
        [sensor_meta["bands"]["std"][b] for b in band_order], dtype=torch.float32,
    ).view(1, len(band_order), 1, 1)
    chips_normed = (chips - means) / stds

    # Timestamp encoding: Clay expects [week_of_year/52, hour/24] sin/cos pairs.
    # Drones don't have hour-of-day metadata typically, default to noon.
    week = captured_at.isocalendar().week
    hour = 12.0
    timestamps = torch.tensor(
        [[
            torch.sin(torch.tensor(week / 52.0 * 2 * 3.14159)).item(),
            torch.cos(torch.tensor(week / 52.0 * 2 * 3.14159)).item(),
            torch.sin(torch.tensor(hour / 24.0 * 2 * 3.14159)).item(),
            torch.cos(torch.tensor(hour / 24.0 * 2 * 3.14159)).item(),
        ]],
        dtype=torch.float32,
    ).repeat(chips.shape[0], 1)

    # Geographic encoding: [sin(lat), cos(lat), sin(lon), cos(lon)] in radians.
    lat_rad = center_lat * 3.14159 / 180.0
    lon_rad = center_lon * 3.14159 / 180.0
    latlons = torch.tensor(
        [[
            torch.sin(torch.tensor(lat_rad)).item(),
            torch.cos(torch.tensor(lat_rad)).item(),
            torch.sin(torch.tensor(lon_rad)).item(),
            torch.cos(torch.tensor(lon_rad)).item(),
        ]],
        dtype=torch.float32,
    ).repeat(chips.shape[0], 1)

    return {
        "pixels": chips_normed,
        "time": timestamps,
        "latlon": latlons,
        "waves": wavelengths[0],  # encoder expects 1D wavelengths shared across batch
        "gsd": torch.tensor(sensor_meta["gsd"], dtype=torch.float32),
    }


def _load_clay_model():
    """Load Clay v1.5 module from checkpoint. Returns the module in eval mode.
    The caller is responsible for calling del + gc.collect() afterward to
    free the ~6 GB working set.

    Clay's checkpoint stores `metadata_path: 'configs/metadata.yaml'` as a
    hyperparameter — a path relative to the working directory at load time.
    We override it to the absolute path so the load works regardless of CWD.
    """
    import torch
    from claymodel.module import ClayMAEModule

    if not os.path.exists(_CHECKPOINT_PATH):
        raise FileNotFoundError(
            f"Clay v1.5 checkpoint not found at {_CHECKPOINT_PATH}. "
            f"Download from https://huggingface.co/made-with-clay/Clay/resolve/main/v1.5/clay-v1.5.ckpt"
        )
    t0 = time.time()
    module = ClayMAEModule.load_from_checkpoint(
        _CHECKPOINT_PATH,
        map_location="cpu",
        metadata_path=_CLAY_METADATA_PATH,
    )
    module.eval()
    for p in module.parameters():
        p.requires_grad = False
    logger.info("Loaded Clay v1.5 in %.1fs", time.time() - t0)
    return module


def _tile_rgb_cog(cog_url: str, tile_size: int, max_tiles: int):
    """Yield (tile_x, tile_y, rgb_array_HxWxC, bbox_wgs84) for the COG.

    Uses native zoom (no downsampling) — Clay was trained at native sensor
    resolution. RGB orthophotos at 5-10cm/pixel produce 256-pixel tiles
    covering ~13-26 m on a side.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds

    os.environ.setdefault("GDAL_HTTP_UNSAFESSL", "YES")
    with rasterio.open(cog_url) as ds:
        w, h = ds.width, ds.height
        crs = ds.crs
        # Iterate tile grid
        nx = w // tile_size
        ny = h // tile_size
        total = nx * ny
        # Sample a uniform subset if total > max_tiles
        stride = max(1, int((total / max_tiles) ** 0.5)) if total > max_tiles else 1

        count = 0
        for ty in range(0, ny, stride):
            for tx in range(0, nx, stride):
                if count >= max_tiles:
                    return
                px = tx * tile_size
                py = ty * tile_size
                window = rasterio.windows.Window(px, py, tile_size, tile_size)
                # Read first 3 bands as RGB
                arr = ds.read([1, 2, 3], window=window, masked=True, fill_value=0)
                # Skip mostly-empty tiles
                if hasattr(arr, "mask"):
                    valid_pct = float(1.0 - arr.mask.mean())
                else:
                    valid_pct = 1.0
                if valid_pct < 0.2:
                    continue
                arr_data = (
                    arr.data.astype("float32")
                    if hasattr(arr, "data") else arr.astype("float32")
                )
                # Tile bounds in WGS84
                window_bounds = rasterio.windows.bounds(window, ds.transform)
                w_lon, s_lat, e_lon, n_lat = transform_bounds(
                    crs, "EPSG:4326", *window_bounds, densify_pts=21,
                )
                yield tx, ty, arr_data, [w_lon, s_lat, e_lon, n_lat]
                count += 1


async def embed_layer(
    layer_id: str,
    cog_url: Optional[str] = None,
    skip_if_already_embedded: bool = True,
) -> dict:
    """Tile a layer's COG, run Clay encoder, write embeddings to Milvus.

    Returns {layer_id, status, tiles_embedded, ...}.

    Skips silently for non-rgb_visual layers (V1 scope limitation).

    The function is async because it's called from FastAPI BackgroundTasks
    that already run on the event loop. Internally, all the work is done
    SYNCHRONOUSLY (DB, S3, Clay, Milvus) — torch + rasterio CPU-bound work
    interacts badly with asyncio executors on this image, hanging silently.
    Doing one big synchronous block in a fire-and-forget context is safe and
    far more reliable.
    """
    logger.info("embed_layer: start layer_id=%s", layer_id)
    return _embed_layer_sync(layer_id, cog_url, skip_if_already_embedded)


def _embed_layer_sync(
    layer_id: str,
    cog_url: Optional[str] = None,
    skip_if_already_embedded: bool = True,
) -> dict:
    """Synchronous body of embed_layer. See embed_layer docstring for why."""
    import json as _json
    import torch
    import psycopg2
    import psycopg2.extras
    import boto3
    from src.services.milvus_client import (
        ensure_clay_tiles_collection, insert_tile_embeddings,
        delete_layer_embeddings,
    )
    from src.tools.raster_query import _detect_raster_type

    # Sync DB lookup
    pg_url = (
        f"host={os.environ.get('POSTGRES_HOST', 'postgresdb')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'mundidb')} "
        f"user={os.environ.get('POSTGRES_USER', 'mundiuser')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', 'changeme')}"
    )
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT layer_id, name, type, s3_key, bounds, metadata,
                       created_on, owner_uuid
                FROM map_layers
                WHERE layer_id = %s
                """,
                (layer_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"layer_id": layer_id, "status": "layer_not_found"}

    metadata = (
        _json.loads(row["metadata"]) if isinstance(row["metadata"], str)
        else (dict(row["metadata"]) if row["metadata"] else {})
    )
    cog_key = metadata.get("cog_key")
    if not cog_key:
        return {"layer_id": layer_id, "status": "cog_pending"}

    raster_type, _rt_explanation = _detect_raster_type(metadata, row["name"], row["s3_key"])
    if raster_type != "rgb_visual":
        return {
            "layer_id": layer_id, "status": "skipped_not_rgb_visual",
            "raster_type": raster_type,
        }

    ensure_clay_tiles_collection()
    if skip_if_already_embedded:
        from src.services.milvus_client import (
            get_milvus_client, COLLECTION_CLAY_TILES,
        )
        from pymilvus import Collection
        get_milvus_client()
        coll = Collection(COLLECTION_CLAY_TILES)
        existing = coll.query(
            expr=f'layer_id == "{layer_id}"',
            output_fields=["id"], limit=1,
        )
        if existing:
            return {
                "layer_id": layer_id, "status": "already_embedded",
                "tiles_embedded": 0,
            }

    # Build presigned S3 URL using sync boto3 (mirrors the async S3 client config)
    s3_client = boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("S3_DEFAULT_REGION", "us-east-1"),
    )
    bucket = os.environ.get("S3_BUCKET", "test-bucket")
    if not cog_url:
        cog_url = s3_client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": cog_key},
            ExpiresIn=3600,
        )

    bounds = list(row["bounds"]) if row["bounds"] else None
    if bounds and len(bounds) == 4:
        center_lat = (bounds[1] + bounds[3]) / 2
        center_lon = (bounds[0] + bounds[2]) / 2
    else:
        center_lat, center_lon = 0.0, 0.0
    captured_at = row["created_on"] or datetime.utcnow()
    captured_at_epoch = int(captured_at.timestamp())
    owner_uuid = str(row["owner_uuid"])
    partner_id = metadata.get("partner_id") or ""

    def _do_embed():
        import gc

        # Drop any prior partial embeddings for this layer (idempotent re-run)
        delete_layer_embeddings(layer_id)

        module = _load_clay_model()
        encoder = module.model.encoder if hasattr(module, "model") else module.encoder
        # Clay is an MAE — by default masks 75% of patches during encoding.
        # For inference we want all patches encoded; set mask_ratio=0.
        encoder.mask_ratio = 0.0

        # Collect tiles
        tile_rows = []
        batch_chips = []
        batch_meta = []
        total = 0
        t0 = time.time()
        try:
            for tx, ty, arr_data, bbox in _tile_rgb_cog(
                cog_url, _TILE_SIZE, _MAX_TILES_PER_LAYER,
            ):
                # arr_data: (3, H, W) — Clay expects (B, C, H, W) so add batch dim later
                chip = torch.from_numpy(arr_data).unsqueeze(0)  # (1, 3, H, W)
                batch_chips.append(chip)
                batch_meta.append((tx, ty, bbox))
                if len(batch_chips) >= _BATCH_SIZE:
                    chips_t = torch.cat(batch_chips, dim=0)
                    datacube = _build_drone_inputs(
                        chips_t, captured_at, center_lat, center_lon,
                    )
                    with torch.no_grad():
                        out = encoder(datacube)
                    # Clay encoder returns (encoded_patches, ...). Take the
                    # cls_token (first patch in the sequence) as the per-tile
                    # embedding — standard ViT inference pattern.
                    encoded_patches = out[0] if isinstance(out, tuple) else out
                    emb = encoded_patches[:, 0, :]  # cls token, shape (B, D)
                    embeddings = emb.cpu().numpy()
                    for i, (xtx, xty, xbbox) in enumerate(batch_meta):
                        tile_rows.append({
                            "embedding": embeddings[i].tolist(),
                            "tile_x": xtx, "tile_y": xty,
                            "bbox_wgs84": xbbox,
                        })
                    batch_chips, batch_meta = [], []
                    total += embeddings.shape[0]

            # Final partial batch
            if batch_chips:
                chips_t = torch.cat(batch_chips, dim=0)
                datacube = _build_drone_inputs(
                    chips_t, captured_at, center_lat, center_lon,
                )
                with torch.no_grad():
                    out = encoder(datacube)
                encoded_patches = out[0] if isinstance(out, tuple) else out
                emb = encoded_patches[:, 0, :]
                embeddings = emb.cpu().numpy()
                for i, (xtx, xty, xbbox) in enumerate(batch_meta):
                    tile_rows.append({
                        "embedding": embeddings[i].tolist(),
                        "tile_x": xtx, "tile_y": xty,
                        "bbox_wgs84": xbbox,
                    })
                total += embeddings.shape[0]
        finally:
            del module, encoder
            gc.collect()

        if tile_rows:
            inserted = insert_tile_embeddings(
                layer_id=layer_id,
                partner_id=partner_id,
                owner_uuid=owner_uuid,
                captured_at_epoch=captured_at_epoch,
                zoom=18,  # native zoom — drone GSD is sub-meter
                rows=tile_rows,
            )
        else:
            inserted = 0
        elapsed = time.time() - t0
        return inserted, elapsed

    # _do_embed is heavy (~25-45s) but this function is already invoked from
    # a fire-and-forget BackgroundTask, so blocking the loop here is fine —
    # and avoids threading/asyncio interactions that hang silently when torch
    # operates inside run_in_executor on this image.
    inserted, elapsed = _do_embed()
    return {
        "layer_id": layer_id,
        "status": "ok" if inserted > 0 else "no_valid_tiles",
        "tiles_embedded": inserted,
        "elapsed_seconds": round(elapsed, 1),
        "raster_type": raster_type,
    }
