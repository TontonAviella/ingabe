import time

from src.dependencies.sage_routing import USER_RASTER
from src.services import sage_tool_observability as obs


class FakeSession:
    def get_user_id(self):
        return "user-1"

    def get_org_id(self):
        return "org-1"


def test_build_sage_tool_context_records_route_alignment_without_arg_values():
    context = obs.build_sage_tool_context(
        tool_name="create_raster_h3_context_layer",
        tool_args={"layer_id": "Lsecret", "analysis_goal": "private prompt"},
        routing_reason="intent:spatial_insight,user_raster",
        selected_categories={USER_RASTER, "spatial_insight"},
        tool_registry="pydantic",
        map_id="M1",
        project_id="P1",
        conversation_id=7,
        client_turn_id="turn_abc",
        message_id="42",
    )

    assert context["tool_name"] == "create_raster_h3_context_layer"
    assert context["tool_category"] == "spatial_insight"
    assert context["routing_alignment"] == "matched"
    assert context["tool_arg_key_count"] == 2
    assert context["tool_arg_keys_csv"] == "analysis_goal,layer_id"
    assert context["client_turn_id"] == "turn_abc"
    assert context["message_id"] == "42"
    assert "Lsecret" not in str(context)
    assert "private prompt" not in str(context)


def test_summarize_tool_result_uses_status_and_result_keys_without_payload_values():
    summary = obs.summarize_tool_result(
        {
            "status": "success",
            "layer_id": "Lsecret",
            "pmtiles_key": "pmtiles/private.pmtiles",
        }
    )

    assert summary["tool_status"] == "success"
    assert summary["tool_success"] is True
    assert summary["tool_has_error"] is False
    assert summary["result_key_count"] == 3
    assert summary["result_keys_csv"] == "layer_id,pmtiles_key,status"
    assert "Lsecret" not in str(summary)


def test_capture_sage_routing_decision_includes_turn_correlation(monkeypatch):
    calls = []

    def fake_capture(event, session, properties):
        calls.append((event, session, properties))
        return True

    monkeypatch.setattr(obs, "capture_for_session", fake_capture)

    captured = obs.capture_sage_routing_decision(
        session=FakeSession(),
        map_id="M1",
        project_id="P1",
        conversation_id=7,
        routing_reason="intent:user_raster",
        selected_categories={USER_RASTER},
        is_small_talk=False,
        model="model-a",
        tool_count=12,
        user_message_length=31,
        tool_payload_bytes=2048,
        client_turn_id="turn_abc",
        message_id="42",
    )

    assert captured is True
    assert calls[0][0] == obs.SAGE_ROUTING_DECISION_EVENT
    props = calls[0][2]
    assert props["client_turn_id"] == "turn_abc"
    assert props["message_id"] == "42"
    assert props["selected_categories_csv"] == USER_RASTER


def test_capture_sage_tool_result_message_pops_context_and_captures(monkeypatch):
    calls = []

    def fake_capture(event, session, properties):
        calls.append((event, session, properties))
        return True

    monkeypatch.setattr(obs, "capture_for_session", fake_capture)
    contexts = {
        "call_1": {
            "started_at": time.monotonic() - 0.05,
            "tool_name": "describe_user_raster",
            "tool_category": "user_raster",
            "tool_registry": "pydantic",
            "routing_reason": "intent:user_raster",
            "routing_selected_categories_csv": "user_raster",
            "routing_alignment": "matched",
            "tool_arg_key_count": 1,
            "tool_arg_keys_csv": "layer_id",
            "map_id": "M1",
            "project_id": "P1",
            "conversation_id": 7,
        }
    }

    captured = obs.capture_sage_tool_result_message(
        message={
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"status":"success","area_hectares":12.3}',
        },
        context_by_tool_call_id=contexts,
        session=FakeSession(),
    )

    assert captured is True
    assert contexts == {}
    assert calls[0][0] == obs.SAGE_TOOL_COMPLETED_EVENT
    props = calls[0][2]
    assert props["tool_name"] == "describe_user_raster"
    assert props["tool_status"] == "success"
    assert props["tool_success"] is True
    assert props["result_keys_csv"] == "area_hectares,status"
    assert props["elapsed_ms"] >= 0
