"""REST API routes for Apache Iceberg lakehouse operations.

Provides endpoints for:
- Creating Iceberg tables from existing layers
- Querying tables with time-travel support
- Managing table schemas and snapshots
- Table lifecycle operations (create, drop, list)
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, status, Depends, Query
from pydantic import BaseModel, Field

from src.dependencies.session import verify_session_required, UserContext
from src.services.lakehouse import get_lakehouse_manager

logger = logging.getLogger(__name__)

lakehouse_router = APIRouter()


# Request/Response models
class CreateTableRequest(BaseModel):
    layer_id: str = Field(..., description="ID of the layer to convert to Iceberg table")
    table_name: Optional[str] = Field(None, description="Optional table name (defaults to layer_id)")


class QueryTableRequest(BaseModel):
    where: Optional[str] = Field(None, description="SQL WHERE clause (without WHERE keyword)")
    limit: int = Field(1000, ge=1, le=10000, description="Maximum number of rows to return")
    snapshot_id: Optional[int] = Field(None, description="Snapshot ID for time-travel queries")


# Routes

@lakehouse_router.get(
    "/lakehouse/tables",
    operation_id="list_lakehouse_tables",
)
async def list_tables(
    namespace: str = Query("vector_layers", description="Iceberg namespace to list tables from"),
    session: UserContext = Depends(verify_session_required),
):
    """List all Iceberg tables in a namespace.

    Returns metadata for each table including schema, current snapshot, and storage location.
    """
    def _list():
        manager = get_lakehouse_manager()
        return manager.list_tables(namespace=namespace)

    loop = asyncio.get_running_loop()
    tables = await loop.run_in_executor(None, _list)

    return {
        "namespace": namespace,
        "tables": tables,
        "count": len(tables),
    }


@lakehouse_router.post(
    "/lakehouse/tables",
    operation_id="create_lakehouse_table",
    status_code=status.HTTP_201_CREATED,
)
async def create_table(
    request: CreateTableRequest,
    session: UserContext = Depends(verify_session_required),
):
    """Create a new Iceberg table from an existing map layer.

    Converts a vector layer stored in S3 to an Iceberg table with ACID properties.
    The table will be stored in the lakehouse warehouse on S3 with metadata in PostgreSQL.
    """
    manager = get_lakehouse_manager()

    try:
        result = await manager.create_table_from_layer(
            layer_id=request.layer_id,
            table_name=request.table_name,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create Iceberg table from layer {request.layer_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Table creation failed: {str(e)}"
        )


@lakehouse_router.get(
    "/lakehouse/tables/{layer_id}",
    operation_id="get_lakehouse_table_metadata",
)
async def get_table_metadata(
    layer_id: str,
    session: UserContext = Depends(verify_session_required),
):
    """Get detailed metadata for an Iceberg table.

    Returns schema, partition specification, snapshots, and table properties.
    """
    def _get_metadata():
        manager = get_lakehouse_manager()
        return manager.get_table_metadata(layer_id)

    loop = asyncio.get_running_loop()
    try:
        metadata = await loop.run_in_executor(None, _get_metadata)
        return metadata
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get metadata for table {layer_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve table metadata: {str(e)}"
        )


@lakehouse_router.post(
    "/lakehouse/tables/{layer_id}/query",
    operation_id="query_lakehouse_table",
)
async def query_table(
    layer_id: str,
    request: QueryTableRequest,
    session: UserContext = Depends(verify_session_required),
):
    """Query an Iceberg table using DuckDB with optional time-travel.

    Supports:
    - SQL WHERE clauses for filtering
    - Time-travel queries via snapshot_id
    - Efficient columnar queries via DuckDB
    """
    def _query():
        manager = get_lakehouse_manager()
        return manager.query_table(
            layer_id=layer_id,
            sql_where=request.where,
            limit=request.limit,
            snapshot_id=request.snapshot_id,
        )

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _query)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Query failed for table {layer_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query execution failed: {str(e)}"
        )


@lakehouse_router.delete(
    "/lakehouse/tables/{layer_id}",
    operation_id="drop_lakehouse_table",
)
async def drop_table(
    layer_id: str,
    session: UserContext = Depends(verify_session_required),
):
    """Drop an Iceberg table.

    WARNING: This permanently deletes the table metadata and data files from S3.
    This operation cannot be undone.
    """
    def _drop():
        manager = get_lakehouse_manager()
        return manager.drop_table(layer_id)

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _drop)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to drop table {layer_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to drop table: {str(e)}"
        )


@lakehouse_router.get(
    "/lakehouse/tables/{layer_id}/snapshots",
    operation_id="list_table_snapshots",
)
async def list_snapshots(
    layer_id: str,
    session: UserContext = Depends(verify_session_required),
):
    """List all snapshots for an Iceberg table.

    Snapshots enable time-travel queries and provide a history of table changes.
    Each snapshot includes timestamp, parent snapshot, and operation summary.
    """
    def _list_snapshots():
        manager = get_lakehouse_manager()
        metadata = manager.get_table_metadata(layer_id)
        return metadata.get("snapshots", [])

    loop = asyncio.get_running_loop()
    try:
        snapshots = await loop.run_in_executor(None, _list_snapshots)
        return {
            "layer_id": layer_id,
            "snapshots": snapshots,
            "count": len(snapshots),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list snapshots for table {layer_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list snapshots: {str(e)}"
        )


@lakehouse_router.post(
    "/lakehouse/tables/{layer_id}/expire-snapshots",
    operation_id="expire_table_snapshots",
)
async def expire_snapshots(
    layer_id: str,
    older_than_days: int = Query(7, ge=1, le=365, description="Expire snapshots older than this many days"),
    session: UserContext = Depends(verify_session_required),
):
    """Expire old snapshots to reclaim storage.

    Removes snapshots older than the specified threshold. This helps manage storage costs
    by cleaning up old table versions that are no longer needed.
    """
    def _expire():
        manager = get_lakehouse_manager()
        return manager.expire_snapshots(layer_id, older_than_days=older_than_days)

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _expire)
        return result
    except Exception as e:
        logger.error(f"Failed to expire snapshots for table {layer_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Snapshot expiry failed: {str(e)}"
        )


@lakehouse_router.post(
    "/lakehouse/tables/{layer_id}/compact",
    operation_id="compact_table",
)
async def compact_table(
    layer_id: str,
    session: UserContext = Depends(verify_session_required),
):
    """Trigger table compaction to optimize file sizes.

    Combines small Parquet files into larger ones for better query performance.
    This is typically scheduled as a background job via Dagster in Phase 3.
    """
    def _compact():
        manager = get_lakehouse_manager()
        return manager.compact_table(layer_id)

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _compact)
        return result
    except Exception as e:
        logger.error(f"Failed to compact table {layer_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Compaction failed: {str(e)}"
        )
