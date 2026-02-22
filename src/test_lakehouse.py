"""Tests for Apache Iceberg lakehouse integration.

Tests cover:
- Table creation from existing layers
- Table listing and metadata retrieval
- Time-travel queries
- Snapshot management
- Error handling for edge cases
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from fastapi import HTTPException

pyiceberg = pytest.importorskip("pyiceberg", reason="pyiceberg not installed")

from src.services.lakehouse import LakehouseManager, get_lakehouse_manager


@pytest.fixture
def lakehouse_manager():
    """Fixture providing a LakehouseManager instance."""
    return LakehouseManager(catalog_name="test_catalog")


@pytest.fixture
def mock_catalog():
    """Fixture providing a mocked PyIceberg catalog."""
    catalog = Mock()
    catalog.list_tables.return_value = []
    catalog.create_namespace.return_value = None
    return catalog


@pytest.fixture
def mock_table():
    """Fixture providing a mocked Iceberg table."""
    table = Mock()
    table.location.return_value = "s3://test-bucket/lakehouse/warehouse/test_table"

    # Mock metadata
    metadata = Mock()
    metadata.current_snapshot_id = 123456
    metadata.format_version = 2
    metadata.properties = {"write.format.default": "parquet"}
    metadata.snapshots = []

    table.metadata = metadata

    # Mock schema
    schema = Mock()
    schema.fields = []
    table.schema.return_value = schema

    return table


class TestLakehouseManager:
    """Test suite for LakehouseManager class."""

    def test_initialization(self, lakehouse_manager):
        """Test LakehouseManager initialization with default values."""
        assert lakehouse_manager.catalog_name == "test_catalog"
        assert "s3://test-bucket/lakehouse" in lakehouse_manager.warehouse_location

    def test_build_postgres_uri(self, lakehouse_manager):
        """Test PostgreSQL URI construction from environment variables."""
        uri = lakehouse_manager._build_postgres_uri()

        assert "postgresql+psycopg2://" in uri
        assert "mundiuser" in uri or "POSTGRES_USER" in uri
        assert "mundidb" in uri or "POSTGRES_DB" in uri

    @patch("src.services.lakehouse.load_catalog")
    def test_get_catalog(self, mock_load_catalog, lakehouse_manager, mock_catalog):
        """Test catalog initialization and caching."""
        mock_load_catalog.return_value = mock_catalog

        # First call should initialize catalog
        catalog1 = lakehouse_manager._get_catalog()
        assert catalog1 is not None
        assert mock_load_catalog.called

        # Second call should return cached catalog
        mock_load_catalog.reset_mock()
        catalog2 = lakehouse_manager._get_catalog()
        assert catalog2 is catalog1
        assert not mock_load_catalog.called

    @patch.object(LakehouseManager, "_get_catalog")
    def test_list_tables_empty(self, mock_get_catalog, lakehouse_manager, mock_catalog):
        """Test listing tables when namespace is empty."""
        mock_get_catalog.return_value = mock_catalog
        mock_catalog.list_tables.return_value = []

        result = lakehouse_manager.list_tables(namespace="test_namespace")

        assert isinstance(result, list)
        assert len(result) == 0
        mock_catalog.list_tables.assert_called_once_with("test_namespace")

    @patch.object(LakehouseManager, "_get_catalog")
    def test_list_tables_with_data(self, mock_get_catalog, lakehouse_manager, mock_catalog, mock_table):
        """Test listing tables with data."""
        mock_get_catalog.return_value = mock_catalog
        mock_catalog.list_tables.return_value = [("test_namespace", "table1")]
        mock_catalog.load_table.return_value = mock_table

        result = lakehouse_manager.list_tables(namespace="test_namespace")

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "table1"
        assert result[0]["namespace"] == "test_namespace"
        assert "location" in result[0]

    @patch.object(LakehouseManager, "_get_catalog")
    def test_get_table_not_found(self, mock_get_catalog, lakehouse_manager, mock_catalog):
        """Test getting a table that doesn't exist."""
        from pyiceberg.exceptions import NoSuchTableError

        mock_get_catalog.return_value = mock_catalog
        mock_catalog.load_table.side_effect = NoSuchTableError("Table not found")

        result = lakehouse_manager.get_table("nonexistent_layer")
        assert result is None

    @patch.object(LakehouseManager, "_get_catalog")
    def test_get_table_success(self, mock_get_catalog, lakehouse_manager, mock_catalog, mock_table):
        """Test successfully getting a table."""
        mock_get_catalog.return_value = mock_catalog
        mock_catalog.load_table.return_value = mock_table

        result = lakehouse_manager.get_table("test_layer")

        assert result is not None
        assert result is mock_table

    @patch.object(LakehouseManager, "get_table")
    def test_get_table_metadata_not_found(self, mock_get_table, lakehouse_manager):
        """Test getting metadata for non-existent table raises HTTPException."""
        mock_get_table.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            lakehouse_manager.get_table_metadata("nonexistent_layer")

        assert exc_info.value.status_code == 404
        assert "not found" in str(exc_info.value.detail).lower()

    @patch.object(LakehouseManager, "get_table")
    def test_get_table_metadata_success(self, mock_get_table, lakehouse_manager, mock_table):
        """Test successfully getting table metadata."""
        mock_get_table.return_value = mock_table

        result = lakehouse_manager.get_table_metadata("test_layer")

        assert result["name"] == "layer_test_layer"
        assert result["namespace"] == "vector_layers"
        assert "location" in result
        assert "schema" in result
        assert "snapshots" in result
        assert result["format_version"] == 2

    @patch.object(LakehouseManager, "_get_catalog")
    def test_drop_table_success(self, mock_get_catalog, lakehouse_manager, mock_catalog):
        """Test successfully dropping a table."""
        mock_get_catalog.return_value = mock_catalog
        mock_catalog.drop_table.return_value = True

        result = lakehouse_manager.drop_table("test_layer")

        assert "message" in result
        assert "dropped successfully" in result["message"].lower()
        mock_catalog.drop_table.assert_called_once_with("vector_layers.layer_test_layer")

    @patch.object(LakehouseManager, "_get_catalog")
    def test_drop_table_not_found(self, mock_get_catalog, lakehouse_manager, mock_catalog):
        """Test dropping a non-existent table raises HTTPException."""
        from pyiceberg.exceptions import NoSuchTableError

        mock_get_catalog.return_value = mock_catalog
        mock_catalog.drop_table.side_effect = NoSuchTableError("Table not found")

        with pytest.raises(HTTPException) as exc_info:
            lakehouse_manager.drop_table("nonexistent_layer")

        assert exc_info.value.status_code == 404

    @patch("src.services.lakehouse.get_lakehouse_connection")
    @patch.object(LakehouseManager, "get_table")
    def test_query_table_success(self, mock_get_table, mock_get_connection, lakehouse_manager, mock_table):
        """Test successfully querying a table."""
        mock_get_table.return_value = mock_table

        # Mock DuckDB connection
        mock_con = Mock()
        mock_cursor = Mock()
        mock_cursor.description = [("col1",), ("col2",)]
        mock_cursor.fetchall.return_value = [("val1", "val2"), ("val3", "val4")]
        mock_con.execute.return_value = mock_cursor
        mock_get_connection.return_value = mock_con

        result = lakehouse_manager.query_table("test_layer", limit=10)

        assert "headers" in result
        assert "rows" in result
        assert "row_count" in result
        assert result["headers"] == ["col1", "col2"]
        assert len(result["rows"]) == 2
        mock_con.close.assert_called_once()

    @patch("src.services.lakehouse.get_lakehouse_connection")
    @patch.object(LakehouseManager, "get_table")
    def test_query_table_with_where_clause(self, mock_get_table, mock_get_connection, lakehouse_manager, mock_table):
        """Test querying a table with WHERE clause."""
        mock_get_table.return_value = mock_table

        mock_con = Mock()
        mock_cursor = Mock()
        mock_cursor.description = [("col1",)]
        mock_cursor.fetchall.return_value = [("filtered_val",)]
        mock_con.execute.return_value = mock_cursor
        mock_get_connection.return_value = mock_con

        result = lakehouse_manager.query_table("test_layer", sql_where="col1 = 'value'", limit=10)

        assert result["row_count"] == 1
        # Verify WHERE clause was included in query
        call_args = mock_con.execute.call_args[0][0]
        assert "WHERE" in call_args
        assert "col1 = 'value'" in call_args

    @patch("src.services.lakehouse.get_lakehouse_connection")
    @patch.object(LakehouseManager, "get_table")
    def test_query_table_time_travel(self, mock_get_table, mock_get_connection, lakehouse_manager, mock_table):
        """Test time-travel query with snapshot ID."""
        mock_get_table.return_value = mock_table

        mock_con = Mock()
        mock_cursor = Mock()
        mock_cursor.description = [("col1",)]
        mock_cursor.fetchall.return_value = [("historical_val",)]
        mock_con.execute.return_value = mock_cursor
        mock_get_connection.return_value = mock_con

        result = lakehouse_manager.query_table("test_layer", snapshot_id=123456, limit=10)

        assert result["row_count"] == 1
        # Verify snapshot ID was included in query
        call_args = mock_con.execute.call_args[0][0]
        assert "version" in call_args
        assert "123456" in call_args

    @patch.object(LakehouseManager, "get_table")
    def test_query_table_not_found(self, mock_get_table, lakehouse_manager):
        """Test querying a non-existent table raises HTTPException."""
        mock_get_table.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            lakehouse_manager.query_table("nonexistent_layer")

        assert exc_info.value.status_code == 404

    @patch.object(LakehouseManager, "get_table")
    def test_expire_snapshots_success(self, mock_get_table, lakehouse_manager, mock_table):
        """Test successfully expiring snapshots."""
        mock_get_table.return_value = mock_table
        mock_table.expire_snapshots = Mock()

        result = lakehouse_manager.expire_snapshots("test_layer", older_than_days=7)

        assert result["status"] == "success"
        assert result["layer_id"] == "test_layer"
        mock_table.expire_snapshots.assert_called_once()

    @patch.object(LakehouseManager, "get_table")
    def test_expire_snapshots_table_not_found(self, mock_get_table, lakehouse_manager):
        """Test expiring snapshots for non-existent table."""
        mock_get_table.return_value = None

        result = lakehouse_manager.expire_snapshots("nonexistent_layer")

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    @patch.object(LakehouseManager, "get_table")
    def test_compact_table_success(self, mock_get_table, lakehouse_manager, mock_table):
        """Test table compaction scheduling."""
        mock_snapshot = Mock()
        mock_snapshot.snapshot_id = 789

        mock_get_table.return_value = mock_table
        mock_table.current_snapshot.return_value = mock_snapshot

        result = lakehouse_manager.compact_table("test_layer")

        assert result["status"] == "success"
        assert result["layer_id"] == "test_layer"
        assert result["snapshot_id"] == 789

    @patch.object(LakehouseManager, "get_table")
    def test_compact_table_no_snapshots(self, mock_get_table, lakehouse_manager, mock_table):
        """Test compaction when table has no snapshots."""
        mock_get_table.return_value = mock_table
        mock_table.current_snapshot.return_value = None

        result = lakehouse_manager.compact_table("test_layer")

        assert result["status"] == "skipped"
        assert "no snapshots" in result["message"].lower()


