"""Tests for SQL injection validation in query_postgis_database.

Confirms that validate_sql_query blocks dangerous patterns before
the LLM-generated SQL reaches the database.
"""

import pytest
from fastapi import HTTPException

from src.routes.message_routes import validate_sql_query


def test_valid_select_passes():
    """A plain SELECT with LIMIT should pass through unchanged."""
    sql = "SELECT id, name FROM parcels WHERE area > 100 LIMIT 10"
    result = validate_sql_query(sql)
    assert "SELECT" in result


def test_pg_sleep_blocked():
    """pg_sleep injection must be rejected."""
    with pytest.raises(HTTPException) as exc_info:
        validate_sql_query("SELECT pg_sleep(10)")
    assert exc_info.value.status_code == 400


def test_dblink_blocked():
    """dblink lateral-movement attempt must be rejected."""
    with pytest.raises(HTTPException) as exc_info:
        validate_sql_query(
            "SELECT * FROM dblink('host=evil.com dbname=x', 'SELECT 1') AS t(a int)"
        )
    assert exc_info.value.status_code == 400


def test_multi_statement_blocked():
    """Multiple SQL statements (semicolon injection) must be rejected."""
    with pytest.raises(HTTPException) as exc_info:
        validate_sql_query("SELECT 1; DROP TABLE users")
    assert exc_info.value.status_code == 400


def test_non_select_blocked():
    """Non-SELECT queries (INSERT, UPDATE, DELETE, etc.) must be rejected."""
    for stmt in [
        "INSERT INTO users VALUES (1, 'evil')",
        "UPDATE users SET name='evil'",
        "DELETE FROM users",
        "DROP TABLE users",
    ]:
        with pytest.raises(HTTPException) as exc_info:
            validate_sql_query(stmt)
        assert exc_info.value.status_code == 400, f"Should block: {stmt}"


def test_pg_read_file_blocked():
    """pg_read_file file-read exploit must be rejected."""
    with pytest.raises(HTTPException) as exc_info:
        validate_sql_query("SELECT pg_read_file('/etc/passwd')")
    assert exc_info.value.status_code == 400
