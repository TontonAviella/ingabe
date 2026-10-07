from datetime import datetime, timezone

import pytest

from src.services.brain_context import (
    build_brain_context_packet,
    extract_user_message_text,
)
from src.services.brain_service import Page, SearchResult


def _page(slug: str, title: str, truth: str) -> Page:
    return Page(
        id=1,
        slug=slug,
        type="field",
        title=title,
        compiled_truth=truth,
        timeline="",
        frontmatter={},
        content_hash=None,
        owner_uuid="00000000-0000-0000-0000-000000000001",
        viewer_uuids=[],
        editor_uuids=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


class LayerOnlyBrain:
    async def search_hybrid(self, conn, query, embedding=None, limit=None, type=None):
        return [
            SearchResult(
                slug="layer-lold123",
                page_id=11,
                title="Old demo layer",
                type="field",
                chunk_text="This old layer should not appear on a blank current map.",
                chunk_source="compiled_truth",
                score=0.91,
            )
        ]

    async def get_pages_in_bbox(self, conn, bbox, limit=50, type=None):
        return [
            _page(
                "layer-lold123-f0",
                "Old demo feature",
                "This old feature intersects the viewport but is not visible on the map.",
            )
        ]

    async def list_pages(self, conn, limit=100, offset=0, type=None, tag=None):
        return [_page("layer-lold123", "Old demo layer", "Recent old layer fallback.")]


class FakeBrain:
    async def search_hybrid(self, conn, query, embedding=None, limit=None, type=None):
        return [
            SearchResult(
                slug="raster-rgb-1",
                page_id=10,
                title="RGB drone flight",
                type="field",
                chunk_text="Orthophoto shows storm-damaged maize in the eastern plots.",
                chunk_source="compiled_truth",
                score=0.82,
            )
        ]

    async def get_pages_in_bbox(self, conn, bbox, limit=50, type=None):
        return [_page("field-gasabo", "Gasabo maize field", "Field is inside the active viewport.")]

    async def list_pages(self, conn, limit=100, offset=0, type=None, tag=None):
        return [_page("recent-field", "Recent field", "Recent Brain fallback.")]


class FakeConn:
    async def fetch(self, query, *args):
        return [
            {
                "slug": "raster-rgb-1",
                "title": "RGB drone flight",
                "frontmatter": {
                    "layer_id": "layer-rgb-1",
                },
                "updated_at": datetime(2026, 6, 9, tzinfo=timezone.utc),
            }
        ]


def test_extract_user_message_text_supports_content_parts():
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Find damage"},
            {"type": "input_text", "input_text": "near this field"},
        ],
    }

    assert extract_user_message_text(message) == "Find damage\nnear this field"


@pytest.mark.asyncio
async def test_build_brain_context_packet_includes_query_and_spatial_memory():
    packet = await build_brain_context_packet(
        FakeConn(),
        FakeBrain(),
        query_text="Where have we seen this damage before?",
        viewport_bounds=[30.0, -2.0, 30.2, -1.8],
    )

    assert packet is not None
    assert '<BrainContext format="memory_packet">' in packet
    assert "source=query" in packet
    assert "source=spatial" in packet
    assert "visual index:" not in packet


@pytest.mark.asyncio
async def test_brain_context_filters_layer_memories_when_current_map_has_no_layers():
    packet = await build_brain_context_packet(
        FakeConn(),
        LayerOnlyBrain(),
        query_text="What visible layers are on this map?",
        viewport_bounds=[-75.0, -75.0, 75.0, 75.0],
        visible_layer_ids=[],
    )

    assert packet is None


@pytest.mark.asyncio
async def test_brain_context_keeps_only_visible_layer_memories():
    packet = await build_brain_context_packet(
        FakeConn(),
        LayerOnlyBrain(),
        query_text="What visible layers are on this map?",
        viewport_bounds=[-75.0, -75.0, 75.0, 75.0],
        visible_layer_ids=["Lold123"],
    )

    assert packet is not None
    assert "Old demo layer" in packet
    assert "Old demo feature" in packet


class OrthophotoBrain:
    """The 2026-10-07 turn: the question matched no page, and the only page in
    the viewport was the user's own orthophoto, a layer on the current map."""

    async def search_hybrid(self, conn, query, embedding=None, limit=None, type=None):
        return []

    async def get_pages_in_bbox(self, conn, bbox, limit=50, type=None):
        return [
            _page(
                "raster-lrrlzvhe5zj1",
                "Raster: Cyampirita_Orthophoto",
                "Raster layer: Cyampirita_Orthophoto. Bounds: [30.4175, -1.7009, 30.4315, -1.6929].",
            )
        ]

    async def list_pages(self, conn, limit=100, offset=0, type=None, tag=None):
        return []


@pytest.mark.asyncio
async def test_brain_context_keeps_the_raster_page_of_a_visible_layer():
    packet = await build_brain_context_packet(
        FakeConn(),
        OrthophotoBrain(),
        query_text="How is cassava doing this season around this photo?",
        viewport_bounds=[30.41, -1.71, 30.44, -1.69],
        visible_layer_ids=["LrrLzvhE5zj1", "LQmvuX9mQavb"],
    )

    assert packet is not None
    assert "source=spatial; slug=raster-lrrlzvhe5zj1" in packet


@pytest.mark.asyncio
async def test_brain_context_drops_the_raster_page_of_a_layer_not_on_the_map():
    packet = await build_brain_context_packet(
        FakeConn(),
        OrthophotoBrain(),
        query_text="How is cassava doing this season around this photo?",
        viewport_bounds=[30.41, -1.71, 30.44, -1.69],
        visible_layer_ids=["LQmvuX9mQavb"],
    )

    assert packet is None
