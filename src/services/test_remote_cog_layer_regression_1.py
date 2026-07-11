"""Regression coverage for persistent Sage remote COG layers."""

import json
from contextlib import asynccontextmanager

import pytest

from src.services import remote_cog_layer as remote_cog

from src.services.remote_cog_layer import (
    build_remote_cog_maplibre_layer,
    build_remote_cog_tile_url,
    infer_remote_cog_metadata,
    validate_remote_cog_bounds,
)
from src.services.remote_cog_url import RemoteCogUrlError


@pytest.fixture
def anyio_backend():
    return "asyncio"


# Regression: ISSUE-003 - Sage satellite imagery disappeared after page reload.
# Found by /qa on 2026-07-09
# Report: .gstack/qa-reports/qa-report-localhost-2026-07-09.md
def test_remote_cog_style_is_reloadable_and_scoped_to_its_bounds() -> None:
    metadata = {
        "remote_cog_url": "https://example.com/scene.tif?token=a&part=1",
        "expression": "visual",
        "maxzoom": 14,
        "opacity": 0.9,
    }

    tile_url = build_remote_cog_tile_url(metadata)
    source_id, source, layer = build_remote_cog_maplibre_layer(
        "Lscene", metadata, [29.7, -2.45, 29.95, -2.2]
    )

    assert (
        "url=https%3A%2F%2Fexample.com%2Fscene.tif%3Ftoken%3Da%26part%3D1" in tile_url
    )
    assert source_id == "remote-cog-source-Lscene"
    assert source["tiles"] == [tile_url]
    assert source["bounds"] == [29.7, -2.45, 29.95, -2.2]
    assert source["maxzoom"] == 14
    assert layer == {
        "id": "raster-layer-Lscene",
        "type": "raster",
        "source": "remote-cog-source-Lscene",
        "paint": {"raster-opacity": 0.9},
    }


def test_earth_search_cog_url_retains_scene_provenance() -> None:
    metadata = infer_remote_cog_metadata(
        "https://sentinel-cogs.s3.us-west-2.amazonaws.com/"
        "sentinel-s2-l2a-cogs/35/M/RT/2026/7/"
        "S2B_35MRT_20260705_0_L2A/TCI.tif"
    )

    assert metadata == {
        "source_catalog": "earth_search",
        "collection": "sentinel-2-l2a",
        "scene_id": "S2B_35MRT_20260705_0_L2A",
        "scene_date": "2026-07-05",
        "platform": "sentinel-2b",
    }


@pytest.mark.parametrize(
    "bounds",
    [
        [30, -2, 29, -1],
        [29, -1, 30, -2],
        [float("nan"), -2, 30, -1],
        [-181, -2, 30, -1],
        [29, -91, 30, -1],
    ],
)
def test_remote_cog_bounds_reject_invalid_wgs84_extents(bounds) -> None:
    with pytest.raises(ValueError):
        validate_remote_cog_bounds(bounds)


def test_remote_cog_bounds_normalize_valid_values() -> None:
    assert validate_remote_cog_bounds(["29.7", -2.45, 29.95, -2.2]) == [
        29.7,
        -2.45,
        29.95,
        -2.2,
    ]


class _FakeTransaction:
    def __init__(self, conn: "_FakeConn") -> None:
        self.conn = conn

    async def __aenter__(self):
        self.conn.in_transaction = True
        self.conn.transaction_entries += 1
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        self.conn.in_transaction = False
        self.conn.transaction_exits.append(exc_type)


class _FakeConn:
    def __init__(self, *, authorized: bool = True) -> None:
        self.authorized = authorized
        self.in_transaction = False
        self.transaction_entries = 0
        self.transaction_exits: list[type[BaseException] | None] = []
        self.current_layers: list[str] = []
        self.layer: dict | None = None
        self.style_id: str | None = None
        self.executions: list[tuple[str, tuple]] = []

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    async def fetchrow(self, query: str, *args):
        assert self.in_transaction
        if "SELECT m.layers" in query:
            return {"layers": list(self.current_layers)} if self.authorized else None
        if "SELECT ml.layer_id" in query:
            if self.layer is None:
                return None
            metadata = self.layer["metadata"]
            display_key = (
                self.layer["map_id"],
                self.layer["remote_url"],
                metadata["expression"],
                metadata["style_hint"],
                metadata["colormap"],
                metadata["rescale"],
                "" if metadata["band_index"] is None else str(metadata["band_index"]),
            )
            if display_key == args:
                return {
                    "layer_id": self.layer["layer_id"],
                    "style_id": self.style_id,
                }
            return None
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def execute(self, query: str, *args):
        assert self.in_transaction
        normalized_query = " ".join(query.split())
        self.executions.append((normalized_query, args))
        if normalized_query.startswith("INSERT INTO map_layers"):
            self.layer = {
                "layer_id": args[0],
                "name": args[2],
                "metadata": json.loads(args[3]),
                "bounds": args[4],
                "map_id": args[5],
                "remote_url": args[6],
            }
        elif normalized_query.startswith("INSERT INTO layer_styles"):
            self.style_id = args[0]
        elif normalized_query.startswith("UPDATE map_layers"):
            assert self.layer is not None
            self.layer.update(
                name=args[1],
                metadata=json.loads(args[2]),
                bounds=args[3],
            )
        elif normalized_query.startswith("UPDATE user_mundiai_maps"):
            self.current_layers = list(args[0])


