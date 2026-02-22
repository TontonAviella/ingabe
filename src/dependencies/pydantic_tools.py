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

from typing import Awaitable, Callable, TypeAlias, Any, Mapping
from pydantic import BaseModel

from src.tools.zoom import (
    ZoomToBoundsArgs,
    zoom_to_bounds,
)
from src.tools.pyd import IngabeToolCallMetaArgs
from src.tools.openstreetmap import (
    download_from_openstreetmap as osm_download_tool,
    DownloadFromOpenStreetMapArgs,
)
from src.tools.create_point import (
    create_point_layer,
    CreatePointLayerArgs,
)
from src.openstreetmap import has_openstreetmap_api_key


ToolFn = Callable[[Any, Any], Awaitable[dict]]
PydanticToolRegistry: TypeAlias = Mapping[
    str, tuple[ToolFn, type[BaseModel], type[BaseModel]]
]


def get_pydantic_tool_calls() -> PydanticToolRegistry:
    """Return mapping of tool name -> (async function, ArgModel, IngabeArgModel).

    Defined as a FastAPI dependency to allow overrides in tests or different deployments.
    """
    registry: dict[str, tuple[ToolFn, type[BaseModel], type[BaseModel]]] = {
        "zoom_to_bounds": (
            zoom_to_bounds,
            ZoomToBoundsArgs,
            IngabeToolCallMetaArgs,
        ),
        "create_point_layer": (
            create_point_layer,
            CreatePointLayerArgs,
            IngabeToolCallMetaArgs,
        ),
    }
    if has_openstreetmap_api_key():
        registry["download_from_openstreetmap"] = (
            osm_download_tool,
            DownloadFromOpenStreetMapArgs,
            IngabeToolCallMetaArgs,
        )
    return registry
