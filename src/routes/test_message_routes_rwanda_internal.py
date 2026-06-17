from src.routes.message_routes import (
    _rwanda_internal_conn_id,
    _rwanda_internal_summary_id,
)


def test_rwanda_internal_ids_are_project_scoped_and_fit_db_columns():
    first = _rwanda_internal_conn_id("project-one")
    second = _rwanda_internal_conn_id("project-two")
    summary = _rwanda_internal_summary_id("project-one")

    assert first.startswith("CRw")
    assert summary.startswith("SRw")
    assert len(first) == 12
    assert len(summary) == 12
    assert first != second
