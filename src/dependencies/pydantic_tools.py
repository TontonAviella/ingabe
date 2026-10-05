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
from src.tools.create_point import (
    create_point_layer,
    CreatePointLayerArgs,
)
from src.tools.search_place import (
    search_location,
    SearchLocationArgs,
)
from src.tools.admin_units import (
    list_admin_units,
    ListAdminUnitsArgs,
)
from src.tools.display_layer import (
    display_satellite_layer,
    DisplaySatelliteLayerArgs,
    display_layer,
    DisplayLayerArgs,
    display_geojson_layer,
    DisplayGeojsonLayerArgs,
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
from src.tools.sar import (
    predict_ndvi_from_sar,
    PredictNdviFromSarArgs,
    detect_flood_extent,
    DetectFloodExtentArgs,
)
from src.tools.raster_query import (
    describe_user_raster,
    DescribeUserRasterArgs,
    compute_zonal_stats,
    ComputeZonalStatsArgs,
    read_pixel_at,
    ReadPixelAtArgs,
    get_value_distribution,
    GetValueDistributionArgs,
)
from src.tools.raster_interpret import (
    interpret_raster_health,
    InterpretRasterHealthArgs,
    find_stress_zones,
    FindStressZonesArgs,
    compare_rasters,
    CompareRastersArgs,
    evaluate_insurance_trigger,
    EvaluateInsuranceTriggerArgs,
)
from src.tools.rgb_visual import (
    analyze_rgb_field,
    AnalyzeRgbFieldArgs,
)
from src.tools.raster_h3_context import (
    create_raster_h3_context_layer,
    CreateRasterH3ContextLayerArgs,
)
from src.tools.raster_object_candidates import (
    analyze_raster_object_candidates,
    AnalyzeRasterObjectCandidatesArgs,
)


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
        "list_admin_units": (
            list_admin_units,
            ListAdminUnitsArgs,
            IngabeToolCallMetaArgs,
        ),
        "display_satellite_layer": (
            display_satellite_layer,
            DisplaySatelliteLayerArgs,
            IngabeToolCallMetaArgs,
        ),
        "display_layer": (
            display_layer,
            DisplayLayerArgs,
            IngabeToolCallMetaArgs,
        ),
        "display_geojson_layer": (
            display_geojson_layer,
            DisplayGeojsonLayerArgs,
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
        "predict_ndvi_from_sar": (
            predict_ndvi_from_sar,
            PredictNdviFromSarArgs,
            IngabeToolCallMetaArgs,
        ),
        "detect_flood_extent": (
            detect_flood_extent,
            DetectFloodExtentArgs,
            IngabeToolCallMetaArgs,
        ),
        "describe_user_raster": (
            describe_user_raster,
            DescribeUserRasterArgs,
            IngabeToolCallMetaArgs,
        ),
        "compute_zonal_stats": (
            compute_zonal_stats,
            ComputeZonalStatsArgs,
            IngabeToolCallMetaArgs,
        ),
        "interpret_raster_health": (
            interpret_raster_health,
            InterpretRasterHealthArgs,
            IngabeToolCallMetaArgs,
        ),
        "analyze_rgb_field": (
            analyze_rgb_field,
            AnalyzeRgbFieldArgs,
            IngabeToolCallMetaArgs,
        ),
        "read_pixel_at": (
            read_pixel_at,
            ReadPixelAtArgs,
            IngabeToolCallMetaArgs,
        ),
        "get_value_distribution": (
            get_value_distribution,
            GetValueDistributionArgs,
            IngabeToolCallMetaArgs,
        ),
        "find_stress_zones": (
            find_stress_zones,
            FindStressZonesArgs,
            IngabeToolCallMetaArgs,
        ),
        "compare_rasters": (
            compare_rasters,
            CompareRastersArgs,
            IngabeToolCallMetaArgs,
        ),
        "evaluate_insurance_trigger": (
            evaluate_insurance_trigger,
            EvaluateInsuranceTriggerArgs,
            IngabeToolCallMetaArgs,
        ),
        "create_raster_h3_context_layer": (
            create_raster_h3_context_layer,
            CreateRasterH3ContextLayerArgs,
            IngabeToolCallMetaArgs,
        ),
        "analyze_raster_object_candidates": (
            analyze_raster_object_candidates,
            AnalyzeRasterObjectCandidatesArgs,
            IngabeToolCallMetaArgs,
        ),
    }
    return registry
