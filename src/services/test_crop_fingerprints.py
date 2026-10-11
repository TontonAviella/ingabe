"""Crop calls from plot fingerprints and checked plots (the model replaced by fixed vectors: no download)."""

from __future__ import annotations

import asyncio
import hashlib

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from src.services import crop_fingerprints as cf
from src.services import drone_vision
from src.services.test_farm_records import _FakeS3

X0, Y0, PIXEL_M = 830000, 9810000, 0.05


def _vec(*values: float) -> np.ndarray:
    v = np.array(values + (0.0,) * (8 - len(values)), dtype=np.float32)
    return v / np.linalg.norm(v)


def _field() -> tuple[dict[int, np.ndarray], dict[int, str]]:
    """Four maize plots, three banana, two cassava and one unchecked plot of each look, as fingerprints."""
    rng = np.random.default_rng(0)
    looks = {"maize": _vec(1, 0, 0), "banana": _vec(0, 1, 0), "cassava": _vec(0, 0, 1)}
    prints, checks, n = {}, {}, 0
    for crop, k in (("maize", 4), ("banana", 3), ("cassava", 2)):
        for i in range(k + 1):
            n += 1
            v = looks[crop] + rng.normal(0, 0.02, 8).astype(np.float32)
            prints[n] = v / np.linalg.norm(v)
            if i < k:
                checks[n] = crop
    return prints, checks


def test_a_plot_takes_the_crop_of_its_most_similar_checked_plots():
    prints, checks = _field()
    calls = cf.calls(prints, checks)
    unchecked = {n: calls[n] for n in prints if n not in checks}
    assert sorted(c.crop for c in unchecked.values()) == ["banana", "maize", "unsure"]
    assert all(c.share == 1.0 for c in unchecked.values() if c.crop != "unsure")


def test_a_crop_with_too_few_checks_is_never_named():
    prints, checks = _field()
    cassava_unchecked = max(n for n in prints if n not in checks)  # the last plot made is the unchecked cassava
    call = cf.calls(prints, checks)[cassava_unchecked]
    assert (call.crop, call.candidates[0]) == ("unsure", "cassava")  # two cassava checks, three are needed


def test_a_checked_plot_is_called_with_its_own_check_hidden():
    prints, checks = _field()
    n = next(iter(checks))
    call = cf.calls(prints, checks)[n]
    assert n not in [neighbour for neighbour, _, _ in call.neighbours]
    assert call.crop == "maize"  # from the other three maize plots


def test_no_checks_no_calls():
    prints, _ = _field()
    assert cf.calls(prints, {}) == {}


def _survey(numbers) -> drone_vision.Survey:
    look = drone_vision.PlotLook(number=0, main_crop="cassava", other_crops=["maize", "beans"], stage="young",
                                 crop_cover_percent=40, weeds="few", problems=["gaps"], note="", confidence="medium")
    return drone_vision.Survey(looks={n: drone_vision.PlotLook(**{**look.__dict__, "number": n}) for n in numbers},
                               plots=len(numbers), model="claude-haiku-5-5", done_at="2026-10-10T00:00:00+00:00",
                               cost_usd=0.0)


def test_calls_replace_only_the_crop_of_the_looks():
    prints, checks = _field()
    survey = _survey(prints)
    looked = cf.looked(survey, prints, checks)
    look = looked.look(1)
    assert (look.main_crop, look.source, look.stage, look.weeds, look.problems) == (
        "maize", "fingerprints", "young", "few", ["gaps"])
    assert look.other_crops == ["beans"]  # the named crop is not also listed as another crop
    assert cf.looked(survey, None, checks) is survey  # no fingerprints yet: the looks as they were
    small = cf.looked(_survey(list(prints) + [99]), prints, checks).look(99)  # plot 99: too small, no fingerprint
    assert (small.main_crop, small.source) == ("unsure", "fingerprints")  # not left to the vision looks


def test_squares_lie_inside_the_plot_and_are_picked_the_same_way_each_time(tmp_path):
    path = tmp_path / "photo.tif"
    rng = np.random.default_rng(1)
    with rasterio.open(path, "w", driver="GTiff", width=600, height=600, count=3, dtype="uint8", crs="EPSG:32735",
                       transform=from_origin(X0, Y0, PIXEL_M, PIXEL_M)) as ds:
        ds.write(rng.integers(0, 255, (3, 600, 600), dtype="uint8"))
    big = box(X0 + 1, Y0 - 29, X0 + 29, Y0 - 1)  # 28 m: 36 whole squares, 8 picked
    small = box(X0 + 1, Y0 - 4, X0 + 4, Y0 - 1)  # 3 m: no whole square
    with rasterio.open(path) as ds:
        first = cf.plot_squares(ds, big, 7, -1.5)
        again = cf.plot_squares(ds, big, 7, -1.5)
        assert cf.plot_squares(ds, small, 8, -1.5) == []
    assert len(first) == cf.MAX_SQUARES
    assert first[0].shape == (cf.SQUARE_PX, cf.SQUARE_PX, 3) and first[0].dtype == np.uint8
    assert all(np.array_equal(a, b) for a, b in zip(first, again))


