from typing import Any, Dict, List
from pydantic import BaseModel, Field
from src.routes.websocket import kue_ephemeral_action
from src.tools.pyd import IngabeToolCallMetaArgs
from src.openstreetmap import download_from_openstreetmap as core_osm_download


class DownloadFromOpenStreetMapArgs(BaseModel):
    tags: str = Field(
        ...,
        description="Tags to filter for e.g. leisure=park, use & to AND tags together e.g. highway=footway&name=*, no commas",
    )
    bbox: list[float] = Field(
        ...,
        description="Bounding box in [xmin, ymin, xmax, ymax] format e.g. [9.023802,39.172149,9.280779,39.275211] for Cagliari, Italy",
    )
    new_layer_name: str = Field(
        ...,
        description="Human-friendly name e.g. Walking paths or Liquor stores in Seattle",
    )


async def download_from_openstreetmap(
    args: DownloadFromOpenStreetMapArgs, meta: IngabeToolCallMetaArgs
) -> Dict[str, Any]:
    """Download features from OSM and add to project as a cloud FlatGeobuf layer"""
    tags = args.tags
    bbox = args.bbox
    new_layer_name = args.new_layer_name

    async with kue_ephemeral_action(
        meta.conversation_id, f"Downloading data from OpenStreetMap: {tags}"
    ):
        result = await core_osm_download(
            map_id=meta.map_id,
            bbox=bbox,
            tags=tags,
            new_layer_name=new_layer_name,
            session=meta.session,
        )

    # Add instructions to result if download was successful
    if result.get("status") == "success":
        raw_layers = result.get("uploaded_layers")
        typed_layers: List[Dict[str, Any]] = (
            raw_layers if isinstance(raw_layers, list) else []
        )
        layer_names = [
            f"{new_layer_name}_{layer['geometry_type']}" for layer in typed_layers
        ]
        layer_ids = [layer["layer_id"] for layer in typed_layers]
        if layer_names and layer_ids:
            result["kue_instructions"] = (
                f"New layers available: {', '.join(layer_names)} "
                f"(IDs: {', '.join(layer_ids)}), all currently invisible. "
                'To make any of these visible to the user on their map, use "add_layer_to_map" with the layer_id and a descriptive new_name.'
            )

    return result
