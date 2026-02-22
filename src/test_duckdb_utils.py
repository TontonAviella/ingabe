"""Tests for DuckDB utility functions — column quoting and reserved keyword handling."""

import pytest
from src.duckdb import quoted_col_for, DUCKDB_RESERVED_KEYWORDS


class TestQuotedColFor:
    """Column quoting for safe SQL identifier usage."""

    def test_simple_lowercase(self):
        assert quoted_col_for("name") == "name"

    def test_simple_with_underscore(self):
        assert quoted_col_for("first_name") == "first_name"

    def test_starts_with_underscore(self):
        assert quoted_col_for("_hidden") == "_hidden"

    def test_mixed_case_quoted(self):
        assert quoted_col_for("firstName") == '"firstName"'

    def test_all_uppercase_quoted(self):
        assert quoted_col_for("NAME") == '"NAME"'

    def test_starts_with_digit_quoted(self):
        assert quoted_col_for("1column") == '"1column"'

    def test_contains_space_quoted(self):
        assert quoted_col_for("my column") == '"my column"'

    def test_contains_special_chars_quoted(self):
        assert quoted_col_for("col-name") == '"col-name"'

    def test_empty_string_quoted(self):
        result = quoted_col_for("")
        assert result == '""'

    @pytest.mark.parametrize("keyword", ["select", "from", "where", "order", "group", "table"])
    def test_reserved_keywords_quoted(self, keyword):
        result = quoted_col_for(keyword)
        assert result == f'"{keyword}"'

    def test_non_reserved_not_quoted(self):
        assert quoted_col_for("area") == "area"
        assert quoted_col_for("population") == "population"

    def test_reserved_keywords_list_not_empty(self):
        assert len(DUCKDB_RESERVED_KEYWORDS) > 10
        assert "select" in DUCKDB_RESERVED_KEYWORDS
        assert "from" in DUCKDB_RESERVED_KEYWORDS
