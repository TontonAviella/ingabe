"""Sage tool shortlist: BM25 ranking, rank fusion, hybrid with fallback."""
from __future__ import annotations

import asyncio

import pytest

from src.services import sage_tool_shortlist as sl


def _tool(name: str, description: str, *params: str) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {p: {"type": "string"} for p in params}},
    }}


TOOLS = [
    _tool("get_forecast", "Get weather forecast for any location in Rwanda.", "district"),
    _tool("get_weather_stats", "Read daily weather statistics: observed precipitation and temperature.", "district"),
    _tool("native_buffer", "Buffers vector layers to a specified distance.", "INPUT", "DISTANCE"),
    _tool("set_layer_style", "Creates a new style for a layer and applies it.", "layer_id"),
    _tool("search_brain", "Search the knowledge brain for entities and information.", "query"),
    _tool("get_drought_status", "Read drought status per district.", "district"),
]


def test_tokenize_splits_snake_case_stems_and_strips_accents() -> None:
    assert sl.tokenize("get_cell_ndvi_stats") == ["cell", "ndvi", "stat"]
    assert sl.tokenize("Prévision météo") == ["prevision", "meteo"]
    assert sl.tokenize("show me the houses") == ["house"]


@pytest.mark.parametrize(
    ("text", "best"),
    [
        ("will it rain in Musanze tomorrow?", "get_forecast"),
        ("how much rain fell last month?", "get_weather_stats"),
        ("draw a zone around the schools layer", "native_buffer"),
        ("colour the farms layer red", "set_layer_style"),
        ("is Bugesera in drought?", "get_drought_status"),
        ("Y a-t-il une sécheresse à Kirehe ?", "get_drought_status"),
    ],
)
def test_rank_tools_puts_the_obvious_tool_first(text: str, best: str) -> None:
    assert sl.rank_tools(text, [], TOOLS)[0][0] == best


def test_recent_tool_stays_available_for_follow_ups() -> None:
    history = [
        {"role": "user", "content": "what do we know about Kabeza?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "search_brain", "arguments": "{}"}}]},
    ]
    names = [n for n, _ in sl.rank_tools("and the other one?", history, TOOLS)]
    assert names[0] == "search_brain"


def test_shortlist_tools_keeps_k_in_catalog_order() -> None:
    kept = sl.shortlist_tools("rain forecast for Huye", [], TOOLS, k=2)
    assert [t["function"]["name"] for t in kept] == [
        n for n in (t["function"]["name"] for t in TOOLS) if n in {"get_forecast", "get_weather_stats"}
    ]


def test_fuse_rankings_rewards_agreement() -> None:
    fused = sl.fuse_rankings(["a", "b", "c"], ["b", "a", "d"])
    assert fused[:2] in (["a", "b"], ["b", "a"])
    assert set(fused) == {"a", "b", "c", "d"}


def _fake_embedder(calls: list[list[str]]):
    """Embeds by keyword: dimension 0 = 'forecast/weather', 1 = 'drought'."""
    async def embed(texts: list[str]):
        calls.append(list(texts))
        vecs = []
        for t in texts:
            low = t.lower()
            vecs.append([1.0 if ("forecast" in low or "weather" in low or "storm" in low) else 0.0,
                         1.0 if ("drought" in low or "dry" in low) else 0.0, 0.1])
        return vecs, "fake-embed"
    return embed


@pytest.mark.asyncio
async def test_hybrid_uses_embeddings_and_caches_tool_vectors() -> None:
    calls: list[list[str]] = []
    cache = sl.ToolEmbeddingCache()
    embed = _fake_embedder(calls)
    await cache.vectors(TOOLS, embed)
    # "storms" is not in any description: BM25 alone has no signal, embeddings do.
    first = await sl.hybrid_shortlist("any storms coming?", [], TOOLS, k=1, embed=embed, cache=cache)
    assert first.method == "hybrid"
    assert [t["function"]["name"] for t in first.tools] == ["get_forecast"]
    await sl.hybrid_shortlist("dry spell ahead?", [], TOOLS, k=1, embed=embed, cache=cache)
    tool_batches = [c for c in calls if len(c) > 1]
    assert len(tool_batches) == 1  # tool documents embedded once, then cached


@pytest.mark.asyncio
async def test_a_cold_cache_never_blocks_the_turn() -> None:
    release = asyncio.Event()
    calls: list[list[str]] = []
    fast = _fake_embedder(calls)

    async def slow_embed(texts: list[str]):
        await release.wait()  # a cold Ollama loading the model
        return await fast(texts)

    cache = sl.ToolEmbeddingCache()
    turn = await asyncio.wait_for(
        sl.hybrid_shortlist("is Bugesera in drought?", [], TOOLS, k=2, embed=slow_embed, cache=cache), 1.0)
    assert turn.method == "bm25_warming"
    assert "get_drought_status" in [t["function"]["name"] for t in turn.tools]
    release.set()
    await cache._warming
    assert cache.ready(TOOLS)
    warm = await sl.hybrid_shortlist("any storms coming?", [], TOOLS, k=1, embed=fast, cache=cache)
    assert warm.method == "hybrid"


@pytest.mark.asyncio
async def test_a_slow_query_embedding_falls_back_to_bm25(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    fast = _fake_embedder(calls)
    cache = sl.ToolEmbeddingCache()
    await cache.vectors(TOOLS, fast)

    async def stalled(texts: list[str]):
        await asyncio.sleep(5)
        return await fast(texts)

    monkeypatch.setattr(sl, "QUERY_EMBED_TIMEOUT_S", 0.05)
    turn = await sl.hybrid_shortlist("is Bugesera in drought?", [], TOOLS, k=2, embed=stalled, cache=cache)
    assert turn.method == "bm25_timeout"
    assert "get_drought_status" in [t["function"]["name"] for t in turn.tools]


@pytest.mark.asyncio
async def test_hybrid_falls_back_to_bm25_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(texts: list[str]):
        raise RuntimeError("ollama down")

    # Patch the module logger: src.* loggers stop propagating to caplog
    # once the app's logging config is loaded (CODING_STANDARDS: caplog).
    warnings: list[str] = []
    monkeypatch.setattr(sl.logger, "warning",
                        lambda msg, *args, **kwargs: warnings.append(msg % args if args else msg))
    cache = sl.ToolEmbeddingCache()
    await cache.vectors(TOOLS, _fake_embedder([]))
    result = await sl.hybrid_shortlist("is Bugesera in drought?", [], TOOLS, k=2,
                                       embed=broken, cache=cache)
    assert result.method == "bm25"
    assert "get_drought_status" in [t["function"]["name"] for t in result.tools]
    assert any("embeddings unavailable" in w for w in warnings)
