import numpy as np

from src.services import line_conductors


def test_outlines_come_back_in_the_photos_own_pixels_and_slivers_are_dropped():
    assert line_conductors.outlines([[(0.0, 0.0), (10.0, 0.0), (10.0, 1.5)], [(1.0, 1.0), (2.0, 2.0)]], 0.5) == [
        [[0, 0], [20, 0], [20, 3]]]


def test_a_photo_is_sent_shrunk_to_the_measured_size(monkeypatch):
    seen = {}

    def sam3(rgb, prompt, confidence):
        seen.update(shape=rgb.shape[:2], prompt=prompt, confidence=confidence)
        return [[(0.0, 0.0), (1536.0, 0.0), (1536.0, 4.0)]]

    monkeypatch.setattr(line_conductors.hosted_detector, "sam3_polygons", sam3)
    result = line_conductors.analyse(np.zeros((2160, 3840, 3), np.uint8))
    assert seen == {"shape": (864, 1536), "prompt": "power line", "confidence": 0.3}
    assert result == {"outlines": [[[0, 0], [3840, 0], [3840, 10]]], "width": 3840, "height": 2160}
