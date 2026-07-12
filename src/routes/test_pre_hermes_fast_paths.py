from __future__ import annotations

import asyncio
from types import SimpleNamespace

import src.routes.message_routes as message_routes


def test_pre_hermes_path_stops_after_first_deterministic_match(monkeypatch) -> None:
    calls: list[str] = []

    async def messages(*_args, **_kwargs):
        return [SimpleNamespace(message_json={"role": "user", "content": "houses"})]

    def handler(name: str, handled: bool):
        async def run(**kwargs):
            calls.append(name)
            assert kwargs["openai_messages"][-1]["content"] == "houses"
            return handled

        return run

    monkeypatch.setattr(message_routes, "get_all_conversation_messages", messages)
    monkeypatch.setattr(
        message_routes,
        "_maybe_run_fast_admin_boundary_turn",
        handler("admin", False),
    )
    monkeypatch.setattr(
        message_routes,
        "_maybe_run_fast_raster_object_turn",
        handler("objects", True),
    )
    monkeypatch.setattr(
        message_routes,
        "_maybe_run_fast_raster_context_turn",
        handler("context", True),
    )
    monkeypatch.setattr(
        message_routes,
        "_maybe_run_fast_raster_fact_turn",
        handler("facts", True),
    )

    handled = asyncio.run(
        message_routes._maybe_run_deterministic_turn_before_hermes(
            map_id="Mmap",
            session=object(),
            user_id="Uuser",
            conversation=SimpleNamespace(id="Cconversation"),
        )
    )

    assert handled is True
    assert calls == ["admin", "objects"]
