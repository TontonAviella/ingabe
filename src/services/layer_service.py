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

"""Layer business logic extracted from route handlers.

Provides shared functions that multiple route modules need, without
introducing cross-route import dependencies.
"""

import json

from fastapi import HTTPException, status

from src.dependencies.layer_describer import LayerDescriber
from src.structures import get_async_db_connection


async def describe_layer_internal(
    layer_id: str,
    layer_describer: LayerDescriber,
    session_user_id: str,
) -> str:
    """Generate a Markdown description of a layer, including its active style.

    Raises 404 if the layer does not exist, 403 if the caller does not own
    the map that contains it.
    """
    async with get_async_db_connection() as conn:
        layer = await conn.fetchrow(
            """
            SELECT layer_id, name, type, metadata, bounds, geometry_type,
                   created_on, last_edited, feature_count, s3_key, remote_url,
                   postgis_query, postgis_connection_id
            FROM map_layers
            WHERE layer_id = $1
            """,
            layer_id,
        )

        if not layer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Layer not found"
            )

        # Check if the layer is associated with any maps via the layers array
        # Order by created_on DESC to get the most recently created map first
        map_result = await conn.fetchrow(
            """
            SELECT id, title, description, owner_uuid
            FROM user_mundiai_maps
            WHERE $1 = ANY(layers) AND soft_deleted_at IS NULL
            ORDER BY created_on DESC
            """,
            layer_id,
        )
        if map_result:
            # User must own the map to access this endpoint
            if session_user_id != str(map_result["owner_uuid"]):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You must own this map to access layer description",
                )

        # Use the injected LayerDescriber to generate the response
        markdown_response = await layer_describer.describe_layer(layer_id, dict(layer))

        # Fetch active style JSON if layer is associated with a map
        if map_result:
            style_result = await conn.fetchrow(
                """
                SELECT ls.style_json, ls.style_id
                FROM map_layer_styles mls
                JOIN layer_styles ls ON mls.style_id = ls.style_id
                WHERE mls.map_id = $1 AND mls.layer_id = $2
                """,
                map_result["id"],
                layer_id,
            )
            if style_result:
                # Add style information if available (for vector layers)
                style_section = f"\n## Style ID ({style_result['style_id']})\n"
                style_section += "```json\n"
                # Parse style_json if it's a string (asyncpg returns JSON as strings)
                style_json = style_result["style_json"]
                if isinstance(style_json, str):
                    style_section += style_json
                else:
                    style_section += json.dumps(style_json)
                style_section += "\n```"
                markdown_response += style_section

        return markdown_response
