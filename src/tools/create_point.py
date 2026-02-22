# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import json
import logging
from io import BytesIO
from typing import Any, Dict

from fastapi import UploadFile
from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services.map_service import internal_upload_layer
from src.structures import async_conn
from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)


class CreatePointLayerArgs(BaseModel):
    longitude: float = Field(
        ...,
        description="Longitude in decimal degrees (WGS84), e.g. 30.1127",
    )
    latitude: float = Field(
        ...,
        description="Latitude in decimal degrees (WGS84), e.g. -2.2321",
    )
    label: str = Field(
        ...,
        description="Human-readable label for the point, e.g. 'Rusumo Falls'",
    )


async def create_point_layer(
    args: CreatePointLayerArgs, meta: IngabeToolCallMetaArgs
) -> Dict[str, Any]:
    """Create a new point layer from coordinates. Use this when the user provides lat/lon coordinates and you need a layer to mark that location, buffer into a circle, or use as input for geoprocessing tools."""
    lon = args.longitude
    lat = args.latitude
    label = args.label

    # Validate coordinate ranges
    if not (-180 <= lon <= 180):
        return {"status": "error", "message": f"Longitude {lon} out of range [-180, 180]"}
    if not (-90 <= lat <= 90):
        return {"status": "error", "message": f"Latitude {lat} out of range [-90, 90]"}

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": label},
                "geometry": {
                    "type": "Point",
                    "coordinates": [lon, lat],
                },
            }
        ],
    }

    # Look up project_id from map_id
    user_id = meta.session.get_user_id()
    async with async_conn("create_point_layer_project_lookup") as conn:
        map_row = await conn.fetchrow(
            """
            SELECT project_id
            FROM user_mundiai_maps
            WHERE id = $1 AND owner_uuid = $2 AND soft_deleted_at IS NULL
            """,
            meta.map_id,
            user_id,
        )
        if not map_row:
            return {"status": "error", "message": f"Map {meta.map_id} not found"}
        project_id: str = map_row["project_id"]

    geojson_bytes = json.dumps(geojson).encode("utf-8")

    async with kue_ephemeral_action(
        meta.conversation_id, f"Creating point at {lat:.4f}, {lon:.4f}"
    ):
        upload_file = UploadFile(
            filename="point.geojson", file=BytesIO(geojson_bytes)
        )
        layer_response = await internal_upload_layer(
            map_id=meta.map_id,
            file=upload_file,
            layer_name=label,
            add_layer_to_map=False,
            user_id=user_id,
            project_id=project_id,
        )

    layer_id = layer_response.id
    logger.info(
        "Created point layer %s at (%s, %s) for map %s",
        layer_id, lat, lon, meta.map_id,
    )

    return {
        "status": "success",
        "layer_id": layer_id,
        "label": label,
        "coordinates": [lon, lat],
        "kue_instructions": (
            f"Point layer '{label}' created (ID: {layer_id}), currently invisible. "
            "To show it on the map, use add_layer_to_map. "
            "To create a circle/buffer around this point, use native_buffer with this layer as INPUT."
        ),
    }
