from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, Field

from src.services.geolibre_runner import (
    GeolibreRunInput,
    decode_inline_file_value,
    geolibre_runner_status,
    list_geolibre_tool_manifests,
    run_geolibre_smoke_suite,
    run_geolibre_tool as run_geolibre_tool_service,
)
from src.tools.pyd import IngabeToolCallMetaArgs


class GetGeolibreToolCapabilitiesArgs(BaseModel):
    search: str = Field(
        ...,
        description="Optional lowercase search text for tool id/name/description. Pass empty string to list the default catalog page.",
    )
    source: str = Field(
        ...,
        description="Optional source filter: geolibre or whitebox. Pass empty string for both.",
    )
    category: str = Field(
        ...,
        description="Optional category filter such as Raster, Vector, Hydrology, Lidar, Terrain, or Conversion. Pass empty string for all.",
    )
    limit: int = Field(
        ...,
        description="Maximum tools to return. Use 20-50 for Sage planning; maximum returned by the service is 200.",
    )
    include_runtime_status: bool = Field(
        ...,
        description="Whether to include runtime status and sample core tools. Usually true for diagnostics, false for normal planning.",
    )


async def get_geolibre_tool_capabilities(
    args: GetGeolibreToolCapabilitiesArgs,
    meta: IngabeToolCallMetaArgs,
) -> dict[str, Any]:
    """List GeoLibre-Rust/Whitebox tools available through the Rust/WASM runner.

    Use this when Sage needs to choose a concrete GeoLibre tool for raster,
    vector, satellite, Open Buildings, DEM, terrain, hydrology, LiDAR, tiling,
    PMTiles, or GeoParquet work. This is discovery only; use run_geolibre_tool
    to execute a selected tool.
    """

    result = list_geolibre_tool_manifests(
        search=args.search,
        source=args.source,
        category=args.category,
        limit=args.limit,
    )
    if args.include_runtime_status:
        result["runtime"] = geolibre_runner_status(include_manifest_sample=True)
    return result


class RunGeolibreToolArgs(BaseModel):
    tool_id: str = Field(
        ...,
        description="GeoLibre/Whitebox tool id from get_geolibre_tool_capabilities, e.g. spectral_index, slope, write_geoparquet, vector_convert.",
    )
    args_json: str = Field(
        ...,
        description="JSON array of CLI-style args, e.g. [\"--input=/work/in.geojson\", \"--output=/work/out.parquet\"].",
    )
    input_files_json: str = Field(
        ...,
        description=(
            "JSON object mapping /work-relative filenames to UTF-8 text, base64:<data>, "
            "or an http(s) URL. Do not pass secrets or huge inline files."
        ),
    )
    source_category: str = Field(
        ...,
        description="Evidence source family: raster, vector, satellite, open_buildings, terrain, hydrology, lidar, or geolibre.",
    )
    pipeline_family: str = Field(
        ...,
        description="Low-cardinality evidence pipeline name, e.g. satellite_geolibre, open_buildings_geolibre, user_raster_geolibre.",
    )
    analysis_domain: str = Field(
        ...,
        description="Domain label such as agriculture, housing, infrastructure, environment, hydrology, or platform.",
    )
    evidence_kind: str = Field(
        ...,
        description="Specific proof kind, e.g. satellite_spectral_index, open_buildings_vector_conversion, terrain_slope.",
    )
    timeout_seconds: int = Field(
        ...,
        description="Execution timeout for the live Sage call. Use 10-60 for small inputs; long jobs should run in Dagster.",
    )
    max_output_bytes: int = Field(
        ...,
        description="Maximum output bytes to summarize before omitting file details. Use 10000000-32000000 for live runs.",
    )


async def run_geolibre_tool(
    args: RunGeolibreToolArgs,
    meta: IngabeToolCallMetaArgs,
) -> dict[str, Any]:
    """Run a selected GeoLibre-Rust/Whitebox tool through geolibre_wasm.

    Use for concrete geoprocessing against small or already-clipped raster,
    vector, satellite, Open Buildings, DEM, hydrology, LiDAR, GeoParquet, or
    PMTiles inputs. This emits local pipeline evidence and PostHog events named
    geolibre_tool_completed and geospatial_pipeline_flow_completed. It returns
    file summaries, not raw output bytes; persist heavy outputs in a job.
    """

    try:
        cli_args = json.loads(args.args_json)
        if not isinstance(cli_args, list) or not all(
            isinstance(item, str) for item in cli_args
        ):
            return {"status": "error", "error": "args_json must be a JSON array of strings."}
    except json.JSONDecodeError as exc:
        return {"status": "error", "error": f"args_json is not valid JSON: {exc}"}

    try:
        raw_inputs = json.loads(args.input_files_json)
        if not isinstance(raw_inputs, dict):
            return {"status": "error", "error": "input_files_json must be a JSON object."}
        input_files = {
            str(name): decode_inline_file_value(str(value))
            for name, value in raw_inputs.items()
        }
    except json.JSONDecodeError as exc:
        return {"status": "error", "error": f"input_files_json is not valid JSON: {exc}"}
    except Exception as exc:
        return {"status": "error", "error": f"input file decoding failed: {exc}"}

    payload = GeolibreRunInput(
        tool_id=args.tool_id,
        args=cli_args,
        input_files=input_files,
        source_category=args.source_category,
        pipeline_family=args.pipeline_family,
        analysis_domain=args.analysis_domain,
        evidence_kind=args.evidence_kind,
        max_output_bytes=args.max_output_bytes,
        distinct_id=f"sage-conversation-{meta.conversation_id}",
        context={
            "map_id_present": bool(meta.map_id),
            "project_id_present": bool(meta.project_id),
        },
    )
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: run_geolibre_tool_service(payload),
            ),
            timeout=max(1, int(args.timeout_seconds)),
        )
    except asyncio.TimeoutError:
        return {
            "status": "error",
            "backend": "geolibre_wasm",
            "tool_id": args.tool_id,
            "error": f"GeoLibre tool timed out after {args.timeout_seconds}s.",
        }


class RunGeolibreSmokeSuiteArgs(BaseModel):
    emit_evidence: bool = Field(
        ...,
        description="Must be true to run. The smoke suite emits local pipeline evidence and PostHog events if backend PostHog is configured.",
    )


async def run_geolibre_smoke_suite_tool(
    args: RunGeolibreSmokeSuiteArgs,
    meta: IngabeToolCallMetaArgs,
) -> dict[str, Any]:
    """Run a tiny Open Buildings vector conversion and satellite NDVI GeoLibre smoke test.

    Use this only for diagnostics or release checks. It proves the GeoLibre
    runner can process Open Buildings-like vector data and satellite-like
    multiband raster data, and emits evidence for operators.
    """

    if not args.emit_evidence:
        return {"status": "skipped", "reason": "emit_evidence must be true"}
    return await asyncio.get_running_loop().run_in_executor(
        None,
        run_geolibre_smoke_suite,
    )
