"""The Hermes callback (/internal/tool-call) was removed on 2026-10-12. /internal/ stays on the SPA
fallback's JSON list in src/wsgi.py, so a stray call gets a JSON 404 rather than 200 with index.html."""


def test_removed_tool_call_route_answers_json_404(sync_client) -> None:
    response = sync_client.post("/internal/tool-call", json={"tool_name": "search_brain"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()
