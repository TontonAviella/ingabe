"""Dagster assets that prove GeoLibre-Rust/WASM processing is operational."""

from typing import Any

from dagster import AssetExecutionContext, asset

from src.pipelines.posthog_observability import observed_dagster_asset
from src.services.geolibre_runner import run_geolibre_smoke_suite


@asset(
    group_name="geolibre_runtime",
    description=(
        "Validate GeoLibre-Rust/WASM against small Open Buildings vector and "
        "satellite raster workflows, emitting PostHog/pipeline evidence."
    ),
)
@observed_dagster_asset(
    asset_name="geolibre_runtime_probe",
    pipeline_family="geolibre_runtime",
    source_category="geolibre",
    analysis_domain="platform",
    evidence_kind="runtime_smoke",
)
def geolibre_runtime_probe(context: AssetExecutionContext) -> dict[str, Any]:
    """Run tiny vector/raster workflows through geolibre_wasm for proof."""

    result = run_geolibre_smoke_suite()
    context.log.info(
        "GeoLibre probe completed: status=%s sample_success=%s/%s tool_count=%s",
        result.get("status"),
        result.get("sample_success_count"),
        result.get("sample_workflow_count"),
        result.get("tool_count"),
    )
    return result
