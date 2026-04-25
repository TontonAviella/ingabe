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
from src.tools.search_place import (
    search_location,
    SearchLocationArgs,
)
from src.tools.display_layer import (
    display_satellite_layer,
    DisplaySatelliteLayerArgs,
)
from src.tools.spectral_index import (
    compute_spectral_index,
    ComputeSpectralIndexArgs,
)
from src.tools.wapor import (
    get_soil_moisture,
    GetSoilMoistureArgs,
    get_evapotranspiration,
    GetEvapotranspirationArgs,
)
from src.tools.food_security import (
    get_food_security_alerts,
    GetFoodSecurityAlertsArgs,
)
from src.tools.sar import (
    predict_ndvi_from_sar,
    PredictNdviFromSarArgs,
    detect_water_bodies,
    DetectWaterBodiesArgs,
    detect_flood_extent,
    DetectFloodExtentArgs,
)
from src.tools.alos import (
    get_alos_l_band_stats,
    GetAlosLBandStatsArgs,
    get_alos_temporal_variation,
    GetAlosTemporalVariationArgs,
)
from src.tools.cygnss import (
    check_cygnss_availability,
    CheckCygnssAvailabilityArgs,
    get_cygnss_soil_moisture,
    GetCygnssSoilMoistureArgs,
    get_cygnss_watermask,
    GetCygnssWatermaskArgs,
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
        "search_location": (
            search_location,
            SearchLocationArgs,
            IngabeToolCallMetaArgs,
        ),
        "display_satellite_layer": (
            display_satellite_layer,
            DisplaySatelliteLayerArgs,
            IngabeToolCallMetaArgs,
        ),
        "compute_spectral_index": (
            compute_spectral_index,
            ComputeSpectralIndexArgs,
            IngabeToolCallMetaArgs,
        ),
        "get_soil_moisture": (
            get_soil_moisture,
            GetSoilMoistureArgs,
            IngabeToolCallMetaArgs,
        ),
        "get_evapotranspiration": (
            get_evapotranspiration,
            GetEvapotranspirationArgs,
            IngabeToolCallMetaArgs,
        ),
        "get_food_security_alerts": (
            get_food_security_alerts,
            GetFoodSecurityAlertsArgs,
            IngabeToolCallMetaArgs,
        ),
        "predict_ndvi_from_sar": (
            predict_ndvi_from_sar,
            PredictNdviFromSarArgs,
            IngabeToolCallMetaArgs,
        ),
        "detect_water_bodies": (
            detect_water_bodies,
            DetectWaterBodiesArgs,
            IngabeToolCallMetaArgs,
        ),
        "detect_flood_extent": (
            detect_flood_extent,
            DetectFloodExtentArgs,
            IngabeToolCallMetaArgs,
        ),
        "get_alos_l_band_stats": (
            get_alos_l_band_stats,
            GetAlosLBandStatsArgs,
            IngabeToolCallMetaArgs,
        ),
        "get_alos_temporal_variation": (
            get_alos_temporal_variation,
            GetAlosTemporalVariationArgs,
            IngabeToolCallMetaArgs,
        ),
        "check_cygnss_availability": (
            check_cygnss_availability,
            CheckCygnssAvailabilityArgs,
            IngabeToolCallMetaArgs,
        ),
        "get_cygnss_soil_moisture": (
            get_cygnss_soil_moisture,
            GetCygnssSoilMoistureArgs,
            IngabeToolCallMetaArgs,
        ),
        "get_cygnss_watermask": (
            get_cygnss_watermask,
            GetCygnssWatermaskArgs,
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
