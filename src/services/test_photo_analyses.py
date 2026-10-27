import asyncio
import json

import numpy as np

from src.services import photo_analyses
from src.services.photo_inspections import PhotoFacts
from src.services.test_farm_records import _FakeS3

SHA = "a" * 64


def _photo(sha=SHA):
    return PhotoFacts(sha256=sha, filename="DJI_0001.JPG", width=8, height=6, taken_at=None, lat=None, lon=None,
                      altitude_m=None, relative_altitude_m=None, gimbal_pitch=-30.0, gimbal_yaw=None, flight_yaw=None,
                      make="DJI", model="M3E")


def _job(calls, method="sam3-tree-v1"):
    def analyse(rgb):
        calls.append(rgb.shape)
        return {"trees": [[0, 0, 4, 4]]}
    return photo_analyses.PhotoAnalysis(name="surroundings", method=method, store_prefix="towers/surroundings/v1",
                                        select=lambda photos: list(photos), analyse=analyse, enabled=lambda: True)


def _fake_reader(monkeypatch):
    class _Picture:
        pixels = np.zeros((6, 8, 3), np.uint8)

    class _Reader:
        def __init__(self, **kw):
            pass

        async def read(self):
            return _Picture()

    monkeypatch.setattr(photo_analyses, "InspectionPhoto", _Reader)
    monkeypatch.setattr(photo_analyses.photo_inspections, "view_of", lambda photo: "oblique")


def test_a_photo_analysed_in_one_inspection_is_reused_by_another_without_a_second_call(monkeypatch):
    _fake_reader(monkeypatch)
    s3, calls = _FakeS3(), []
    job = _job(calls)
    asyncio.run(photo_analyses._analyse_photo(job, s3, "b", "project-A", "layer-1", {}, _photo()))
    assert len(calls) == 1
    # the same photo uploaded by someone else, in another project and inspection
    found = asyncio.run(photo_analyses.load(job, s3, "b", "project-B", "layer-9", [_photo()]))
    assert found[SHA]["trees"] == [[0, 0, 4, 4]]


def test_results_kept_under_the_inspection_before_are_still_read():
    s3 = _FakeS3()
    job = _job([])
    s3.objects[f"towers/surroundings/v1/project-A/layer-1/{SHA}.json"] = json.dumps(
        {"method": job.method, "trees": []}).encode()
    found = asyncio.run(photo_analyses.load(job, s3, "b", "project-A", "layer-1", [_photo()]))
    assert found[SHA]["trees"] == []


def test_a_result_of_another_method_is_not_reused(monkeypatch):
    _fake_reader(monkeypatch)
    s3 = _FakeS3()
    asyncio.run(photo_analyses._analyse_photo(_job([], method="old"), s3, "b", "p", "l", {}, _photo()))
    assert asyncio.run(photo_analyses.load(_job([], method="new"), s3, "b", "p", "l", [_photo()])) == {}
