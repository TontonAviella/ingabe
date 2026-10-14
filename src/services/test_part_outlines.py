"""Outlines of parts on inspection photos (part_outlines), with FastSAM replaced by a fake that returns a chosen
mask: a box outlined in its padded crop and put back in the photo's pixels, a tap traced, a trace that is the
background refused, no weights refused plainly."""

from __future__ import annotations

import numpy as np
import pytest

from src.services import fastsam_model, part_outlines


class _Tensor:
    def __init__(self, a):
        self.a = a

    def cpu(self):
        return self

    def numpy(self):
        return self.a


class _Masks:
    def __init__(self, mask):
        self.data = [_Tensor(mask)]


class _Result:
    def __init__(self, mask):
        self.masks = _Masks(mask)


@pytest.fixture
def fake(monkeypatch):
    """FastSAM's stand-in: a box prompt gets the box less a 2 px margin; a point prompt a square of `half` px."""
    seen = {"calls": [], "half": 30}

    def model(crop, **kw):
        seen["calls"].append((crop.shape[:2], kw))
        mask = np.zeros(crop.shape[:2], np.float32)
        if "bboxes" in kw:
            x0, y0, x1, y1 = (int(v) for v in kw["bboxes"][0])
            mask[y0 + 2:y1 - 2, x0 + 2:x1 - 2] = 1
        else:
            (x, y), = kw["points"]
            h = seen["half"]
            mask[max(0, int(y) - h):int(y) + h, max(0, int(x) - h):int(x) + h] = 1
        return [_Result(mask)]

    monkeypatch.setattr(fastsam_model, "weights_status", lambda: {"available": True, "path": "fake.pt"})
    monkeypatch.setattr(fastsam_model, "load", lambda path: model)
    return seen


def test_a_box_is_outlined_in_its_padded_crop_and_put_back_in_the_photos_pixels(fake):
    (outline,) = part_outlines.outline_boxes(np.zeros((1000, 2000, 3), np.uint8), [(500.0, 300.0, 600.0, 500.0)])
    xs, ys = [p[0] for p in outline[0]], [p[1] for p in outline[0]]
    assert (min(xs), min(ys), max(xs), max(ys)) == (502, 302, 597, 497)
    ((shape, kw),) = fake["calls"]
    assert kw["imgsz"] == part_outlines.IMGSZ
    assert shape == (int(500 + 200 * 0.15 + 20) - int(300 - 200 * 0.15 - 20),
                     int(600 + 100 * 0.15 + 20) - int(500 - 100 * 0.15 - 20))


def test_a_tap_is_traced_and_the_background_around_a_missed_tap_is_refused(fake):
    rgb = np.zeros((1200, 1600, 3), np.uint8)
    traced = part_outlines.trace(rgb, 800.0, 600.0)
    assert traced is not None and traced.box == (770.0, 570.0, 829.0, 629.0)
    fake["half"] = 400  # most of the 600 px window it is traced in
    assert part_outlines.trace(rgb, 800.0, 600.0) is None


def test_no_weights_is_a_plain_refusal(monkeypatch):
    monkeypatch.setattr(fastsam_model, "weights_status", lambda: {"available": False, "reason": "no FastSAM-s.pt"})
    with pytest.raises(part_outlines.OutlinerUnavailable, match="no FastSAM-s.pt"):
        part_outlines.outline_boxes(np.zeros((10, 10, 3), np.uint8), [(0.0, 0.0, 5.0, 5.0)])
