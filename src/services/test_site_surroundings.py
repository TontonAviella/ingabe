"""Site surroundings: the shares of a photo under SAM 3's tree outlines and green outside them."""

from __future__ import annotations

import numpy as np

from src.services import site_surroundings


def test_trees_are_the_outlines_and_other_vegetation_is_green_outside_them():
    rgb = np.zeros((100, 100, 3), np.uint8)
    rgb[:, :50] = (40, 160, 40)  # green on the left half
    rgb[:, 50:] = (120, 120, 120)  # grey on the right half
    tree = [(0.0, 0.0), (25.0, 0.0), (25.0, 100.0), (0.0, 100.0)]  # a quarter of the picture, inside the green
    g = site_surroundings.ground(rgb, [tree])
    assert abs(g.trees - 0.25) < 0.02 and abs(g.other_vegetation - 0.25) < 0.02 and g.trees_found == 1
    assert site_surroundings.ground(rgb, []).trees == 0 and abs(site_surroundings.ground(rgb, []).other_vegetation - 0.5) < 0.01


def test_a_large_photo_is_shrunk_before_it_is_sent(monkeypatch):
    sent = {}

    def polygons(rgb, prompt, confidence):
        sent.update(shape=rgb.shape, prompt=prompt, confidence=confidence)
        return []

    monkeypatch.setattr(site_surroundings.hosted_detector, "sam3_polygons", polygons)
    site_surroundings.analyse(np.zeros((3000, 4000, 3), np.uint8))
    assert sent == {"shape": (768, 1024, 3), "prompt": "tree", "confidence": 0.3}
