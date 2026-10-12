from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
from typing import Any

from src.services.life_harness import life_harness_enabled
from src.services.raster_object_candidates import _fastsam_weights_status


def build_runtime_audit() -> dict[str, Any]:
    fastsam_package = _package_status("ultralytics", "ultralytics")
    fastsam_weights = _fastsam_weights_status()
    fastsam_ready = bool(
        fastsam_package["installed"] and fastsam_weights.get("available")
    )

    components: dict[str, Any] = {
        "fastsam": {
            **fastsam_package,
            "weights": fastsam_weights,
            "ready": fastsam_ready,
            "decision": "keep: primary orthophoto object-mask engine",
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
                "exclude: duplicates the Sage chat loop and Life-Harness and adds a "
                "second agent runtime, provider stack, UI, sandbox, and tool registry"
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
    }

    required = ("fastsam", "life_harness")
    return {
        "status": (
            "healthy"
            if all(components[name].get("ready") for name in required)
            else "degraded"
        ),
        "runtime_policy": "local_only",
        "source_control": "https://github.com/TontonAviella/ingabe.git",
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit local Ingabe runtime components"
    )
    parser.parse_args()
    print(json.dumps(build_runtime_audit(), indent=2, default=str))


if __name__ == "__main__":
    main()
