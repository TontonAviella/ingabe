"""Tests for SQL validation and PostGIS readonly checks.

Covers validate_sql_query (SQL injection prevention) and
check_postgis_readonly (EXPLAIN plan enforcement).
"""

import pytest
from fastapi import HTTPException

from src.routes.message_routes import check_postgis_readonly, validate_sql_query


# ---------------------------------------------------------------------------
# validate_sql_query
# ---------------------------------------------------------------------------


class TestValidateSqlQuery:
    """SQL injection prevention tests."""

    # ── Valid queries ──────────────────────────────────────────────────

    def test_simple_select(self):
        result = validate_sql_query("SELECT * FROM parcels")
        assert result == "SELECT * FROM parcels"

    def test_select_with_where(self):
        result = validate_sql_query("SELECT id, name FROM farms WHERE area > 100")
        assert "WHERE area > 100" in result

    def test_trailing_semicolon_stripped(self):
        result = validate_sql_query("SELECT 1;")
        assert result == "SELECT 1"

    def test_leading_whitespace_stripped(self):
        result = validate_sql_query("   SELECT 1  ")
        assert result == "SELECT 1"

    def test_case_insensitive_select(self):
        result = validate_sql_query("select * from t")
        assert result == "select * from t"

    def test_select_with_subquery(self):
        result = validate_sql_query(
            "SELECT * FROM (SELECT id FROM t) AS sub"
        )
        assert "sub" in result

    # ── Empty / non-SELECT rejection ──────────────────────────────────

    def test_empty_query_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_sql_query("")
        assert exc_info.value.status_code == 400
        assert "empty" in exc_info.value.detail.lower()

    def test_whitespace_only_rejected(self):
        with pytest.raises(HTTPException):
            validate_sql_query("   ")

    def test_non_select_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_sql_query("UPDATE users SET admin = true")
        assert exc_info.value.status_code == 400

    # ── Dangerous keyword blocking ────────────────────────────────────

    @pytest.mark.parametrize(
        "dangerous_query",
        [
            "SELECT * FROM t; DROP TABLE users",
            "SELECT * FROM t; DELETE FROM users",
        ],
        ids=["drop_via_semicolon", "delete_via_semicolon"],
    )
    def test_multiple_statements_rejected(self, dangerous_query):
        with pytest.raises(HTTPException) as exc_info:
            validate_sql_query(dangerous_query)
        assert exc_info.value.status_code == 400
        assert "Multiple" in exc_info.value.detail or "semicolon" in exc_info.value.detail.lower()

    @pytest.mark.parametrize(
        "dangerous_query,keyword",
        [
            ("SELECT * FROM t WHERE INSERT INTO foo VALUES(1)", "INSERT"),
            ("SELECT * FROM t WHERE 1=1 UNION SELECT * FROM (DELETE FROM x) y", "DELETE"),
            ("SELECT DROP FROM t", "DROP"),
            ("SELECT * FROM t WHERE ALTER TABLE x", "ALTER"),
            ("SELECT TRUNCATE FROM t", "TRUNCATE"),
            ("SELECT * FROM t WHERE GRANT ALL", "GRANT"),
            ("SELECT pg_sleep(10)", "pg_sleep"),
            ("SELECT dblink('host=evil')", "dblink"),
            ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
            ("SELECT COPY t TO '/tmp/data'", "COPY"),
        ],
        ids=[
            "insert", "delete", "drop", "alter", "truncate",
            "grant", "pg_sleep", "dblink", "pg_read_file", "copy",
        ],
    )
    def test_dangerous_keywords_blocked(self, dangerous_query, keyword):
        with pytest.raises(HTTPException) as exc_info:
            validate_sql_query(dangerous_query)
        assert exc_info.value.status_code == 400
        assert "Dangerous" in exc_info.value.detail or keyword.lower() in exc_info.value.detail.lower()

    # ── Safe patterns that look dangerous but aren't ──────────────────

    def test_select_into_column_allowed(self):
        """Column named 'into' shouldn't trigger the INTO OUTFILE check."""
        # The dangerous pattern is INTO\s+OUTFILE|DUMPFILE, not bare INTO
        result = validate_sql_query("SELECT inserted_at FROM t")
        assert "inserted_at" in result


# ---------------------------------------------------------------------------
# check_postgis_readonly
# ---------------------------------------------------------------------------


class TestCheckPostgisReadonly:
    """EXPLAIN plan readonly enforcement tests."""

    def test_select_plan_allowed(self):
        plan = {"Node Type": "Seq Scan", "Relation Name": "parcels"}
        check_postgis_readonly(plan)  # should not raise

    def test_nested_select_allowed(self):
        plan = {
            "Node Type": "Hash Join",
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "a"},
                {"Node Type": "Index Scan", "Relation Name": "b"},
            ],
        }
        check_postgis_readonly(plan)  # should not raise

    def test_modify_table_rejected(self):
        plan = {"Node Type": "ModifyTable", "Operation": "Insert"}
        with pytest.raises(ValueError, match="Write operations not allowed"):
            check_postgis_readonly(plan)

    def test_nested_modify_rejected(self):
        plan = {
            "Node Type": "Hash Join",
            "Plans": [
                {"Node Type": "Seq Scan"},
                {
                    "Node Type": "Nested Loop",
                    "Plans": [{"Node Type": "ModifyTable"}],
                },
            ],
        }
        with pytest.raises(ValueError, match="Write operations not allowed"):
            check_postgis_readonly(plan)

    def test_empty_plan(self):
        check_postgis_readonly({})  # should not raise