def test_a_plot_fingerprint_is_the_mean_of_its_squares_with_length_one():
    import torch

    class Fake:
        def __call__(self, batch: torch.Tensor) -> torch.Tensor:
            assert tuple(batch.shape[1:]) == (3, 224, 224)
            return torch.ones(batch.shape[0], 768) * batch.mean()

    squares = [np.full((cf.SQUARE_PX, cf.SQUARE_PX, 3), 120, np.uint8) for _ in range(3)]
    v = cf.fingerprint(squares, Fake())
    assert v.shape == (768,) and abs(float(np.linalg.norm(v)) - 1) < 1e-5


def test_kept_fingerprints_round_trip():
    prints, _ = _field()
    s3 = _FakeS3()
    key = "crop_fingerprints/v1|cog/x.tif|found|t|abc"
    asyncio.run(s3.put_object(Bucket="b", Key=cf._object_key(key), Body=cf._to_bytes(prints), ContentType="x"))
    cf._kept.clear()
    back = asyncio.run(cf.load(s3, "b", key))
    assert sorted(back) == sorted(prints) and all(np.allclose(back[n], prints[n]) for n in prints)
    assert asyncio.run(cf.load(s3, "b", key + "x")) is None


def test_the_model_file_is_used_only_when_its_sha256_matches(tmp_path, monkeypatch):
    good = b"the model bytes"

    class Store:
        def __init__(self, data):
            self.data = data

        def download_file(self, bucket, key, path):
            with open(path, "wb") as f:
                f.write(self.data)

    monkeypatch.setattr(cf, "_MODEL_PATH", str(tmp_path / "models" / "fp.pt"))
    monkeypatch.setattr(cf, "MODEL_SHA256", hashlib.sha256(good).hexdigest())
    with pytest.raises(RuntimeError):
        cf.ensure_model(Store(b"something else"), "b")
    assert cf.ensure_model(Store(good), "b") == str(tmp_path / "models" / "fp.pt")
    assert cf.ensure_model(Store(b"never fetched again"), "b")  # a good file on disk is kept


def test_picture_examples_are_kept_per_project_without_unsure_answers():
    s3 = _FakeS3()
    asyncio.run(cf.save_examples(s3, "b", "proj-a", "cog/x.tif|found|t", {1: "maize", 2: "unsure", 3: "banana"},
                                 "Roger, drone pictures 2026-10-10"))
    assert asyncio.run(cf.load_examples(s3, "b", "proj-a", "cog/x.tif|found|t")) == {1: "maize", 3: "banana"}
    assert asyncio.run(cf.load_examples(s3, "b", "proj-b", "cog/x.tif|found|t")) == {}
    assert asyncio.run(cf.load_examples(s3, "b", None, "cog/x.tif|found|t")) == {}


def test_a_long_run_resumes_checkpoints_and_renews_its_photo_link(tmp_path, monkeypatch):
    import torch
    from pyproj import Transformer
    from shapely.geometry import mapping
    from shapely.ops import transform as reproject

    path = tmp_path / "photo.tif"
    with rasterio.open(path, "w", driver="GTiff", width=1000, height=400, count=3, dtype="uint8", crs="EPSG:32735",
                       transform=from_origin(X0, Y0, PIXEL_M, PIXEL_M)) as ds:
        ds.write(np.random.default_rng(2).integers(0, 255, (3, 400, 1000), dtype="uint8"))
    to_wgs84 = Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True).transform
    features = [{"type": "Feature", "properties": {"number": n},
                 "geometry": mapping(reproject(to_wgs84, box(X0 + 1 + 16 * i, Y0 - 15, X0 + 15 + 16 * i, Y0 - 1)))}
                for i, n in enumerate((1, 2, 3))]

    class Plots:
        geojson = {"type": "FeatureCollection", "features": features}

    class Fake:
        def __call__(self, batch):
            return torch.ones(batch.shape[0], 768)

    links, saved = [], []
    monkeypatch.setattr(cf, "_load_model", lambda p: Fake())
    monkeypatch.setattr(cf, "REOPEN_S", 0)  # reopen before every plot
    monkeypatch.setattr(cf, "CHECKPOINT_PLOTS", 2)
    done = {1: np.zeros(768, np.float32)}  # plot 1 came from an earlier run's checkpoint

    def link():
        links.append(1)
        return str(path)

    prints = cf.fingerprint_plots(link, Plots(), "unused", lambda d, n: None, done, lambda p: saved.append(sorted(p)))
    assert sorted(prints) == [1, 2, 3] and not prints[1].any()  # plot 1 kept, not recomputed
    assert saved == [[1, 2]] and len(links) == 3  # one checkpoint at plot 2; opened once, reopened for plots 2 and 3