def test_get_lakehouse_manager_singleton():
    """Test that get_lakehouse_manager returns a singleton instance."""
    manager1 = get_lakehouse_manager()
    manager2 = get_lakehouse_manager()

    assert manager1 is manager2
    assert isinstance(manager1, LakehouseManager)


@pytest.mark.asyncio
async def test_create_table_from_layer_nonexistent_layer(lakehouse_manager):
    """Test creating table from non-existent layer raises HTTPException."""
    with patch("src.services.lakehouse.get_async_db_connection") as mock_db:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_db.return_value = AsyncMock()
        mock_db.return_value.__aenter__.return_value = mock_conn

        with pytest.raises(HTTPException) as exc_info:
            await lakehouse_manager.create_table_from_layer("nonexistent_layer")

        assert exc_info.value.status_code == 404
        assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_create_table_from_raster_layer(lakehouse_manager):
    """Test that creating table from raster layer raises HTTPException."""
    with patch("src.services.lakehouse.get_async_db_connection") as mock_db:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={
            "layer_id": "test_layer",
            "type": "raster",  # Not a vector layer
        })
        mock_db.return_value = AsyncMock()
        mock_db.return_value.__aenter__.return_value = mock_conn

        with pytest.raises(HTTPException) as exc_info:
            await lakehouse_manager.create_table_from_layer("test_layer")

        assert exc_info.value.status_code == 400
        assert "vector" in str(exc_info.value.detail).lower()
