from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from src.services.geolibre_runner import (
    geolibre_runner_status,
    run_geolibre_smoke_suite,
)
from src.services.life_harness import life_harness_enabled
from src.services.raster_object_candidates import _fastsam_weights_status


def build_runtime_audit(*, deep: bool = False) -> dict[str, Any]:
    fastsam_package = _package_status("ultralytics", "ultralytics")
    fastsam_weights = _fastsam_weights_status()
    fastsam_ready = bool(
        fastsam_package["installed"] and fastsam_weights.get("available")
    )

    hermes_package = _package_status("hermes_cli", "hermes-agent")
    hermes_enabled = _truthy_env("MUNDI_USE_HERMES")
    hermes_tool_route = _truthy_env("MUNDI_TOOL_CALL_ENABLED")
    hermes_secret = bool(os.environ.get("HERMES_GATEWAY_SECRET", "").strip())
    plugin_path = Path(
        os.environ.get(
            "MUNDI_HERMES_PLUGIN_PATH",
            "/app/hermes_integration/plugins/ingabe-sage",
        )
    )
    hermes_ready = bool(
        hermes_package["installed"]
        and plugin_path.is_dir()
        and hermes_enabled
        and hermes_tool_route
        and hermes_secret
    )

    geolibre = geolibre_runner_status(include_manifest_sample=False)
    components: dict[str, Any] = {
        "fastsam": {
            **fastsam_package,
            "weights": fastsam_weights,
            "ready": fastsam_ready,
            "decision": "keep: primary orthophoto object-mask engine",
        },
        "geolibre_rust": {
            **geolibre,
            "ready": geolibre.get("status") == "success",
            "decision": (
                "keep: deterministic raster/vector conversion, spectral, terrain, "
                "hydrology, GeoParquet, and PMTiles work"
            ),
        },
        "hermes_sage": {
            **hermes_package,
            "enabled": hermes_enabled,
            "tool_route_enabled": hermes_tool_route,
            "gateway_secret_configured": hermes_secret,
            "plugin_present": plugin_path.is_dir(),
            "ready": hermes_ready,
            "decision": (
                "gated: keep installed for complex planning, but do not make it "
                "the default until its local callback and latency gates pass"
            ),
        },
        "life_harness": {
            "installed": True,
            "enabled": life_harness_enabled(),
            "ready": life_harness_enabled(),
            "decision": "keep: low-overhead procedural and tool-contract guard",
        },
        "harnessx": {
            **_package_status("harnessx", "harnessx"),
            "ready": False,
            "decision": (
                "exclude: duplicates Hermes/Life-Harness and adds a second agent "
                "runtime, provider stack, UI, sandbox, and tool registry"
            ),
        },
        "davidondrej_skills": {
            "installed": False,
            "ready": False,
            "decision": (
                "exclude wholesale: no drone/GIS procedure; import individual ideas "
                "only when a measured Ingabe failure requires one"
            ),
        },
        "segment_geospatial_samgeo": {
            **_package_status("samgeo", "segment-geospatial"),
            "ready": False,
            "decision": (
                "exclude: duplicates FastSAM, adds heavyweight SAM runtimes, and is "
                "not the selected local CPU path"
            ),
        },
        "forge3d": {
            **_package_status("forge3d", "forge3d"),
            "enabled": False,
            "ready": False,
            "decision": (
                "optional only: useful for a future dedicated 3D viewer/export path, "
                "not for FastSAM accuracy or current MapLibre delivery"
            ),
        },
    }

    if deep:
        components["geolibre_rust"]["smoke"] = run_geolibre_smoke_suite()
        components["hermes_sage"]["plugin_probe"] = _hermes_plugin_probe()

    required = ("fastsam", "geolibre_rust", "life_harness")
    return {
        "status": (
            "healthy"
            if all(components[name].get("ready") for name in required)
            else "degraded"
        ),
        "runtime_policy": "local_only",
        "source_control": "https://github.com/TontonAviella/ingabe.git",
        "deep": deep,
        "components": components,
    }


def _package_status(import_name: str, package_name: str) -> dict[str, Any]:
    installed = importlib.util.find_spec(import_name) is not None
    version = None
    if installed:
        try:
            version = importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return {"installed": installed, "version": version}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in {"1", "true", "yes"}


def _hermes_plugin_probe() -> dict[str, Any]:
    try:
        from hermes_cli.plugins import PluginManager

        manager = PluginManager()
        manager.discover_and_load()
        plugins = manager.list_plugins()
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    ingabe = next(
        (plugin for plugin in plugins if plugin.get("name") == "ingabe-sage"),
        None,
    )
    return {
        "status": "success" if ingabe and not ingabe.get("error") else "error",
        "plugin": ingabe,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit local Ingabe runtime components"
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="also execute the GeoLibre smoke suite and Hermes plugin discovery",
    )
    args = parser.parse_args()
    print(json.dumps(build_runtime_audit(deep=args.deep), indent=2, default=str))


if __name__ == "__main__":
    main()
