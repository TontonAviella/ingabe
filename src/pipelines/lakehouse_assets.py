"""Dagster assets for Iceberg lakehouse maintenance.

Wraps the lakehouse manager operations (src/services/lakehouse.py) for
scheduled maintenance tasks like compaction, snapshot expiry, and
table optimization.
"""

import json
import logging
from typing import Any

from dagster import AssetExecutionContext, asset

from src.pipelines.resources import PostgresResource
from src.services.lakehouse import get_lakehouse_manager

logger = logging.getLogger(__name__)


@asset(
    description="Compact small data files in Iceberg tables",
    metadata={
        "dagster/group": "lakehouse_maintenance",
    },
)
def iceberg_compaction(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Compact small Iceberg data files into larger ones.

    Improves query performance by reducing the number of small files
    that need to be read. Runs on Iceberg-registered vector tables.

    Returns:
        Dict containing compaction results
    """
    # Find Iceberg-registered tables
    query = """
        SELECT layer_id, name, metadata
        FROM map_layers
        WHERE type = 'vector'
        AND (metadata->>'iceberg_registered')::boolean = true
        LIMIT 10
    """

    results = postgres.execute_query(query)

    if not results:
        context.log.info("No Iceberg tables found for compaction")
        return {"status": "no_tables", "count": 0}

    lakehouse = get_lakehouse_manager()
    compacted = []
    errors = []

    for layer_id, name, metadata_json in results:
        try:
            context.log.info(f"Compacting Iceberg table for layer {layer_id}")
            result = lakehouse.compact_table(layer_id)

            if result["status"] == "success":
                compacted.append({
                    "layer_id": layer_id,
                    "snapshot_id": result.get("snapshot_id"),
                })
                context.log.info(f"Compaction completed for {layer_id}")
            elif result["status"] == "skipped":
                context.log.info(f"Compaction skipped for {layer_id}: {result['message']}")
            else:
                errors.append({
                    "layer_id": layer_id,
                    "error": result.get("message", "Unknown error"),
                })

        except Exception as e:
            context.log.error(f"Compaction failed for {layer_id}: {e}")
            errors.append({"layer_id": layer_id, "error": str(e)})

    return {
        "status": "success",
        "compacted": compacted,
        "errors": errors,
        "count": len(compacted),
    }


@asset(
    description="Expire old Iceberg snapshots to reclaim storage",
    metadata={
        "dagster/group": "lakehouse_maintenance",
    },
)
def snapshot_expiry(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Expire old Iceberg table snapshots.

    Removes snapshots older than 7 days to reclaim storage space.
    Preserves recent snapshots for time-travel queries.

    Returns:
        Dict containing expiry results
    """
    # Find Iceberg-registered tables
    query = """
        SELECT layer_id, name
        FROM map_layers
        WHERE type = 'vector'
        AND (metadata->>'iceberg_registered')::boolean = true
        LIMIT 10
    """

    results = postgres.execute_query(query)

    if not results:
        context.log.info("No Iceberg tables found for snapshot expiry")
        return {"status": "no_tables", "count": 0}

    lakehouse = get_lakehouse_manager()
    expired = []
    errors = []

    for layer_id, name in results:
        try:
            context.log.info(f"Expiring old snapshots for layer {layer_id}")
            result = lakehouse.expire_snapshots(
                layer_id=layer_id,
                older_than_days=7,
            )

            if result["status"] == "success":
                expired.append({
                    "layer_id": layer_id,
                    "cutoff_days": result["cutoff_days"],
                })
                context.log.info(f"Snapshot expiry completed for {layer_id}")
            else:
                errors.append({
                    "layer_id": layer_id,
                    "error": result.get("message", "Unknown error"),
                })

        except Exception as e:
            context.log.error(f"Snapshot expiry failed for {layer_id}: {e}")
            errors.append({"layer_id": layer_id, "error": str(e)})

    return {
        "status": "success",
        "expired": expired,
        "errors": errors,
        "count": len(expired),
    }


@asset(
    description="Optimize Iceberg table layout",
    metadata={
        "dagster/group": "lakehouse_maintenance",
    },
)
def table_optimization(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Optimize Iceberg table file layout and organization.

    Rewrites data files to improve query performance by:
    - Combining small files into optimal-sized files
    - Reorganizing data for better locality
    - Updating statistics for query planning

    Returns:
        Dict containing optimization results
    """
    # Find Iceberg-registered tables
    query = """
        SELECT layer_id, name
        FROM map_layers
        WHERE type = 'vector'
        AND (metadata->>'iceberg_registered')::boolean = true
        LIMIT 5
    """

    results = postgres.execute_query(query)

    if not results:
        context.log.info("No Iceberg tables found for optimization")
        return {"status": "no_tables", "count": 0}

    lakehouse = get_lakehouse_manager()
    optimized = []
    errors = []

    for layer_id, name in results:
        try:
            context.log.info(f"Optimizing table layout for layer {layer_id}")
            result = lakehouse.optimize_table_layout(layer_id)

            if result["status"] == "success":
                optimized.append({"layer_id": layer_id})
                context.log.info(f"Table optimization completed for {layer_id}")
            else:
                errors.append({
                    "layer_id": layer_id,
                    "error": result.get("message", "Unknown error"),
                })

        except Exception as e:
            context.log.error(f"Table optimization failed for {layer_id}: {e}")
            errors.append({"layer_id": layer_id, "error": str(e)})

    return {
        "status": "success",
        "optimized": optimized,
        "errors": errors,
        "count": len(optimized),
    }
