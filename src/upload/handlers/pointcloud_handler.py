"""Point cloud upload handler — LAS/LAZ files."""

import json
import logging

from src.upload.base import BaseUploadHandler, HandlerResult, UploadContext
from src.upload.preprocessing import preprocess_point_cloud

logger = logging.getLogger(__name__)


class PointCloudUploadHandler(BaseUploadHandler):
    """Handles point cloud uploads (LAS, LAZ).

    Preprocessing reprojects to EPSG:4326 via ``las2las64`` and extracts
    anchor coordinates and Z-range metadata.
    """

    async def preprocess(self, ctx: UploadContext) -> HandlerResult:
        pc = await preprocess_point_cloud(ctx.temp_file_path, ctx.metadata_dict)
        return HandlerResult(
            layer_type="point_cloud",
            bounds=pc.bounds,
            updated_temp_file_path=pc.path,
            temp_dir_to_cleanup=pc.temp_dir,
        )

    async def create_layers(
        self, ctx: UploadContext, result: HandlerResult
    ) -> HandlerResult:
        """Insert a single point cloud layer row."""
        await ctx.conn.execute(
            """
            INSERT INTO map_layers
            (layer_id, owner_uuid, name, type, metadata, bounds, geometry_type, feature_count, s3_key, size_bytes, source_map_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            ctx.layer_id,
            ctx.user_id,
            ctx.layer_name,
            "point_cloud",
            json.dumps(ctx.metadata_dict),
            result.bounds,
            None,
            None,
            ctx.s3_key,
            ctx.file_size_bytes,
            ctx.map_id,
        )
        result.created_layer_ids.append(ctx.layer_id)
        result.first_layer_name = ctx.layer_name
        result.first_layer_url = f"/api/layer/{ctx.layer_id}.laz"

        return result
