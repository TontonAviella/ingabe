from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from pydantic import BaseModel


class SelectedFeature(BaseModel):
    layer_id: str
    attributes: Dict[str, Any]


class MapStateProvider(ABC):
    @abstractmethod
    async def get_system_messages(
        self,
        messages: List[Dict[str, Any]],
        current_map_description: str,
        selected_feature: SelectedFeature | None,
        viewport_bounds: Optional[List[float]] = None,
    ) -> List[Dict[str, Any]]:
        pass


def _build_current_aoi_block(
    selected_feature: SelectedFeature | None,
    viewport_bounds: Optional[List[float]],
) -> str:
    """Build the <CurrentAOI> hint Sage uses to ground every spatial tool call.

    Precedence: selected_feature (user clicked a feature) > viewport_bounds
    (ambient view) > country default. Sage reads this block first when deciding
    what bbox/geometry/lat-lon to pass to tools.
    """
    if selected_feature:
        return (
            "<CurrentAOI>\n"
            f"  source: selected_feature\n"
            f"  layer_id: {selected_feature.layer_id}\n"
            "  grain: feature\n"
            "  hint: The user has selected a feature on layer {layer_id}. "
            "Look up the layer's bounds inside <MapState> and pass them as bbox/geometry "
            "to any analytical tool. Pass the same bounds to display_layer for "
            "visual output. Do NOT default to a district name when a feature is selected.\n"
            "</CurrentAOI>"
        ).replace("{layer_id}", selected_feature.layer_id)

    if viewport_bounds and len(viewport_bounds) == 4:
        west, south, east, north = viewport_bounds
        return (
            "<CurrentAOI>\n"
            f"  source: viewport_bounds\n"
            f"  bbox: {west:.5f},{south:.5f},{east:.5f},{north:.5f}\n"
            "  grain: ambient_view\n"
            "  hint: No specific feature selected. The user's current map view is the "
            "implicit AOI. Use this bbox for tools that need spatial scope. For "
            "tools that take a single point (lat/lon), use the bbox center.\n"
            "</CurrentAOI>"
        )

    return (
        "<CurrentAOI>\n"
        "  source: default\n"
        "  grain: country\n"
        "  hint: No selected feature, no viewport bounds. Default to Rwanda country "
        "scale (centroid -1.94, 29.87). Ask the user to draw a polygon or pick a "
        "place name if a tool needs finer scope.\n"
        "</CurrentAOI>"
    )


class DefaultMapStateProvider(MapStateProvider):
    async def get_system_messages(
        self,
        messages: List[Dict[str, Any]],
        current_map_description: str,
        selected_feature: SelectedFeature | None,
        viewport_bounds: Optional[List[float]] = None,
    ) -> List[Dict[str, Any]]:
        system_messages = []

        tagged_description = f"<MapState>\n{current_map_description}\n</MapState>"
        system_messages.append({"role": "system", "content": tagged_description})

        if selected_feature:
            selected_feature_content = f"<SelectedFeature>\n{selected_feature.model_dump_json()}\n</SelectedFeature>"
            system_messages.append(
                {"role": "system", "content": selected_feature_content}
            )
        else:
            system_messages.append(
                {"role": "system", "content": "<NoSelectedFeature />"}
            )

        # AOI block — anchors every spatial tool call. Reads selected_feature +
        # viewport_bounds and synthesizes the user's "subject of attention." Sage
        # uses this to stop defaulting to district names when a finer scope exists.
        system_messages.append(
            {"role": "system", "content": _build_current_aoi_block(selected_feature, viewport_bounds)}
        )

        return system_messages


def get_map_state_provider() -> MapStateProvider:
    return DefaultMapStateProvider()
