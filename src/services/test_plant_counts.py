"""Green peaks on a made-up picture: round green crowns of known number on bare soil, and a patch of solid green."""

from __future__ import annotations

import numpy as np

from src.services import plant_counts

PIXEL_M = 0.03


def _picture(rows=6, cols=7, spacing_m=0.9, radius_m=0.22):
    """An 8 m square of soil with rows x cols round green crowns, as (rows, cols, 3) of 0..255."""
    size = int(8 / PIXEL_M)
    rgb = np.empty((size, size, 3), dtype="uint8")
    rgb[...] = (196, 158, 118)  # soil
    yy, xx = np.mgrid[0:size, 0:size] * PIXEL_M
    for i in range(rows):
        for j in range(cols):
            disc = (yy - (1.0 + i * spacing_m)) ** 2 + (xx - (1.0 + j * spacing_m)) ** 2 <= radius_m ** 2
            rgb[disc] = (70, 150, 55)
    return rgb


def test_every_crown_is_one_peak():
    rgb = _picture()
    peaks, solid = plant_counts.crowns(rgb, np.ones(rgb.shape[:2], bool), PIXEL_M)
    assert len(peaks) == 42 and not solid.any()  # crowns that stand apart: no solid patch
    # only where asked: the left half holds 4 of the 7 columns
    left = np.zeros(rgb.shape[:2], bool)
    left[:, : int(4.15 / PIXEL_M)] = True
    assert len(plant_counts.crowns(rgb, left, PIXEL_M)[0]) == 24


def test_a_patch_of_solid_green_is_one_peak_not_one_per_pixel():
    """Field 24 on Cyampirita, 2026-11-14: 697 m2 with twelve solid patches was given 4,987 plants, because every pixel
    on a patch's flat top counted. A flat top is one peak, and the patch is given as solid."""
    rgb = _picture()
    rows, cols = slice(int(3.3 / PIXEL_M), int(5.7 / PIXEL_M)), slice(int(4.3 / PIXEL_M), int(6.7 / PIXEL_M))
    rgb[rows, cols] = (70, 150, 55)  # a 2.4 m x 2.4 m bush over nine of the crowns
    peaks, solid = plant_counts.crowns(rgb, np.ones(rgb.shape[:2], bool), PIXEL_M)
    assert len(peaks) == 42 - 9 + 1
    # no peaks closer than the rule allows: two are never nearer than half of SPACING_M
    gaps = np.sqrt(((peaks[:, None] - peaks[None]) ** 2).sum(-1)) + np.eye(len(peaks)) * 1e9
    assert gaps.min() * PIXEL_M >= plant_counts.SPACING_M / 2
    assert abs(solid.sum() * PIXEL_M ** 2 - 2.4 * 2.4) < 0.6  # the bush, measured as one solid patch
    # a closed canopy is one solid patch and one peak
    rgb[...] = (70, 150, 55)
    peaks, solid = plant_counts.crowns(rgb, np.ones(rgb.shape[:2], bool), PIXEL_M)
    assert len(peaks) <= 1 and solid.mean() > 0.95