def _fake_async_conn(conn: _FakeConn):
    @asynccontextmanager
    async def context(*_args, **_kwargs):
        yield conn

    return context


@pytest.mark.anyio
async def test_persistence_reuses_equivalent_layer_and_inferred_provenance_wins(
    monkeypatch,
) -> None:
    conn = _FakeConn()
    generated_ids = iter(("Lexisting", "Sexisting"))
    monkeypatch.setattr(remote_cog, "async_conn", _fake_async_conn(conn))
    monkeypatch.setattr(
        remote_cog,
        "generate_id",
        lambda prefix: next(generated_ids),
    )
    remote_url = (
        "https://sentinel-cogs.s3.us-west-2.amazonaws.com/"
        "sentinel-s2-l2a-cogs/35/M/RT/2026/7/"
        "S2B_35MRT_20260705_0_L2A/TCI.tif"
    )
    kwargs = {
        "map_id": "Mmap",
        "user_uuid": "00000000-0000-0000-0000-000000000001",
        "layer_name": "Sentinel scene",
        "remote_url": remote_url,
        "bounds": [29.7, -2.45, 29.95, -2.2],
        "expression": "visual",
        "style_hint": "visual",
        "extra_metadata": {
            "source_catalog": "llm_catalog",
            "collection": "wrong-collection",
            "scene_id": "wrong-scene",
            "scene_date": "1999-01-01",
            "platform": "wrong-platform",
            "cloud_cover": 4.5,
        },
    }

    first = await remote_cog.persist_remote_cog_layer(**kwargs)
    second = await remote_cog.persist_remote_cog_layer(**kwargs)

    assert first == second
    assert first.layer_id == "Lexisting"
    assert conn.current_layers == ["Lexisting"]
    assert conn.layer is not None
    assert conn.layer["metadata"]["source_catalog"] == "earth_search"
    assert conn.layer["metadata"]["collection"] == "sentinel-2-l2a"
    assert conn.layer["metadata"]["scene_id"] == "S2B_35MRT_20260705_0_L2A"
    assert conn.layer["metadata"]["scene_date"] == "2026-07-05"
    assert conn.layer["metadata"]["platform"] == "sentinel-2b"
    assert conn.layer["metadata"]["cloud_cover"] == 4.5
    assert sum(q.startswith("INSERT INTO map_layers") for q, _ in conn.executions) == 1
    assert (
        sum(q.startswith("INSERT INTO layer_styles") for q, _ in conn.executions) == 1
    )
    assert (
        sum(q.startswith("INSERT INTO map_layer_styles") for q, _ in conn.executions)
        == 1
    )
    assert sum(q.startswith("UPDATE map_layers") for q, _ in conn.executions) == 1


@pytest.mark.anyio
async def test_persistence_rejects_unauthorized_map_inside_transaction(
    monkeypatch,
) -> None:
    conn = _FakeConn(authorized=False)
    monkeypatch.setattr(remote_cog, "async_conn", _fake_async_conn(conn))

    with pytest.raises(PermissionError, match="not editable"):
        await remote_cog.persist_remote_cog_layer(
            map_id="Mforbidden",
            user_uuid="00000000-0000-0000-0000-000000000001",
            layer_name="Forbidden",
            remote_url=(
                "https://isdasoil.s3.amazonaws.com/soil_data/"
                "nitrogen_total/nitrogen_total.tif"
            ),
            bounds=[29.7, -2.45, 29.95, -2.2],
            expression="single_band",
            style_hint="soil_nitrogen",
        )

    assert conn.transaction_entries == 1
    assert conn.transaction_exits == [PermissionError]
    assert conn.executions == []


@pytest.mark.anyio
async def test_persistence_rejects_untrusted_url_before_database_access(
    monkeypatch,
) -> None:
    database_called = False

    def fail_async_conn(*_args, **_kwargs):
        nonlocal database_called
        database_called = True
        raise AssertionError("untrusted URLs must be rejected before database access")

    monkeypatch.setattr(remote_cog, "async_conn", fail_async_conn)

    with pytest.raises(RemoteCogUrlError, match="not trusted"):
        await remote_cog.persist_remote_cog_layer(
            map_id="Mmap",
            user_uuid="00000000-0000-0000-0000-000000000001",
            layer_name="Untrusted",
            remote_url="https://attacker.example/scene.tif",
            bounds=[29.7, -2.45, 29.95, -2.2],
            expression="visual",
            style_hint="visual",
        )

    assert database_called is False


@pytest.mark.anyio
async def test_persistence_accepts_authenticated_local_layer_reference(monkeypatch) -> None:
    conn = _FakeConn()
    generated_ids = iter(("Ldisplay", "Sdisplay"))
    monkeypatch.setattr(remote_cog, "async_conn", _fake_async_conn(conn))
    monkeypatch.setattr(
        remote_cog,
        "generate_id",
        lambda prefix: next(generated_ids),
    )

    persisted = await remote_cog.persist_remote_cog_layer(
        map_id="Mmap",
        user_uuid="00000000-0000-0000-0000-000000000001",
        layer_name="Local orthophoto",
        remote_url="mundi-layer:Lsource",
        bounds=[29.7, -2.45, 29.95, -2.2],
        expression="visual",
        style_hint="visual",
    )

    assert persisted.layer_id == "Ldisplay"
    assert conn.layer is not None
    assert conn.layer["remote_url"] == "mundi-layer:Lsource"
    assert "mundi-layer%3ALsource" in persisted.tile_url
