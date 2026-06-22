import pytest
from fastapi import HTTPException

from src.routes.message_routes import (
    _validate_internal_rwanda_query,
    _rwanda_internal_conn_id,
    _rwanda_internal_summary_id,
)


def test_rwanda_internal_ids_are_project_scoped_and_fit_db_columns():
    first = _rwanda_internal_conn_id("project-one")
    second = _rwanda_internal_conn_id("project-two")
    summary = _rwanda_internal_summary_id("project-one")

    assert first.startswith("C")
    assert summary.startswith("S")
    assert len(first) == 12
    assert len(summary) == 12
    assert first != second


def test_internal_rwanda_query_allowlist_blocks_app_tables():
    _validate_internal_rwanda_query(
        "SELECT district AS id, district, geom FROM rwanda_district_boundaries"
    )

    with pytest.raises(HTTPException):
        _validate_internal_rwanda_query(
            "SELECT id, owner_uuid FROM user_mundiai_maps LIMIT 10"
        )
