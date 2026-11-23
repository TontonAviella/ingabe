"""The order in which the chat loop tries its deterministic fast paths (_run_first_fast_path)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import src.routes.message_routes as message_routes


def _patch_handlers(monkeypatch, calls: list[str], answers: dict[str, bool]) -> None:
    def handler(name: str):
        async def run(**kwargs):
            calls.append(name)
            assert kwargs["openai_messages"][-1]["content"] == "houses"
            return answers[name]

        return run

    monkeypatch.setattr(message_routes, "_maybe_run_fast_admin_boundary_turn", handler("admin"))
    monkeypatch.setattr(message_routes, "_maybe_run_fast_raster_object_turn", handler("objects"))
    monkeypatch.setattr(message_routes, "_maybe_run_fast_satellite_view_turn", handler("satellite"))
    monkeypatch.setattr(message_routes, "_maybe_run_fast_raster_context_turn", handler("context"))
    monkeypatch.setattr(message_routes, "_maybe_run_fast_raster_fact_turn", handler("facts"))


def _run() -> str | None:
    return asyncio.run(
        message_routes._run_first_fast_path(
            map_id="Mmap",
            session=object(),
            user_id="Uuser",
            conversation=SimpleNamespace(id="Cconversation"),
            openai_messages=[{"role": "user", "content": "houses"}],
        )
    )


def test_first_fast_path_stops_after_first_match(monkeypatch) -> None:
    calls: list[str] = []
    _patch_handlers(monkeypatch, calls, {"admin": False, "objects": True, "satellite": True, "context": True, "facts": True})

    assert _run() == "raster_object"
    assert calls == ["admin", "objects"]


def test_no_fast_path_tries_all_five_in_order_and_leaves_the_turn_to_the_model(monkeypatch) -> None:
    calls: list[str] = []
    _patch_handlers(monkeypatch, calls, {"admin": False, "objects": False, "satellite": False, "context": False, "facts": False})

    assert _run() is None
    assert calls == ["admin", "objects", "satellite", "context", "facts"]
