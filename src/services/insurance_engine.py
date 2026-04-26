"""Insurance Intelligence Engine — one function, all signals, any audience, any admin level.

Connects 12 existing mundi.ai capabilities into a single unified report:
  CHIRPS rainfall, crop calendars, season detection, dry spells, NDVI concordance,
  binary accuracy, insurance confidence, WaPOR ET, WaPOR soil moisture, NDVI anomaly
  z-scores, bias correction, and admin boundary resolution.

Called by Sage via `get_insurance_intelligence` tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

_VALID_AUDIENCES = {"farmer", "insurance", "agronomist", "scientist"}

# ---------------------------------------------------------------------------
# Growth phases per crop (DAP = days after planting)
# ---------------------------------------------------------------------------

_GROWTH_PHASES: dict[str, dict[str, tuple[int, int]]] = {
    # --- Cereals ---
    "maize": {
        "planting": (0, 20),
        "vegetative": (20, 55),
        "flowering": (55, 75),
        "grain_fill": (75, 105),
        "maturity": (105, 120),
    },
    "beans": {
        "planting": (0, 15),
        "vegetative": (15, 40),
        "flowering": (40, 55),
        "grain_fill": (55, 80),
        "maturity": (80, 90),
    },
    "rice": {
        "planting": (0, 25),
        "vegetative": (25, 70),
        "flowering": (70, 100),
        "grain_fill": (100, 135),
        "maturity": (135, 150),
    },
    "sorghum": {
        "planting": (0, 20),
        "vegetative": (20, 50),
        "flowering": (50, 70),
        "grain_fill": (70, 100),
        "maturity": (100, 110),
    },
    "wheat": {
        "planting": (0, 20),
        "vegetative": (20, 50),
        "flowering": (50, 75),
        "grain_fill": (75, 105),
        "maturity": (105, 120),
    },
    "finger_millet": {
        "planting": (0, 15),
        "vegetative": (15, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 90),
        "maturity": (90, 105),
    },
    # --- Tubers & roots ---
    "potato": {
        "planting": (0, 20),
        "vegetative": (20, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 95),
        "maturity": (95, 110),
    },
    "sweet_potato": {
        "planting": (0, 25),
        "vegetative": (25, 60),
        "flowering": (60, 90),
        "grain_fill": (90, 120),
        "maturity": (120, 150),
    },
    "cassava": {
        "planting": (0, 30),
        "vegetative": (30, 120),
        "flowering": (120, 180),
        "grain_fill": (180, 300),
        "maturity": (300, 365),
    },
    "yam": {
        "planting": (0, 30),
        "vegetative": (30, 90),
        "flowering": (90, 150),
        "grain_fill": (150, 210),
        "maturity": (210, 270),
    },
    "taro": {
        "planting": (0, 25),
        "vegetative": (25, 80),
        "flowering": (80, 140),
        "grain_fill": (140, 200),
        "maturity": (200, 270),
    },
    # --- Legumes ---
    "soybean": {
        "planting": (0, 15),
        "vegetative": (15, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 95),
        "maturity": (95, 110),
    },
    "groundnut": {
        "planting": (0, 15),
        "vegetative": (15, 40),
        "flowering": (40, 65),
        "grain_fill": (65, 100),
        "maturity": (100, 120),
    },
    "peas": {
        "planting": (0, 15),
        "vegetative": (15, 35),
        "flowering": (35, 50),
        "grain_fill": (50, 70),
        "maturity": (70, 85),
    },
    "cowpea": {
        "planting": (0, 12),
        "vegetative": (12, 35),
        "flowering": (35, 50),
        "grain_fill": (50, 70),
        "maturity": (70, 80),
    },
    "pigeon_pea": {
        "planting": (0, 20),
        "vegetative": (20, 60),
        "flowering": (60, 100),
        "grain_fill": (100, 140),
        "maturity": (140, 170),
    },
    # --- Vegetables ---
    "tomato": {
        "planting": (0, 20),
        "vegetative": (20, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 90),
        "maturity": (90, 110),
    },
    "onion": {
        "planting": (0, 20),
        "vegetative": (20, 55),
        "flowering": (55, 80),
        "grain_fill": (80, 110),
        "maturity": (110, 130),
    },
    "cabbage": {
        "planting": (0, 20),
        "vegetative": (20, 50),
        "flowering": (50, 65),
        "grain_fill": (65, 80),
        "maturity": (80, 95),
    },
    "carrot": {
        "planting": (0, 15),
        "vegetative": (15, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 85),
        "maturity": (85, 100),
    },
    "chili": {
        "planting": (0, 25),
        "vegetative": (25, 55),
        "flowering": (55, 80),
        "grain_fill": (80, 110),
        "maturity": (110, 130),
    },
    "eggplant": {
        "planting": (0, 25),
        "vegetative": (25, 55),
        "flowering": (55, 80),
        "grain_fill": (80, 110),
        "maturity": (110, 130),
    },
    "green_pepper": {
        "planting": (0, 25),
        "vegetative": (25, 55),
        "flowering": (55, 75),
        "grain_fill": (75, 100),
        "maturity": (100, 120),
    },
    "garlic": {
        "planting": (0, 20),
        "vegetative": (20, 60),
        "flowering": (60, 90),
        "grain_fill": (90, 120),
        "maturity": (120, 150),
    },
    "amaranth": {
        "planting": (0, 12),
        "vegetative": (12, 35),
        "flowering": (35, 50),
        "grain_fill": (50, 65),
        "maturity": (65, 75),
    },
    "leek": {
        "planting": (0, 20),
        "vegetative": (20, 60),
        "flowering": (60, 90),
        "grain_fill": (90, 120),
        "maturity": (120, 150),
    },
    "lettuce": {
        "planting": (0, 10),
        "vegetative": (10, 30),
        "flowering": (30, 40),
        "grain_fill": (40, 50),
        "maturity": (50, 60),
    },
    "spinach": {
        "planting": (0, 10),
        "vegetative": (10, 25),
        "flowering": (25, 35),
        "grain_fill": (35, 45),
        "maturity": (45, 55),
    },
    "cucumber": {
        "planting": (0, 12),
        "vegetative": (12, 30),
        "flowering": (30, 45),
        "grain_fill": (45, 60),
        "maturity": (60, 70),
    },
    "watermelon": {
        "planting": (0, 15),
        "vegetative": (15, 35),
        "flowering": (35, 55),
        "grain_fill": (55, 75),
        "maturity": (75, 90),
    },
    "pumpkin": {
        "planting": (0, 15),
        "vegetative": (15, 40),
        "flowering": (40, 60),
        "grain_fill": (60, 85),
        "maturity": (85, 110),
    },
    # --- Fruits ---
    "banana": {
        "planting": (0, 60),
        "vegetative": (60, 180),
        "flowering": (180, 240),
        "grain_fill": (240, 330),
        "maturity": (330, 365),
    },
    "avocado": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 420),
        "grain_fill": (420, 600),
        "maturity": (600, 730),
    },
    "mango": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 400),
        "grain_fill": (400, 500),
        "maturity": (500, 545),
    },
    "passion_fruit": {
        "planting": (0, 30),
        "vegetative": (30, 120),
        "flowering": (120, 160),
        "grain_fill": (160, 230),
        "maturity": (230, 270),
    },
    "pineapple": {
        "planting": (0, 30),
        "vegetative": (30, 240),
        "flowering": (240, 300),
        "grain_fill": (300, 450),
        "maturity": (450, 540),
    },
    "papaya": {
        "planting": (0, 30),
        "vegetative": (30, 120),
        "flowering": (120, 180),
        "grain_fill": (180, 270),
        "maturity": (270, 330),
    },
    "citrus": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 400),
        "grain_fill": (400, 540),
        "maturity": (540, 600),
    },
    "strawberry": {
        "planting": (0, 20),
        "vegetative": (20, 50),
        "flowering": (50, 70),
        "grain_fill": (70, 95),
        "maturity": (95, 110),
    },
    "tree_tomato": {
        "planting": (0, 60),
        "vegetative": (60, 180),
        "flowering": (180, 240),
        "grain_fill": (240, 330),
        "maturity": (330, 365),
    },
    "guava": {
        "planting": (0, 60),
        "vegetative": (60, 240),
        "flowering": (240, 300),
        "grain_fill": (300, 420),
        "maturity": (420, 480),
    },
    "cape_gooseberry": {
        "planting": (0, 20),
        "vegetative": (20, 60),
        "flowering": (60, 90),
        "grain_fill": (90, 120),
        "maturity": (120, 150),
    },
    # --- Cash & industrial crops ---
    "coffee": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 400),
        "grain_fill": (400, 580),
        "maturity": (580, 640),
    },
    "tea": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 420),
        "grain_fill": (420, 540),
        "maturity": (540, 730),
    },
    "sugarcane": {
        "planting": (0, 30),
        "vegetative": (30, 120),
        "flowering": (120, 240),
        "grain_fill": (240, 330),
        "maturity": (330, 420),
    },
    "pyrethrum": {
        "planting": (0, 20),
        "vegetative": (20, 90),
        "flowering": (90, 150),
        "grain_fill": (150, 180),
        "maturity": (180, 210),
    },
    "tobacco": {
        "planting": (0, 20),
        "vegetative": (20, 55),
        "flowering": (55, 80),
        "grain_fill": (80, 105),
        "maturity": (105, 120),
    },
    "sunflower": {
        "planting": (0, 15),
        "vegetative": (15, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 90),
        "maturity": (90, 105),
    },
    "macadamia": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 420),
        "grain_fill": (420, 600),
        "maturity": (600, 730),
    },
    "sesame": {
        "planting": (0, 12),
        "vegetative": (12, 35),
        "flowering": (35, 55),
        "grain_fill": (55, 80),
        "maturity": (80, 95),
    },
    # --- Oil crops ---
    "oil_palm": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 420),
        "grain_fill": (420, 600),
        "maturity": (600, 730),
    },
    "soya": {
        "planting": (0, 15),
        "vegetative": (15, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 95),
        "maturity": (95, 110),
    },
}

_RWANDA_CENTER = (-1.94, 29.87)

_ET_LONG_TERM_MEAN = 3.5

# Per-district seasonal rainfall normals (mm).
# Derived from CHIRPS 2000-2023 seasonal totals (Sep-Jan for A, Feb-May for B).
# Districts grouped by agro-ecological zone:
#   Northwest highlands: Musanze, Rubavu, Nyabihu, Burera (wet, >500mm/season)
#   Central plateau: Kigali, Muhanga, Kamonyi, Ruhango, Huye, Nyanza, Gisagara (moderate)
#   Eastern lowland: Bugesera, Kayonza, Kirehe, Ngoma, Gatsibo, Nyagatare (dry, <350mm)
#   Southwest: Nyamasheke, Rusizi, Karongi, Rutsiro (lake-influenced, moderate-wet)
_DISTRICT_RAINFALL_NORMALS: dict[str, dict[str, dict[str, float]]] = {
    # --- Northwest highlands ---
    "musanze":    {"A": {"mean": 520, "std": 95}, "B": {"mean": 460, "std": 85}},
    "rubavu":     {"A": {"mean": 510, "std": 90}, "B": {"mean": 450, "std": 80}},
    "nyabihu":    {"A": {"mean": 530, "std": 100}, "B": {"mean": 470, "std": 90}},
    "burera":     {"A": {"mean": 490, "std": 90}, "B": {"mean": 430, "std": 80}},
    "gakenke":    {"A": {"mean": 460, "std": 85}, "B": {"mean": 400, "std": 75}},
    # --- Central plateau ---
    "kigali":     {"A": {"mean": 400, "std": 80}, "B": {"mean": 350, "std": 70}},
    "gasabo":     {"A": {"mean": 400, "std": 80}, "B": {"mean": 350, "std": 70}},
    "kicukiro":   {"A": {"mean": 400, "std": 80}, "B": {"mean": 350, "std": 70}},
    "nyarugenge": {"A": {"mean": 400, "std": 80}, "B": {"mean": 350, "std": 70}},
    "muhanga":    {"A": {"mean": 430, "std": 85}, "B": {"mean": 380, "std": 75}},
    "kamonyi":    {"A": {"mean": 420, "std": 80}, "B": {"mean": 370, "std": 70}},
    "ruhango":    {"A": {"mean": 410, "std": 80}, "B": {"mean": 360, "std": 70}},
    "huye":       {"A": {"mean": 440, "std": 85}, "B": {"mean": 390, "std": 75}},
    "nyanza":     {"A": {"mean": 410, "std": 80}, "B": {"mean": 360, "std": 70}},
    "gisagara":   {"A": {"mean": 420, "std": 80}, "B": {"mean": 370, "std": 70}},
    "nyamagabe":  {"A": {"mean": 460, "std": 90}, "B": {"mean": 410, "std": 80}},
    # --- Eastern lowland ---
    "bugesera":   {"A": {"mean": 340, "std": 75}, "B": {"mean": 290, "std": 65}},
    "kayonza":    {"A": {"mean": 360, "std": 75}, "B": {"mean": 310, "std": 65}},
    "kirehe":     {"A": {"mean": 350, "std": 75}, "B": {"mean": 300, "std": 65}},
    "ngoma":      {"A": {"mean": 370, "std": 80}, "B": {"mean": 320, "std": 70}},
    "gatsibo":    {"A": {"mean": 380, "std": 80}, "B": {"mean": 330, "std": 70}},
    "nyagatare":  {"A": {"mean": 350, "std": 80}, "B": {"mean": 300, "std": 70}},
    "rwamagana":  {"A": {"mean": 380, "std": 80}, "B": {"mean": 330, "std": 70}},
    # --- Southwest / lake-influenced ---
    "nyamasheke": {"A": {"mean": 470, "std": 90}, "B": {"mean": 420, "std": 80}},
    "rusizi":     {"A": {"mean": 450, "std": 85}, "B": {"mean": 400, "std": 75}},
    "karongi":    {"A": {"mean": 460, "std": 90}, "B": {"mean": 410, "std": 80}},
    "rutsiro":    {"A": {"mean": 470, "std": 90}, "B": {"mean": 420, "std": 80}},
    "ngororero":  {"A": {"mean": 440, "std": 85}, "B": {"mean": 390, "std": 75}},
    "rulindo":    {"A": {"mean": 430, "std": 85}, "B": {"mean": 380, "std": 75}},
}

_NATIONAL_RAINFALL_NORMALS: dict[str, dict[str, float]] = {
    "A": {"mean": 400.0, "std": 85.0},
    "B": {"mean": 350.0, "std": 75.0},
}

# Primary crops by district, ordered by dominance.
# Source: Rwanda MINAGRI Crop Assessment Survey + RAB crop suitability maps.
# First crop is the default when user doesn't specify one.
_DISTRICT_PRIMARY_CROPS: dict[str, list[str]] = {
    # Northwest highlands (>2000m) — potato, wheat, pyrethrum (no maize)
    "musanze":    ["potato", "wheat", "beans", "peas"],
    "burera":     ["potato", "wheat", "beans", "peas"],
    "nyabihu":    ["potato", "wheat", "beans", "sorghum"],
    "rubavu":     ["potato", "beans", "cassava", "sweet_potato"],
    # Northern transition
    "gakenke":    ["beans", "potato", "maize", "sorghum"],
    "rulindo":    ["beans", "maize", "potato", "cassava"],
    # Central plateau — beans + maize dominant
    "kigali":     ["beans", "maize", "cassava", "sweet_potato"],
    "gasabo":     ["beans", "maize", "cassava", "sweet_potato"],
    "kicukiro":   ["beans", "maize", "cassava", "sweet_potato"],
    "nyarugenge": ["beans", "maize", "cassava", "sweet_potato"],
    "muhanga":    ["beans", "maize", "sweet_potato", "cassava"],
    "kamonyi":    ["beans", "maize", "sweet_potato", "cassava"],
    "ruhango":    ["beans", "maize", "sorghum", "cassava"],
    "huye":       ["beans", "maize", "sweet_potato", "soybean"],
    "nyanza":     ["beans", "maize", "cassava", "sweet_potato"],
    "gisagara":   ["beans", "maize", "cassava", "rice"],
    "nyamagabe":  ["beans", "potato", "maize", "wheat"],
    # Eastern lowland — maize + sorghum dominant
    "bugesera":   ["maize", "sorghum", "cassava", "beans"],
    "kayonza":    ["maize", "beans", "rice", "cassava"],
    "kirehe":     ["maize", "beans", "cassava", "sorghum"],
    "ngoma":      ["maize", "beans", "rice", "cassava"],
    "gatsibo":    ["maize", "beans", "sorghum", "cassava"],
    "nyagatare":  ["maize", "sorghum", "beans", "groundnut"],
    "rwamagana":  ["maize", "beans", "cassava", "rice"],
    # Southwest lake-influenced — beans + cassava
    "nyamasheke": ["beans", "cassava", "sweet_potato", "rice"],
    "rusizi":     ["rice", "beans", "cassava", "maize"],
    "karongi":    ["beans", "cassava", "sweet_potato", "maize"],
    "rutsiro":    ["beans", "maize", "cassava", "sweet_potato"],
    "ngororero":  ["beans", "maize", "sweet_potato", "cassava"],
}


def _default_crop_for_district(district: Optional[str]) -> str:
    """Return the primary crop for a district, or 'beans' as national fallback.

    Beans are Rwanda's most widely grown crop across all agro-ecological zones.
    """
    if district:
        crops = _DISTRICT_PRIMARY_CROPS.get(district.lower().strip())
        if crops:
            return crops[0]
    return "beans"


# Per-district MONTHLY rainfall normals (mm per month).
# Derived from CHIRPS v2.0 2000-2023 monthly totals for Rwanda.
# Rwanda bimodal pattern: Sep-Dec (Season A), Feb-May (Season B), dry Jun-Aug and Jan.
# Structure: district -> month (1-12) -> {"mean": mm, "std": mm}
_MONTHLY_RAINFALL_NORMALS: dict[str, dict[int, dict[str, float]]] = {
    # --- Northwest highlands (wet, orographic enhancement) ---
    "musanze":    {1: {"mean": 55, "std": 28}, 2: {"mean": 80, "std": 32}, 3: {"mean": 120, "std": 40}, 4: {"mean": 140, "std": 42}, 5: {"mean": 85, "std": 35}, 6: {"mean": 18, "std": 14}, 7: {"mean": 10, "std": 10}, 8: {"mean": 25, "std": 16}, 9: {"mean": 75, "std": 32}, 10: {"mean": 130, "std": 42}, 11: {"mean": 145, "std": 44}, 12: {"mean": 90, "std": 35}},
    "rubavu":     {1: {"mean": 50, "std": 26}, 2: {"mean": 75, "std": 30}, 3: {"mean": 115, "std": 38}, 4: {"mean": 135, "std": 40}, 5: {"mean": 80, "std": 33}, 6: {"mean": 15, "std": 12}, 7: {"mean": 8, "std": 8}, 8: {"mean": 22, "std": 15}, 9: {"mean": 70, "std": 30}, 10: {"mean": 125, "std": 40}, 11: {"mean": 140, "std": 42}, 12: {"mean": 85, "std": 33}},
    "nyabihu":    {1: {"mean": 58, "std": 30}, 2: {"mean": 85, "std": 34}, 3: {"mean": 125, "std": 42}, 4: {"mean": 145, "std": 44}, 5: {"mean": 90, "std": 36}, 6: {"mean": 20, "std": 15}, 7: {"mean": 12, "std": 11}, 8: {"mean": 28, "std": 18}, 9: {"mean": 80, "std": 34}, 10: {"mean": 135, "std": 44}, 11: {"mean": 150, "std": 46}, 12: {"mean": 95, "std": 36}},
    "burera":     {1: {"mean": 50, "std": 26}, 2: {"mean": 72, "std": 30}, 3: {"mean": 110, "std": 38}, 4: {"mean": 130, "std": 40}, 5: {"mean": 78, "std": 32}, 6: {"mean": 16, "std": 13}, 7: {"mean": 9, "std": 9}, 8: {"mean": 24, "std": 16}, 9: {"mean": 68, "std": 30}, 10: {"mean": 120, "std": 40}, 11: {"mean": 135, "std": 42}, 12: {"mean": 83, "std": 33}},
    "gakenke":    {1: {"mean": 45, "std": 24}, 2: {"mean": 68, "std": 28}, 3: {"mean": 105, "std": 36}, 4: {"mean": 125, "std": 38}, 5: {"mean": 72, "std": 30}, 6: {"mean": 14, "std": 12}, 7: {"mean": 8, "std": 8}, 8: {"mean": 22, "std": 15}, 9: {"mean": 65, "std": 28}, 10: {"mean": 115, "std": 38}, 11: {"mean": 128, "std": 40}, 12: {"mean": 78, "std": 32}},
    # --- Central plateau (moderate) ---
    "kigali":     {1: {"mean": 38, "std": 22}, 2: {"mean": 60, "std": 26}, 3: {"mean": 95, "std": 34}, 4: {"mean": 115, "std": 36}, 5: {"mean": 58, "std": 26}, 6: {"mean": 10, "std": 10}, 7: {"mean": 5, "std": 6}, 8: {"mean": 18, "std": 14}, 9: {"mean": 55, "std": 26}, 10: {"mean": 100, "std": 35}, 11: {"mean": 110, "std": 36}, 12: {"mean": 65, "std": 28}},
    "gasabo":     {1: {"mean": 38, "std": 22}, 2: {"mean": 60, "std": 26}, 3: {"mean": 95, "std": 34}, 4: {"mean": 115, "std": 36}, 5: {"mean": 58, "std": 26}, 6: {"mean": 10, "std": 10}, 7: {"mean": 5, "std": 6}, 8: {"mean": 18, "std": 14}, 9: {"mean": 55, "std": 26}, 10: {"mean": 100, "std": 35}, 11: {"mean": 110, "std": 36}, 12: {"mean": 65, "std": 28}},
    "kicukiro":   {1: {"mean": 38, "std": 22}, 2: {"mean": 60, "std": 26}, 3: {"mean": 95, "std": 34}, 4: {"mean": 115, "std": 36}, 5: {"mean": 58, "std": 26}, 6: {"mean": 10, "std": 10}, 7: {"mean": 5, "std": 6}, 8: {"mean": 18, "std": 14}, 9: {"mean": 55, "std": 26}, 10: {"mean": 100, "std": 35}, 11: {"mean": 110, "std": 36}, 12: {"mean": 65, "std": 28}},
    "nyarugenge": {1: {"mean": 38, "std": 22}, 2: {"mean": 60, "std": 26}, 3: {"mean": 95, "std": 34}, 4: {"mean": 115, "std": 36}, 5: {"mean": 58, "std": 26}, 6: {"mean": 10, "std": 10}, 7: {"mean": 5, "std": 6}, 8: {"mean": 18, "std": 14}, 9: {"mean": 55, "std": 26}, 10: {"mean": 100, "std": 35}, 11: {"mean": 110, "std": 36}, 12: {"mean": 65, "std": 28}},
    "muhanga":    {1: {"mean": 42, "std": 24}, 2: {"mean": 65, "std": 28}, 3: {"mean": 100, "std": 35}, 4: {"mean": 120, "std": 38}, 5: {"mean": 62, "std": 28}, 6: {"mean": 12, "std": 11}, 7: {"mean": 6, "std": 7}, 8: {"mean": 20, "std": 15}, 9: {"mean": 60, "std": 28}, 10: {"mean": 108, "std": 36}, 11: {"mean": 118, "std": 38}, 12: {"mean": 70, "std": 30}},
    "kamonyi":    {1: {"mean": 40, "std": 23}, 2: {"mean": 62, "std": 27}, 3: {"mean": 98, "std": 34}, 4: {"mean": 118, "std": 37}, 5: {"mean": 60, "std": 27}, 6: {"mean": 11, "std": 10}, 7: {"mean": 5, "std": 6}, 8: {"mean": 19, "std": 14}, 9: {"mean": 58, "std": 27}, 10: {"mean": 105, "std": 36}, 11: {"mean": 115, "std": 37}, 12: {"mean": 68, "std": 29}},
    "ruhango":    {1: {"mean": 39, "std": 22}, 2: {"mean": 61, "std": 26}, 3: {"mean": 96, "std": 34}, 4: {"mean": 116, "std": 36}, 5: {"mean": 58, "std": 26}, 6: {"mean": 10, "std": 10}, 7: {"mean": 5, "std": 6}, 8: {"mean": 18, "std": 14}, 9: {"mean": 56, "std": 26}, 10: {"mean": 102, "std": 35}, 11: {"mean": 112, "std": 36}, 12: {"mean": 66, "std": 28}},
    "huye":       {1: {"mean": 43, "std": 24}, 2: {"mean": 66, "std": 28}, 3: {"mean": 102, "std": 36}, 4: {"mean": 122, "std": 38}, 5: {"mean": 64, "std": 28}, 6: {"mean": 12, "std": 11}, 7: {"mean": 6, "std": 7}, 8: {"mean": 20, "std": 15}, 9: {"mean": 62, "std": 28}, 10: {"mean": 110, "std": 37}, 11: {"mean": 120, "std": 38}, 12: {"mean": 72, "std": 30}},
    "nyanza":     {1: {"mean": 39, "std": 22}, 2: {"mean": 61, "std": 26}, 3: {"mean": 96, "std": 34}, 4: {"mean": 116, "std": 36}, 5: {"mean": 58, "std": 26}, 6: {"mean": 10, "std": 10}, 7: {"mean": 5, "std": 6}, 8: {"mean": 18, "std": 14}, 9: {"mean": 56, "std": 26}, 10: {"mean": 102, "std": 35}, 11: {"mean": 112, "std": 36}, 12: {"mean": 66, "std": 28}},
    "gisagara":   {1: {"mean": 40, "std": 23}, 2: {"mean": 62, "std": 27}, 3: {"mean": 98, "std": 34}, 4: {"mean": 118, "std": 37}, 5: {"mean": 60, "std": 27}, 6: {"mean": 11, "std": 10}, 7: {"mean": 5, "std": 6}, 8: {"mean": 19, "std": 14}, 9: {"mean": 58, "std": 27}, 10: {"mean": 105, "std": 36}, 11: {"mean": 115, "std": 37}, 12: {"mean": 68, "std": 29}},
    "nyamagabe":  {1: {"mean": 45, "std": 25}, 2: {"mean": 70, "std": 30}, 3: {"mean": 108, "std": 37}, 4: {"mean": 128, "std": 40}, 5: {"mean": 68, "std": 30}, 6: {"mean": 14, "std": 12}, 7: {"mean": 8, "std": 8}, 8: {"mean": 22, "std": 15}, 9: {"mean": 65, "std": 29}, 10: {"mean": 115, "std": 38}, 11: {"mean": 125, "std": 40}, 12: {"mean": 75, "std": 31}},
    # --- Eastern lowland (dry, continental) ---
    "bugesera":   {1: {"mean": 30, "std": 20}, 2: {"mean": 50, "std": 24}, 3: {"mean": 80, "std": 30}, 4: {"mean": 95, "std": 32}, 5: {"mean": 48, "std": 23}, 6: {"mean": 8, "std": 8}, 7: {"mean": 3, "std": 4}, 8: {"mean": 14, "std": 12}, 9: {"mean": 45, "std": 22}, 10: {"mean": 85, "std": 32}, 11: {"mean": 95, "std": 33}, 12: {"mean": 55, "std": 25}},
    "kayonza":    {1: {"mean": 32, "std": 20}, 2: {"mean": 52, "std": 24}, 3: {"mean": 85, "std": 32}, 4: {"mean": 100, "std": 34}, 5: {"mean": 50, "std": 24}, 6: {"mean": 9, "std": 9}, 7: {"mean": 4, "std": 5}, 8: {"mean": 15, "std": 12}, 9: {"mean": 48, "std": 23}, 10: {"mean": 90, "std": 33}, 11: {"mean": 100, "std": 34}, 12: {"mean": 58, "std": 26}},
    "kirehe":     {1: {"mean": 30, "std": 20}, 2: {"mean": 50, "std": 24}, 3: {"mean": 82, "std": 31}, 4: {"mean": 98, "std": 33}, 5: {"mean": 48, "std": 23}, 6: {"mean": 8, "std": 8}, 7: {"mean": 3, "std": 4}, 8: {"mean": 14, "std": 12}, 9: {"mean": 46, "std": 22}, 10: {"mean": 88, "std": 32}, 11: {"mean": 96, "std": 33}, 12: {"mean": 55, "std": 25}},
    "ngoma":      {1: {"mean": 34, "std": 21}, 2: {"mean": 55, "std": 25}, 3: {"mean": 88, "std": 32}, 4: {"mean": 105, "std": 35}, 5: {"mean": 52, "std": 25}, 6: {"mean": 10, "std": 10}, 7: {"mean": 4, "std": 5}, 8: {"mean": 16, "std": 13}, 9: {"mean": 50, "std": 24}, 10: {"mean": 92, "std": 33}, 11: {"mean": 102, "std": 35}, 12: {"mean": 60, "std": 27}},
    "gatsibo":    {1: {"mean": 35, "std": 22}, 2: {"mean": 56, "std": 25}, 3: {"mean": 90, "std": 33}, 4: {"mean": 108, "std": 35}, 5: {"mean": 54, "std": 25}, 6: {"mean": 10, "std": 10}, 7: {"mean": 4, "std": 5}, 8: {"mean": 16, "std": 13}, 9: {"mean": 52, "std": 24}, 10: {"mean": 95, "std": 34}, 11: {"mean": 105, "std": 35}, 12: {"mean": 62, "std": 27}},
    "nyagatare":  {1: {"mean": 30, "std": 20}, 2: {"mean": 50, "std": 24}, 3: {"mean": 82, "std": 31}, 4: {"mean": 98, "std": 33}, 5: {"mean": 48, "std": 23}, 6: {"mean": 8, "std": 8}, 7: {"mean": 3, "std": 4}, 8: {"mean": 14, "std": 12}, 9: {"mean": 46, "std": 22}, 10: {"mean": 88, "std": 32}, 11: {"mean": 96, "std": 33}, 12: {"mean": 55, "std": 25}},
    "rwamagana":  {1: {"mean": 35, "std": 22}, 2: {"mean": 56, "std": 25}, 3: {"mean": 88, "std": 32}, 4: {"mean": 105, "std": 35}, 5: {"mean": 52, "std": 25}, 6: {"mean": 10, "std": 10}, 7: {"mean": 4, "std": 5}, 8: {"mean": 16, "std": 13}, 9: {"mean": 50, "std": 24}, 10: {"mean": 92, "std": 33}, 11: {"mean": 102, "std": 35}, 12: {"mean": 60, "std": 27}},
    # --- Southwest / lake-influenced (moderate-wet) ---
    "nyamasheke": {1: {"mean": 48, "std": 26}, 2: {"mean": 72, "std": 30}, 3: {"mean": 110, "std": 38}, 4: {"mean": 130, "std": 40}, 5: {"mean": 72, "std": 30}, 6: {"mean": 14, "std": 12}, 7: {"mean": 8, "std": 8}, 8: {"mean": 22, "std": 15}, 9: {"mean": 66, "std": 28}, 10: {"mean": 118, "std": 38}, 11: {"mean": 128, "std": 40}, 12: {"mean": 78, "std": 32}},
    "rusizi":     {1: {"mean": 45, "std": 25}, 2: {"mean": 68, "std": 28}, 3: {"mean": 105, "std": 36}, 4: {"mean": 125, "std": 38}, 5: {"mean": 68, "std": 28}, 6: {"mean": 12, "std": 11}, 7: {"mean": 7, "std": 7}, 8: {"mean": 20, "std": 14}, 9: {"mean": 62, "std": 27}, 10: {"mean": 112, "std": 37}, 11: {"mean": 122, "std": 38}, 12: {"mean": 74, "std": 31}},
    "karongi":    {1: {"mean": 46, "std": 25}, 2: {"mean": 70, "std": 29}, 3: {"mean": 108, "std": 37}, 4: {"mean": 128, "std": 39}, 5: {"mean": 70, "std": 29}, 6: {"mean": 13, "std": 11}, 7: {"mean": 7, "std": 7}, 8: {"mean": 21, "std": 15}, 9: {"mean": 64, "std": 28}, 10: {"mean": 115, "std": 38}, 11: {"mean": 125, "std": 39}, 12: {"mean": 76, "std": 31}},
    "rutsiro":    {1: {"mean": 48, "std": 26}, 2: {"mean": 72, "std": 30}, 3: {"mean": 110, "std": 38}, 4: {"mean": 130, "std": 40}, 5: {"mean": 72, "std": 30}, 6: {"mean": 14, "std": 12}, 7: {"mean": 8, "std": 8}, 8: {"mean": 22, "std": 15}, 9: {"mean": 66, "std": 28}, 10: {"mean": 118, "std": 38}, 11: {"mean": 128, "std": 40}, 12: {"mean": 78, "std": 32}},
    "ngororero":  {1: {"mean": 43, "std": 24}, 2: {"mean": 66, "std": 28}, 3: {"mean": 102, "std": 36}, 4: {"mean": 122, "std": 38}, 5: {"mean": 64, "std": 28}, 6: {"mean": 12, "std": 11}, 7: {"mean": 6, "std": 7}, 8: {"mean": 20, "std": 14}, 9: {"mean": 60, "std": 27}, 10: {"mean": 108, "std": 36}, 11: {"mean": 118, "std": 38}, 12: {"mean": 72, "std": 30}},
    "rulindo":    {1: {"mean": 42, "std": 24}, 2: {"mean": 65, "std": 28}, 3: {"mean": 100, "std": 35}, 4: {"mean": 120, "std": 38}, 5: {"mean": 62, "std": 28}, 6: {"mean": 12, "std": 11}, 7: {"mean": 6, "std": 7}, 8: {"mean": 20, "std": 15}, 9: {"mean": 60, "std": 28}, 10: {"mean": 108, "std": 36}, 11: {"mean": 118, "std": 38}, 12: {"mean": 70, "std": 30}},
}

# National monthly fallback (average across all districts)
_NATIONAL_MONTHLY_NORMALS: dict[int, dict[str, float]] = {
    1: {"mean": 40, "std": 23}, 2: {"mean": 62, "std": 27}, 3: {"mean": 98, "std": 34},
    4: {"mean": 116, "std": 37}, 5: {"mean": 60, "std": 27}, 6: {"mean": 11, "std": 10},
    7: {"mean": 6, "std": 7}, 8: {"mean": 19, "std": 14}, 9: {"mean": 57, "std": 26},
    10: {"mean": 104, "std": 35}, 11: {"mean": 114, "std": 37}, 12: {"mean": 68, "std": 29},
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PhaseRainfall:
    phase: str
    cumulative_mm: float
    day_count: int
    daily_avg_mm: float
    date_from: str
    date_to: str


@dataclass
class TriggerResult:
    signal: str
    current_value: float
    threshold: float
    direction: str
    triggered: bool
    margin_pct: float
    weight: float
    description: str

    def to_dict(self) -> dict:
        return {
            "signal": self.signal,
            "current_value": round(self.current_value, 2),
            "threshold": self.threshold,
            "direction": self.direction,
            "triggered": self.triggered,
            "margin_pct": round(self.margin_pct, 1),
            "weight": self.weight,
            "description": self.description,
        }


@dataclass
class InsuranceReport:
    location_name: str
    admin_level: str
    crop: str
    season: str
    growth_phase: str
    days_after_planting: int

    phase_rainfall: list[PhaseRainfall] = field(default_factory=list)
    season_rainfall_mm: float = 0.0
    spi: float = 0.0
    spi_1: Optional[float] = None
    spi_3: Optional[float] = None
    drought_diagnostic: str = "insufficient_data"
    drought_diagnostic_label: str = ""

    ndvi_z_score: Optional[float] = None
    ndvi_concordance_score: Optional[float] = None

    et_anomaly_pct: Optional[float] = None
    soil_moisture_pct: Optional[float] = None

    max_dry_spell_days: int = 0
    active_dry_spell_days: int = 0

    triggers: list[TriggerResult] = field(default_factory=list)
    triggers_activated: int = 0
    triggers_total: int = 0

    confidence_score: int = 0
    overall_status: str = "UNKNOWN"
    recommendation: str = ""

    accuracy_components: Optional[dict] = None

    forecast_outlook: Optional[dict] = None

    sources: list[str] = field(default_factory=list)
    period_start: str = ""
    period_end: str = ""
    computed_at: str = ""
    geometry: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "location": self.location_name,
            "admin_level": self.admin_level,
            "crop": self.crop,
            "season": self.season,
            "growth_phase": self.growth_phase,
            "days_after_planting": self.days_after_planting,
            "season_rainfall_mm": round(self.season_rainfall_mm, 1),
            "spi": round(self.spi, 2),
            "spi_1": round(self.spi_1, 2) if self.spi_1 is not None else None,
            "spi_3": round(self.spi_3, 2) if self.spi_3 is not None else None,
            "drought_diagnostic": self.drought_diagnostic,
            "drought_diagnostic_label": self.drought_diagnostic_label,
            "phase_rainfall": [
                {
                    "phase": p.phase,
                    "cumulative_mm": round(p.cumulative_mm, 1),
                    "day_count": p.day_count,
                    "daily_avg_mm": round(p.daily_avg_mm, 1),
                    "date_from": p.date_from,
                    "date_to": p.date_to,
                }
                for p in self.phase_rainfall
            ],
            "ndvi_z_score": round(self.ndvi_z_score, 2) if self.ndvi_z_score is not None else None,
            "ndvi_concordance_score": round(self.ndvi_concordance_score, 2) if self.ndvi_concordance_score is not None else None,
            "et_anomaly_pct": round(self.et_anomaly_pct, 1) if self.et_anomaly_pct is not None else None,
            "soil_moisture_pct": round(self.soil_moisture_pct, 1) if self.soil_moisture_pct is not None else None,
            "max_dry_spell_days": self.max_dry_spell_days,
            "active_dry_spell_days": self.active_dry_spell_days,
            "triggers": [t.to_dict() for t in self.triggers],
            "triggers_activated": self.triggers_activated,
            "triggers_total": self.triggers_total,
            "confidence_score": self.confidence_score,
            "overall_status": self.overall_status,
            "recommendation": self.recommendation,
            "accuracy_components": self.accuracy_components,
            "forecast_outlook": self.forecast_outlook,
            "sources": self.sources,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "computed_at": self.computed_at,
        }


# ---------------------------------------------------------------------------
# 1. Growth-phase rainfall accumulation
# ---------------------------------------------------------------------------

def _get_planting_date(crop: str, season: str, year: int) -> date:
    """Get planting date for a crop/season/year from crop calendars."""
    from src.services.dssat_service import _CROP_CALENDARS

    cal = _CROP_CALENDARS.get(crop, {}).get(season)
    if not cal:
        cal = _CROP_CALENDARS.get("maize", {}).get("A", {"planting": "09-15"})
    month, day = cal["planting"].split("-")
    return date(year, int(month), int(day))


def _get_harvest_dap(crop: str, season: str) -> int:
    from src.services.dssat_service import _CROP_CALENDARS
    cal = _CROP_CALENDARS.get(crop, {}).get(season)
    if not cal:
        return 120
    return cal.get("harvest_dap", 120)


def _current_growth_phase(crop: str, dap: int) -> str:
    phases = _GROWTH_PHASES.get(crop, _GROWTH_PHASES["maize"])
    for phase_name, (start, end) in phases.items():
        if start <= dap < end:
            return phase_name
    return "maturity"


def _compute_phase_rainfall(
    daily_precip: dict[str, Optional[float]],
    planting_date: date,
    crop: str,
    today: date,
) -> list[PhaseRainfall]:
    """Accumulate rainfall per growth phase from daily CHIRPS data."""
    phases = _GROWTH_PHASES.get(crop, _GROWTH_PHASES["maize"])
    results = []

    for phase_name, (dap_start, dap_end) in phases.items():
        phase_start = planting_date + timedelta(days=dap_start)
        phase_end = min(planting_date + timedelta(days=dap_end), today)
        if phase_start > today:
            break

        total_mm = 0.0
        day_count = 0
        total_days = 0
        d = phase_start
        while d < phase_end:
            total_days += 1
            key = d.strftime("%Y-%m-%d")
            val = daily_precip.get(key)
            if val is not None:
                total_mm += val
                day_count += 1
            d += timedelta(days=1)

        # Extrapolate only when we have ≥30% sample coverage; otherwise
        # report the raw sum to avoid amplifying sparse observations.
        min_coverage = 0.3
        if day_count > 0 and total_days > 0 and (day_count / total_days) >= min_coverage:
            daily_avg = total_mm / day_count
            estimated_cumulative = daily_avg * total_days
        else:
            daily_avg = total_mm / max(day_count, 1)
            estimated_cumulative = total_mm

        results.append(PhaseRainfall(
            phase=phase_name,
            cumulative_mm=estimated_cumulative,
            day_count=day_count,
            daily_avg_mm=daily_avg,
            date_from=phase_start.strftime("%Y-%m-%d"),
            date_to=phase_end.strftime("%Y-%m-%d"),
        ))

    return results


# ---------------------------------------------------------------------------
# 2. SPI-1 and SPI-3 from monthly CHIRPS windows
# ---------------------------------------------------------------------------

def _get_monthly_normals(month: int, district: Optional[str] = None) -> dict[str, float]:
    """Get monthly rainfall normals (mean, std) for a given month and district."""
    if district:
        district_key = district.lower().strip()
        district_months = _MONTHLY_RAINFALL_NORMALS.get(district_key)
        if district_months and month in district_months:
            return district_months[month]
    return _NATIONAL_MONTHLY_NORMALS.get(month, {"mean": 60, "std": 25})


def _compute_spi_from_daily(
    daily_precip: dict[str, Optional[float]],
    ref_date: date,
    window_days: int,
    district: Optional[str] = None,
) -> Optional[float]:
    """Compute SPI for a specific window ending at ref_date.

    Sums observed daily rainfall over the window, then compares against the
    expected normal for those calendar months.  For SPI-1 (30 days) we use
    the single month's normals.  For SPI-3 (90 days) we sum the normals
    for the 3 months covered.  This is a simplified z-score SPI — proper
    gamma-distribution fitting needs 30+ years of monthly totals which we
    don't have per-pixel.  The z-score approach is standard for operational
    approximation when gamma fit isn't available.
    """
    window_start = ref_date - timedelta(days=window_days - 1)

    observed = 0.0
    obs_count = 0
    for i in range(window_days):
        d = (window_start + timedelta(days=i)).strftime("%Y-%m-%d")
        val = daily_precip.get(d)
        if val is not None:
            observed += val
            obs_count += 1

    if obs_count < window_days * 0.4:
        return None

    if obs_count < window_days:
        observed = observed * (window_days / obs_count)

    import calendar
    month_day_counts: dict[tuple[int, int], int] = {}
    for i in range(window_days):
        d = window_start + timedelta(days=i)
        key = (d.year, d.month)
        month_day_counts[key] = month_day_counts.get(key, 0) + 1

    expected_mean = 0.0
    expected_var = 0.0
    for (yr, mo), days_in_window in month_day_counts.items():
        days_in_month = calendar.monthrange(yr, mo)[1]
        fraction = days_in_window / days_in_month
        normals = _get_monthly_normals(mo, district)
        expected_mean += normals["mean"] * fraction
        expected_var += (normals["std"] * fraction) ** 2

    expected_std = expected_var ** 0.5
    if expected_std < 1.0:
        return 0.0
    return (observed - expected_mean) / expected_std


def _compute_spi_pair(
    daily_precip: dict[str, Optional[float]],
    ref_date: date,
    district: Optional[str] = None,
) -> dict[str, Optional[float]]:
    """Compute SPI-1 (30-day) and SPI-3 (90-day) from daily CHIRPS data."""
    return {
        "spi_1": _compute_spi_from_daily(daily_precip, ref_date, 30, district),
        "spi_3": _compute_spi_from_daily(daily_precip, ref_date, 90, district),
    }


def _classify_drought_state(
    spi_3: Optional[float],
    soil_moisture_pct: Optional[float],
) -> str:
    """Classify SPI-SM divergence into a named drought diagnostic.

    Patterns (from Copernicus EDO Combined Drought Indicator):
      SPI dry  + SM dry    → consistent_drought (meteorological → agricultural)
      SPI ok   + SM dry    → flash_drought (high ET demand, heatwave)
      SPI dry  + SM ok     → carryover_storage (irrigation, shallow water table)
      SPI wet  + SM dry    → runoff_dominated (steep slopes, hardpan, intense storms)
      otherwise            → normal
    """
    if spi_3 is None or soil_moisture_pct is None:
        return "insufficient_data"

    spi_dry = spi_3 < -1.0
    spi_ok = -1.0 <= spi_3 <= 1.0
    spi_wet = spi_3 > 1.0
    sm_dry = soil_moisture_pct < 35.0
    sm_ok = soil_moisture_pct >= 35.0

    if spi_dry and sm_dry:
        return "consistent_drought"
    if spi_ok and sm_dry:
        return "flash_drought"
    if spi_dry and sm_ok:
        return "carryover_storage"
    if spi_wet and sm_dry:
        return "runoff_dominated"
    return "normal"


_DROUGHT_STATE_LABELS: dict[str, str] = {
    "consistent_drought": "Consistent drought — precipitation deficit confirmed by soil moisture drop",
    "flash_drought": "Flash drought — soil drying from high ET demand despite normal rainfall",
    "carryover_storage": "Soil buffered — precipitation deficit not yet reflected in soil moisture (irrigation, shallow water table, or stored moisture)",
    "runoff_dominated": "Runoff-dominated — rainfall not reaching soil (steep terrain, hardpan, or intense convective storms)",
    "normal": "Normal conditions — no significant drought signal",
    "insufficient_data": "Insufficient data for drought classification",
}


def _compute_spi(
    season_rainfall_mm: float,
    season: str,
    district: Optional[str] = None,
) -> float:
    """Legacy SPI from season cumulative vs long-term normals.

    Kept for backward compatibility with trigger evaluation which expects a
    single SPI value.  New code should use _compute_spi_pair().
    """
    normals = _NATIONAL_RAINFALL_NORMALS.get(season, _NATIONAL_RAINFALL_NORMALS["A"])
    if district:
        district_key = district.lower().strip()
        district_normals = _DISTRICT_RAINFALL_NORMALS.get(district_key, {})
        if season in district_normals:
            normals = district_normals[season]
    if normals["std"] == 0:
        return 0.0
    return (season_rainfall_mm - normals["mean"]) / normals["std"]


# ---------------------------------------------------------------------------
# 3. NDVI anomaly from database cache
# ---------------------------------------------------------------------------

async def _fetch_ndvi_anomaly(
    conn: asyncpg.Connection,
    district: Optional[str] = None,
) -> Optional[float]:
    """Get latest mean NDVI z-score from anomaly_alerts_cache."""
    try:
        if district:
            row = await conn.fetchrow(
                "SELECT AVG(z_score) as mean_z FROM anomaly_alerts_cache "
                "WHERE LOWER(district) = LOWER($1) "
                "AND computed_at > NOW() - INTERVAL '30 days'",
                district,
            )
        else:
            row = await conn.fetchrow(
                "SELECT AVG(z_score) as mean_z FROM anomaly_alerts_cache "
                "WHERE computed_at > NOW() - INTERVAL '30 days'",
            )
        if row and row["mean_z"] is not None:
            return float(row["mean_z"])
    except Exception:
        logger.debug("anomaly_alerts_cache query failed", exc_info=True)
    return None


async def _fetch_sar_backscatter(
    lat: float,
    lon: float,
    date_from: str,
    date_to: str,
) -> Optional[float]:
    """Get mean VH/VV ratio from Sentinel-1 SAR. Cloud-penetrating."""
    try:
        from src.services.sentinel1_service import get_sentinel1_service
        svc = get_sentinel1_service()
        buf = 0.05
        bbox = (lon - buf, lat - buf, lon + buf, lat + buf)
        result = await asyncio.to_thread(
            svc.get_backscatter,
            bbox=bbox,
            date_range=f"{date_from}/{date_to}",
        )
        if result and result.get("status") == "success":
            stats = result.get("statistics", {})
            vh_mean = stats.get("vh", {}).get("mean")
            vv_mean = stats.get("vv", {}).get("mean")
            if vh_mean is not None and vv_mean is not None and vv_mean != 0:
                # Reject NoData sentinels and implausible values.
                # Plausible SAR backscatter: -50 to +10 dB, or 0 to ~10 in linear.
                if vv_mean < -50 or vv_mean > 10 or vh_mean < -50 or vh_mean > 10:
                    return None
                if vv_mean < 0:
                    return 10 ** ((vh_mean - vv_mean) / 10)
                return vh_mean / vv_mean
    except Exception:
        logger.debug("SAR backscatter fetch failed", exc_info=True)
    return None


async def _fetch_ndvi_with_sar_fallback(
    conn: asyncpg.Connection,
    lat: float,
    lon: float,
    date_from: str,
    date_to: str,
    district: Optional[str] = None,
) -> Optional[float]:
    """Get NDVI z-score from optical first, fall back to SAR-predicted NDVI."""
    ndvi_z = await _fetch_ndvi_anomaly(conn, district)
    if ndvi_z is not None:
        return ndvi_z
    try:
        from src.services.sar_ndvi import get_sar_ndvi_predictor
        pred = get_sar_ndvi_predictor()
        buf = 0.05
        bbox = (lon - buf, lat - buf, lon + buf, lat + buf)
        result = await asyncio.to_thread(pred.predict_ndvi, bbox=bbox)
        if result and result.get("status") == "success":
            predicted = result.get("predicted_ndvi")
            if predicted is not None:
                mean_ndvi = 0.45
                std_ndvi = 0.15
                return (predicted - mean_ndvi) / std_ndvi if std_ndvi > 0 else 0.0
    except Exception:
        logger.debug("SAR-predicted NDVI fallback failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# 4. Centroid from GeoJSON geometry
# ---------------------------------------------------------------------------

def _centroid_from_geojson(geom: dict) -> tuple[float, float]:
    """Extract approximate centroid (lat, lon) from a GeoJSON geometry."""
    coords = _flatten_coords(geom.get("coordinates", []))
    if not coords:
        return _RWANDA_CENTER
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def _flatten_coords(coords: Any) -> list[tuple[float, float]]:
    """Recursively flatten nested coordinate arrays to (lon, lat) pairs."""
    if not coords:
        return []
    if isinstance(coords[0], (int, float)):
        return [(coords[0], coords[1])]
    result = []
    for item in coords:
        result.extend(_flatten_coords(item))
    return result


# ---------------------------------------------------------------------------
# 5. Trigger evaluation
# ---------------------------------------------------------------------------

async def _load_triggers(
    conn: asyncpg.Connection,
    crop: str,
    season: str,
    phase: str,
    district: Optional[str] = None,
) -> list[dict]:
    """Load trigger thresholds from insurance_triggers table.

    District-specific rows override national defaults (district IS NULL)
    for the same (phase, signal) combination.
    """
    try:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (phase, signal) "
            "signal, direction, threshold, weight, description "
            "FROM insurance_triggers "
            "WHERE crop = $1 AND season = $2 AND (phase = $3 OR phase = 'full_season') "
            "AND enabled = true "
            "AND (district IS NULL OR LOWER(district) = LOWER($4)) "
            "ORDER BY phase, signal, "
            "CASE WHEN district IS NOT NULL THEN 0 ELSE 1 END, "
            "weight DESC",
            crop, season, phase, district,
        )
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("insurance_triggers table not available, using defaults", exc_info=True)
        return _default_triggers(phase)


def _default_triggers(phase: str) -> list[dict]:
    """Hardcoded fallback triggers when the table doesn't exist yet."""
    triggers = [
        {"signal": "rainfall_cumulative", "direction": "below", "threshold": 100.0, "weight": 1.0,
         "description": "Season cumulative rainfall below 100mm"},
        {"signal": "spi", "direction": "below", "threshold": -1.0, "weight": 0.8,
         "description": "SPI indicates moderate drought"},
        {"signal": "dry_spell_days", "direction": "above", "threshold": 15.0, "weight": 0.6,
         "description": "Maximum dry spell exceeds 15 consecutive days"},
        {"signal": "ndvi_z_score", "direction": "below", "threshold": -1.5, "weight": 0.8,
         "description": "NDVI anomaly indicates severe vegetation stress"},
        {"signal": "et_anomaly", "direction": "below", "threshold": -20.0, "weight": 0.4,
         "description": "ET anomaly exceeds -20% deficit"},
        {"signal": "sar_backscatter", "direction": "below", "threshold": 0.15, "weight": 0.7,
         "description": "SAR VH/VV ratio below 0.15 indicates low vegetation density"},
    ]
    return triggers


def _compute_forecast_outlook(
    forecast_data: Optional[dict],
    season_rainfall_so_far: float,
    planting_date: "date",
    harvest_dap: int,
    today: "date",
    crop: str,
    season: str,
    district: Optional[str] = None,
) -> Optional[dict]:
    """Project rainfall triggers forward using bias-corrected multi-model forecasts.

    Takes the bias-corrected consensus forecast (ECMWF IFS + GFS + ICON + GraphCast)
    and projects cumulative rainfall to harvest. Returns probability assessment of
    whether rainfall triggers will fire.
    """
    if not forecast_data or not forecast_data.get("daily"):
        return None

    forecast_daily = forecast_data["daily"]
    days_remaining = max(0, harvest_dap - (today - planting_date).days)
    if days_remaining == 0:
        return None

    # Sum forecast precipitation (bias-corrected consensus mean)
    forecast_precip_days = []
    for day in forecast_daily:
        precip = day.get("precipitation_mm", {})
        if isinstance(precip, dict) and "mean" in precip:
            forecast_precip_days.append({
                "date": day["date"],
                "mean": precip["mean"],
                "p10": precip.get("p10", precip["mean"]),
                "p90": precip.get("p90", precip["mean"]),
                "models": precip.get("models", {}),
                "n_models": precip.get("n_models", 1),
            })

    if not forecast_precip_days:
        return None

    forecast_days_available = len(forecast_precip_days)
    forecast_total_mean = sum(d["mean"] for d in forecast_precip_days)
    forecast_total_p10 = sum(d["p10"] for d in forecast_precip_days)
    forecast_total_p90 = sum(d["p90"] for d in forecast_precip_days)

    # Project to harvest: scale forecast if it doesn't cover remaining days
    if forecast_days_available < days_remaining:
        daily_avg_mean = forecast_total_mean / forecast_days_available
        daily_avg_p10 = forecast_total_p10 / forecast_days_available
        daily_avg_p90 = forecast_total_p90 / forecast_days_available
        projected_mean = daily_avg_mean * days_remaining
        projected_p10 = daily_avg_p10 * days_remaining
        projected_p90 = daily_avg_p90 * days_remaining
        projection_method = f"{forecast_days_available}-day forecast extrapolated to {days_remaining} days"
    else:
        # Forecast covers remaining season — sum only needed days
        projected_mean = sum(d["mean"] for d in forecast_precip_days[:days_remaining])
        projected_p10 = sum(d["p10"] for d in forecast_precip_days[:days_remaining])
        projected_p90 = sum(d["p90"] for d in forecast_precip_days[:days_remaining])
        projection_method = f"{days_remaining}-day forecast (full coverage)"

    # Projected season totals at harvest
    projected_season_mean = season_rainfall_so_far + projected_mean
    projected_season_p10 = season_rainfall_so_far + projected_p10
    projected_season_p90 = season_rainfall_so_far + projected_p90

    # Season minimum rainfall thresholds (mm) — based on crop water requirements
    # for Rwanda's bimodal seasons. These are conservative (parametric insurance
    # typically triggers at 60-70% of crop water need).
    _SEASON_RAIN_THRESHOLDS = {
        "maize": 300, "rice": 400, "beans": 200, "sorghum": 280,
        "potato": 250, "sweet_potato": 300, "cassava": 350,
        "soybean": 280, "groundnut": 280, "wheat": 250,
    }
    rainfall_threshold = float(_SEASON_RAIN_THRESHOLDS.get(crop, 300))

    # Estimate trigger probability from p10/p90 spread
    # If p10 (pessimistic) is below threshold → high probability of trigger
    # If p90 (optimistic) is below threshold → near-certain trigger
    # If mean is above threshold → low probability
    if projected_season_p90 < rainfall_threshold:
        trigger_probability = 0.90
        trigger_risk = "VERY HIGH"
    elif projected_season_mean < rainfall_threshold:
        # Mean below but p90 above — moderate-high probability
        spread = projected_season_p90 - projected_season_p10
        if spread > 0:
            fraction_below = (rainfall_threshold - projected_season_p10) / spread
            trigger_probability = max(0.1, min(0.9, 1.0 - fraction_below))
        else:
            trigger_probability = 0.70
        trigger_risk = "HIGH" if trigger_probability > 0.5 else "MODERATE"
    elif projected_season_p10 < rainfall_threshold:
        spread = projected_season_p90 - projected_season_p10
        if spread > 0:
            fraction_below = (rainfall_threshold - projected_season_p10) / spread
            trigger_probability = max(0.05, min(0.5, 1.0 - fraction_below))
        else:
            trigger_probability = 0.25
        trigger_risk = "MODERATE" if trigger_probability > 0.25 else "LOW"
    else:
        trigger_probability = 0.05
        trigger_risk = "LOW"

    # Model agreement — confidence in forecast
    model_agreement = "HIGH"
    spreads = [d.get("p90", 0) - d.get("p10", 0) for d in forecast_precip_days]
    avg_spread = sum(spreads) / len(spreads) if spreads else 0
    means = [d["mean"] for d in forecast_precip_days]
    avg_mean = sum(means) / len(means) if means else 1
    if avg_mean > 0.5 and avg_spread / avg_mean > 0.8:
        model_agreement = "LOW"
    elif avg_mean > 0.5 and avg_spread / avg_mean > 0.4:
        model_agreement = "MODERATE"

    bias_corrected = forecast_data.get("bias_correction", {}).get("applied", False)
    terrain_corrected = forecast_data.get("terrain_correction", {}).get("applied", False)

    models_used = forecast_data.get("models_used", [])

    return {
        "days_remaining": days_remaining,
        "forecast_days_available": forecast_days_available,
        "projection_method": projection_method,
        "forecast_precip_mm": round(projected_mean, 1),
        "forecast_precip_p10_mm": round(projected_p10, 1),
        "forecast_precip_p90_mm": round(projected_p90, 1),
        "projected_season_total_mm": round(projected_season_mean, 1),
        "projected_season_p10_mm": round(projected_season_p10, 1),
        "projected_season_p90_mm": round(projected_season_p90, 1),
        "rainfall_trigger_threshold_mm": rainfall_threshold,
        "rainfall_trigger_probability": round(trigger_probability, 2),
        "rainfall_trigger_risk": trigger_risk,
        "model_agreement": model_agreement,
        "models_used": models_used,
        "bias_corrected": bias_corrected,
        "terrain_corrected": terrain_corrected,
    }


def _evaluate_triggers(
    trigger_defs: list[dict],
    current_values: dict[str, Optional[float]],
) -> list[TriggerResult]:
    """Evaluate each trigger against current signal values."""
    results = []
    for trig in trigger_defs:
        signal = trig["signal"]
        value = current_values.get(signal)
        if value is None:
            continue

        threshold = trig["threshold"]
        direction = trig["direction"]
        weight = trig.get("weight", 1.0)

        if direction == "below":
            triggered = value < threshold
            margin = ((value - threshold) / abs(threshold)) * 100 if threshold != 0 else 0
        else:
            triggered = value > threshold
            margin = ((value - threshold) / abs(threshold)) * 100 if threshold != 0 else 0
        margin = max(-999, min(999, margin))

        results.append(TriggerResult(
            signal=signal,
            current_value=value,
            threshold=threshold,
            direction=direction,
            triggered=triggered,
            margin_pct=margin,
            weight=weight,
            description=trig.get("description", signal),
        ))

    return results


# ---------------------------------------------------------------------------
# 6. Composite confidence score
# ---------------------------------------------------------------------------

def _compute_confidence(
    triggers: list[TriggerResult],
    expected_signals: int = 0,
) -> tuple[int, str]:
    """Weighted composite confidence score (0-100) and status label.

    When expected_signals > len(triggers), confidence is penalized
    proportionally — missing data means lower certainty.
    """
    if not triggers:
        return 50, "UNKNOWN"

    total_weight = sum(t.weight for t in triggers)
    if total_weight == 0:
        return 50, "UNKNOWN"

    passing_weight = sum(t.weight for t in triggers if not t.triggered)
    score = int((passing_weight / total_weight) * 100)

    if expected_signals > 0 and len(triggers) < expected_signals:
        coverage = len(triggers) / expected_signals
        score = int(score * coverage)

    activated = sum(1 for t in triggers if t.triggered)
    high_weight_activated = any(t.triggered and t.weight >= 0.8 for t in triggers)

    if activated == 0:
        status = "SAFE"
    elif activated == 1 and not high_weight_activated:
        status = "WATCH"
    elif activated <= 2:
        status = "WARNING"
    else:
        status = "PAYOUT_LIKELY"

    return score, status


def _generate_recommendation(
    status: str, crop: str, phase: str, triggers: list[TriggerResult],
) -> str:
    """Generate actionable recommendation based on trigger results."""
    activated = [t for t in triggers if t.triggered]

    if status == "SAFE":
        return f"{crop.title()} crop in {phase} phase is progressing normally. No intervention needed."

    signals = ", ".join(t.signal.replace("_", " ") for t in activated)

    if status == "WATCH":
        return (
            f"Monitor closely: {signals} approaching threshold. "
            f"Recommend field verification within 7 days."
        )
    if status == "WARNING":
        return (
            f"Warning: {signals} exceeded threshold. "
            f"Recommend immediate field assessment and consider early payout preparation."
        )
    return (
        f"Multiple triggers activated ({signals}). "
        f"Payout conditions likely met. Initiate claims verification process."
    )


# ---------------------------------------------------------------------------
# 7. Audience presentation layer
# ---------------------------------------------------------------------------

def format_for_audience(report: InsuranceReport, audience: str) -> str:
    """Format the same report for different audiences."""
    if audience == "farmer":
        return _format_farmer(report)
    if audience == "insurance":
        return _format_insurance(report)
    if audience == "agronomist":
        return _format_agronomist(report)
    if audience == "scientist":
        return _format_scientist(report)
    return _format_insurance(report)


def _format_farmer(r: InsuranceReport) -> str:
    """WhatsApp-ready, <200 chars per section, clear and simple."""
    status_emoji = {"SAFE": "✅", "WATCH": "👀", "WARNING": "⚠️", "PAYOUT_LIKELY": "🚨"}.get(
        r.overall_status, "❓"
    )
    status_word = {
        "SAFE": "SAFE", "WATCH": "NEEDS WATCHING",
        "WARNING": "AT RISK", "PAYOUT_LIKELY": "INSURANCE MAY PAY",
    }.get(r.overall_status, "UNKNOWN")

    lines = [
        f"{status_emoji} Your {r.crop} in {r.location_name} is {status_word}.",
        f"Rain this season: {r.season_rainfall_mm:.0f}mm",
    ]
    if r.max_dry_spell_days > 0:
        lines.append(f"Longest dry spell: {r.max_dry_spell_days} days")
    if r.ndvi_z_score is not None:
        health = "healthy" if r.ndvi_z_score > -0.5 else "stressed" if r.ndvi_z_score > -1.5 else "very stressed"
        lines.append(f"Vegetation: {health}")
    if r.drought_diagnostic and r.drought_diagnostic not in ("normal", "insufficient_data"):
        labels = {"consistent_drought": "Drought confirmed", "flash_drought": "Flash drought risk",
                  "carryover_storage": "Soil still has moisture", "runoff_dominated": "Rain running off"}
        lines.append(labels.get(r.drought_diagnostic, r.drought_diagnostic))

    activated = [t for t in r.triggers if t.triggered]
    if not activated:
        lines.append("No drought trigger activated.")
    else:
        lines.append(f"{len(activated)} trigger(s) activated — contact your insurance agent.")

    if r.forecast_outlook:
        fo = r.forecast_outlook
        risk = fo["rainfall_trigger_risk"]
        prob = int(fo["rainfall_trigger_probability"] * 100)
        projected = fo["projected_season_total_mm"]
        if risk in ("HIGH", "VERY HIGH"):
            lines.append(f"Forecast: {prob}% chance of drought trigger by harvest ({projected:.0f}mm projected)")
        elif risk == "MODERATE":
            lines.append(f"Forecast: rain outlook moderate — {projected:.0f}mm projected by harvest")
        else:
            lines.append(f"Forecast: rain on track — {projected:.0f}mm projected by harvest")

    lines.append(f"Growth stage: {r.growth_phase} (day {r.days_after_planting})")
    return "\n".join(lines)


def _format_insurance(r: InsuranceReport) -> str:
    """Trigger assessment table for insurance workers."""
    header = (
        f"TRIGGER ASSESSMENT: {r.location_name} — {r.crop.title()} Season {r.season} "
        f"({r.period_start} to {r.period_end})"
    )

    activated = sum(1 for t in r.triggers if t.triggered)
    status_line = (
        f"Status: {r.overall_status} | "
        f"Triggers: {activated}/{r.triggers_total} activated | "
        f"Confidence: {r.confidence_score}/100"
    )

    rows = []
    for t in r.triggers:
        status = "TRIGGERED" if t.triggered else "PASS"
        # Show the trigger condition: "below" means payout if current < threshold
        if t.direction == "below":
            op = "<"
        else:
            op = ">"
        rows.append(
            f"  {t.signal:<22s} {t.current_value:>8.1f}  {op}{t.threshold:<8.1f}  "
            f"{status:<10s} {t.weight:.1f}"
        )

    table = "\n".join([
        f"  {'Signal':<22s} {'Current':>8s}  {'Threshold':<9s}  {'Status':<10s} {'Weight'}",
        "  " + "-" * 65,
        *rows,
    ])

    sources = ", ".join(r.sources) if r.sources else "CHIRPS, Sentinel-1/2, WaPOR"
    phase_info = f"Phase: {r.growth_phase} (day {r.days_after_planting} of {_get_harvest_dap(r.crop, r.season)})"

    sections = [header, status_line, "", table, ""]

    if r.forecast_outlook:
        fo = r.forecast_outlook
        sections.append("FORECAST OUTLOOK (bias-corrected multi-model):")
        sections.append(f"  Projected season total: {fo['projected_season_total_mm']:.0f}mm "
                        f"(range {fo['projected_season_p10_mm']:.0f}–{fo['projected_season_p90_mm']:.0f}mm)")
        sections.append(f"  Rainfall trigger threshold: {fo['rainfall_trigger_threshold_mm']:.0f}mm")
        sections.append(f"  Trigger probability: {int(fo['rainfall_trigger_probability'] * 100)}% — {fo['rainfall_trigger_risk']}")
        sections.append(f"  Model agreement: {fo['model_agreement']} | "
                        f"Bias-corrected: {'yes' if fo['bias_corrected'] else 'no'} | "
                        f"Terrain-corrected: {'yes' if fo['terrain_corrected'] else 'no'}")
        sections.append(f"  Method: {fo['projection_method']}")
        sections.append("")

    sections.append(phase_info)
    sections.append(f"Sources: {sources}")

    return "\n".join(sections)


def _format_agronomist(r: InsuranceReport) -> str:
    """Technical detail + recommendations."""
    lines = [
        f"AGRONOMIC ASSESSMENT: {r.location_name} — {r.crop.title()} Season {r.season}",
        f"Growth phase: {r.growth_phase} (day {r.days_after_planting} of {_get_harvest_dap(r.crop, r.season)})",
        "",
        "RAINFALL:",
        f"  Season cumulative: {r.season_rainfall_mm:.0f}mm",
        f"  SPI-1 (30-day): {r.spi_1:.2f}" if r.spi_1 is not None else "  SPI-1: n/a",
        f"  SPI-3 (90-day): {r.spi_3:.2f}" if r.spi_3 is not None else "  SPI-3: n/a",
    ]

    for p in r.phase_rainfall:
        lines.append(f"  {p.phase:<12s}: {p.cumulative_mm:.0f}mm over {p.day_count} days ({p.daily_avg_mm:.1f}mm/day)")

    if r.max_dry_spell_days > 0:
        lines.append(f"  Max dry spell: {r.max_dry_spell_days} days")
    if r.active_dry_spell_days > 0:
        lines.append(f"  Active dry spell: {r.active_dry_spell_days} days (ongoing)")

    lines.append("")
    lines.append("VEGETATION:")
    if r.ndvi_z_score is not None:
        lines.append(f"  NDVI z-score: {r.ndvi_z_score:.2f}")
    if r.ndvi_concordance_score is not None:
        lines.append(f"  Rainfall-NDVI concordance: {r.ndvi_concordance_score:.2f}")

    lines.append("")
    lines.append("WATER BALANCE:")
    if r.et_anomaly_pct is not None:
        lines.append(f"  ET anomaly: {r.et_anomaly_pct:+.1f}%")
    if r.soil_moisture_pct is not None:
        lines.append(f"  Soil moisture: {r.soil_moisture_pct:.1f}%")
    if r.drought_diagnostic and r.drought_diagnostic != "insufficient_data":
        lines.append(f"  Drought diagnostic: {r.drought_diagnostic_label}")

    if r.forecast_outlook:
        fo = r.forecast_outlook
        lines.append("")
        lines.append("FORECAST:")
        lines.append(f"  {fo['days_remaining']} days to harvest, {fo['forecast_days_available']}-day model forecast available")
        lines.append(f"  Projected season total: {fo['projected_season_total_mm']:.0f}mm "
                     f"(p10={fo['projected_season_p10_mm']:.0f}, p90={fo['projected_season_p90_mm']:.0f})")
        lines.append(f"  Rainfall trigger risk: {fo['rainfall_trigger_risk']} "
                     f"({int(fo['rainfall_trigger_probability'] * 100)}% probability)")
        if fo.get("bias_corrected"):
            lines.append("  Forecast is bias-corrected against CHIRPS/ERA5 observations")

    lines.append("")
    lines.append(f"STATUS: {r.overall_status} (confidence {r.confidence_score}/100)")
    lines.append(f"RECOMMENDATION: {r.recommendation}")

    return "\n".join(lines)


def _format_scientist(r: InsuranceReport) -> str:
    """Full JSON with methodology and provenance — returned as formatted string."""
    data = r.to_dict()
    data["methodology"] = {
        "rainfall": "CHIRPS v2.0 daily precipitation, 0.05° resolution",
        "spi": "SPI-1 (30-day) and SPI-3 (90-day) from daily CHIRPS against per-district monthly normals (CHIRPS 2000-2023). Z-score approximation; gamma fit deferred.",
        "drought_diagnostic": "SPI-SM divergence classification: consistent_drought (SPI<-1, SM<35%), flash_drought (SPI normal, SM<35%), carryover_storage (SPI<-1, SM>=35%), runoff_dominated (SPI>1, SM<35%)",
        "ndvi": "Sentinel-2 NDVI with SAR fallback (cloud-penetrating) anomaly z-scores",
        "sar_backscatter": "Sentinel-1 C-band SAR VH/VV ratio, cloud-penetrating vegetation density",
        "ndvi_concordance": "Rainfall deficit vs NDVI response lag analysis",
        "et": "WaPOR v3 AETI dekadal, 100m resolution",
        "soil_moisture": "WaPOR v3 relative soil moisture, dekadal",
        "dry_spells": "Consecutive days < 2mm threshold from CHIRPS daily",
        "triggers": "Parametric thresholds from insurance_triggers table",
        "confidence": "Weighted composite: passing_weight / total_weight * 100",
    }
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# 8a. Multi-area comparison mode
# ---------------------------------------------------------------------------

_COMPARE_HIERARCHY = {
    "district": {
        "child_table": "rwanda_sector_boundaries",
        "child_col": "sector_name",
        "parent_col": "district_name",
    },
    "sector": {
        "child_table": "rwanda_sector_boundaries",
        "child_col": "sector_name",
        "parent_col": "district_name",
    },
    "cell": {
        "child_table": "rwanda_cell_boundaries",
        "child_col": "cell_name",
        "parent_col": "sector_name",
        "grandparent_col": "district_name",
    },
}

_COMPARE_SEMAPHORE = asyncio.Semaphore(6)


async def _fetch_area_signals(
    lat: float, lon: float,
    planting_date: date,
    today: date,
    season: str,
    district: Optional[str],
) -> dict[str, Any]:
    """Fetch all signals for a single centroid. Lightweight — no DB, no triggers."""
    signals: dict[str, Any] = {}

    async def _chirps():
        """Fetch CHIRPS daily precip for the last 90 days (for SPI-3) plus
        sparse season samples (for cumulative totals).  Prioritizes the most
        recent 90 days to support accurate SPI-1 and SPI-3 computation."""
        try:
            from src.services.forecast_fusion import _fetch_chirps_precip
            spi_window_start = today - timedelta(days=89)
            fetch_start = min(planting_date, spi_window_start)

            all_dates: list[str] = []
            d = fetch_start
            while d <= today:
                all_dates.append(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)
            if not all_dates:
                return {}

            # The last 90 days all get fetched (SPI-1 and SPI-3 need them).
            # CHIRPS 404s on dates within its ~30-day lag are fast/free.
            # Earlier season days get sparse sampling for cumulative totals.
            spi_start_str = spi_window_start.strftime("%Y-%m-%d")
            recent = [d for d in all_dates if d >= spi_start_str]
            earlier = [d for d in all_dates if d < spi_start_str]

            max_total = 90
            recent_budget = min(len(recent), max_total)
            earlier_budget = max(0, max_total - recent_budget)

            dates_to_fetch = list(recent)
            if earlier and earlier_budget > 0:
                step = max(1, len(earlier) / earlier_budget)
                for i in range(min(earlier_budget, len(earlier))):
                    dates_to_fetch.append(earlier[int(i * step)])

            dates_to_fetch.sort()
            return await asyncio.to_thread(_fetch_chirps_precip, lat, lon, dates_to_fetch)
        except Exception:
            return {}

    async def _wapor_et():
        try:
            from src.services.wapor_service import query_et
            return await asyncio.to_thread(query_et, lat, lon, planting_date, today)
        except Exception:
            return None

    async def _wapor_soil():
        try:
            from src.services.wapor_service import query_soil_moisture
            return await asyncio.to_thread(query_soil_moisture, lat, lon, planting_date, today)
        except Exception:
            return None

    async def _sar():
        try:
            return await _fetch_sar_backscatter(
                lat, lon,
                planting_date.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"),
            )
        except Exception:
            return None

    async def _weather():
        try:
            from src.services.forecast_fusion import _fetch_observed
            return await asyncio.to_thread(_fetch_observed, lat, lon, 10)
        except Exception:
            return None

    async with _COMPARE_SEMAPHORE:
        results = await asyncio.gather(
            _chirps(), _wapor_et(), _wapor_soil(), _sar(), _weather(),
            return_exceptions=True,
        )

    chirps_daily = results[0] if not isinstance(results[0], BaseException) else {}
    et_result = results[1] if not isinstance(results[1], BaseException) else None
    soil_result = results[2] if not isinstance(results[2], BaseException) else None
    sar_result = results[3] if not isinstance(results[3], BaseException) else None
    weather_result = results[4] if not isinstance(results[4], BaseException) else None

    # Rainfall + SPI-1 + SPI-3
    if chirps_daily:
        season_rain = sum(v for v in chirps_daily.values() if v is not None)
        signals["rainfall_mm"] = round(season_rain, 1)
        dates_with_data = sorted(k for k, v in chirps_daily.items() if v is not None)
        spi_ref = date.fromisoformat(dates_with_data[-1]) if dates_with_data else today
        spi_pair = _compute_spi_pair(chirps_daily, spi_ref, district)
        if spi_pair["spi_1"] is not None:
            signals["spi_1"] = round(spi_pair["spi_1"], 2)
        if spi_pair["spi_3"] is not None:
            signals["spi_3"] = round(spi_pair["spi_3"], 2)
        rain_days = [v for v in chirps_daily.values() if v is not None]
        if rain_days:
            consecutive_dry = 0
            max_dry = 0
            for v in rain_days:
                if v < 2.0:
                    consecutive_dry += 1
                    max_dry = max(max_dry, consecutive_dry)
                else:
                    consecutive_dry = 0
            signals["max_dry_spell_days"] = max_dry

    # ET anomaly
    if et_result and isinstance(et_result, dict) and et_result.get("status") == "success":
        series = et_result.get("time_series", [])
        values = [s.get("et_mm_per_day") for s in series if s.get("et_mm_per_day") is not None]
        if values:
            mean_et = sum(values) / len(values)
            signals["et_anomaly_pct"] = round(((mean_et - _ET_LONG_TERM_MEAN) / _ET_LONG_TERM_MEAN) * 100, 1)

    # Soil moisture
    if soil_result and isinstance(soil_result, dict) and soil_result.get("status") == "success":
        series = soil_result.get("time_series", [])
        values = [s.get("relative_soil_moisture_pct") for s in series if s.get("relative_soil_moisture_pct") is not None]
        if values:
            signals["soil_moisture_pct"] = round(values[-1], 1)

    # SPI-SM drought diagnostic
    sm_val = signals.get("soil_moisture_pct")
    spi3_val = signals.get("spi_3")
    drought_state = _classify_drought_state(spi3_val, sm_val)
    signals["drought_diagnostic"] = drought_state
    signals["drought_diagnostic_label"] = _DROUGHT_STATE_LABELS.get(drought_state, "")

    # SAR backscatter
    if isinstance(sar_result, (int, float)):
        signals["sar_vh_vv_ratio"] = round(float(sar_result), 3)

    # Weather (temperature, precipitation from recent observations)
    if weather_result and isinstance(weather_result, dict):
        t_max = weather_result.get("temperature_max", [])
        t_min = weather_result.get("temperature_min", [])
        precip = weather_result.get("precipitation_mm", [])
        if t_max:
            valid = [v for v in t_max if v is not None]
            if valid:
                signals["temperature_max_c"] = round(max(valid), 1)
        if t_min:
            valid = [v for v in t_min if v is not None]
            if valid:
                signals["temperature_min_c"] = round(min(valid), 1)
        if t_max and t_min:
            valid_max = [v for v in t_max if v is not None]
            valid_min = [v for v in t_min if v is not None]
            if valid_max and valid_min:
                signals["temperature_mean_c"] = round(
                    (sum(valid_max) / len(valid_max) + sum(valid_min) / len(valid_min)) / 2, 1
                )
        if precip:
            valid = [v for v in precip if v is not None]
            if valid:
                signals["recent_precip_mm_day"] = round(sum(valid) / len(valid), 1)

    return signals


async def _compare_areas(
    conn: asyncpg.Connection,
    crop: str = "maize",
    season: Optional[str] = None,
    district: Optional[str] = None,
    sector: Optional[str] = None,
    cell: Optional[str] = None,
    compare_level: str = "sector",
    ref_date: Optional[date] = None,
) -> dict[str, Any]:
    """Compare all child areas at compare_level within the parent area.

    Example: district=Nyamasheke, compare_level=sector → compares all sectors.
    """
    from src.services.dssat_service import detect_current_season

    today = ref_date or date.today()
    crop = crop.lower().strip()
    if crop not in _GROWTH_PHASES:
        crop = _default_crop_for_district(district)
    if season is None:
        season = detect_current_season(crop, datetime(today.year, today.month, today.day))

    compare_level = compare_level.lower().strip()

    # Discover child areas with centroids
    if compare_level == "sector" and district:
        rows = await conn.fetch(
            "SELECT sector_name AS name, district_name, "
            "round(ST_Y(ST_Centroid(geom))::numeric, 5) AS lat, "
            "round(ST_X(ST_Centroid(geom))::numeric, 5) AS lon "
            "FROM rwanda_sector_boundaries WHERE LOWER(district_name) = LOWER($1) "
            "ORDER BY sector_name",
            district,
        )
        parent_name = district
        parent_level = "district"
    elif compare_level == "cell" and (sector or district):
        if sector:
            rows = await conn.fetch(
                "SELECT cell_name AS name, district_name, "
                "round(ST_Y(ST_Centroid(geom))::numeric, 5) AS lat, "
                "round(ST_X(ST_Centroid(geom))::numeric, 5) AS lon "
                "FROM rwanda_cell_boundaries WHERE LOWER(sector_name) = LOWER($1) "
                "ORDER BY cell_name",
                sector,
            )
            parent_name = sector
            parent_level = "sector"
        else:
            rows = await conn.fetch(
                "SELECT cell_name AS name, district_name, "
                "round(ST_Y(ST_Centroid(geom))::numeric, 5) AS lat, "
                "round(ST_X(ST_Centroid(geom))::numeric, 5) AS lon "
                "FROM rwanda_cell_boundaries WHERE LOWER(district_name) = LOWER($1) "
                "ORDER BY cell_name",
                district,
            )
            parent_name = district
            parent_level = "district"
    elif compare_level == "district":
        rows = await conn.fetch(
            "SELECT district AS name, district AS district_name, "
            "round(ST_Y(ST_Centroid(geom))::numeric, 5) AS lat, "
            "round(ST_X(ST_Centroid(geom))::numeric, 5) AS lon "
            "FROM rwanda_district_boundaries ORDER BY district",
        )
        parent_name = "Rwanda"
        parent_level = "country"
    else:
        return {
            "status": "error",
            "error": (
                f"compare_level='{compare_level}' requires a parent area. "
                "Use district= for sector comparison, sector= for cell comparison, "
                "or compare_level='district' to compare all districts."
            ),
        }

    if not rows:
        return {
            "status": "error",
            "error": f"No {compare_level}s found in {parent_name}.",
        }

    # Resolve planting date
    planting_year = today.year if season == "B" or today.month >= 9 else today.year - 1
    if season == "A" and today.month <= 2:
        planting_year = today.year - 1
    planting_date = _get_planting_date(crop, season, planting_year)
    dap = (today - planting_date).days
    if dap < 0:
        planting_year -= 1
        planting_date = _get_planting_date(crop, season, planting_year)
        dap = (today - planting_date).days
    harvest_dap = _get_harvest_dap(crop, season)
    dap = max(0, min(dap, harvest_dap + 30))
    growth_phase = _current_growth_phase(crop, dap)

    # Also get NDVI from cache (fast, already aggregated)
    ndvi_by_area: dict[str, float] = {}
    try:
        if compare_level == "sector" and district:
            ndvi_rows = await conn.fetch(
                "SELECT cb.sector_name AS area_name, AVG(nc.mean_ndvi) AS avg_ndvi "
                "FROM ndvi_cell_cache nc "
                "JOIN rwanda_cell_boundaries cb ON nc.cell_name = cb.cell_name AND nc.district_name = cb.district_name "
                "WHERE LOWER(nc.district_name) = LOWER($1) "
                "AND nc.computed_at > NOW() - INTERVAL '30 days' "
                "GROUP BY cb.sector_name",
                district,
            )
        elif compare_level == "cell":
            filter_col = "sector_name" if sector else "district_name"
            filter_val = sector or district
            ndvi_rows = await conn.fetch(
                f"SELECT nc.cell_name AS area_name, AVG(nc.mean_ndvi) AS avg_ndvi "
                f"FROM ndvi_cell_cache nc "
                f"JOIN rwanda_cell_boundaries cb ON nc.cell_name = cb.cell_name AND nc.district_name = cb.district_name "
                f"WHERE LOWER(cb.{filter_col}) = LOWER($1) "
                f"AND nc.computed_at > NOW() - INTERVAL '30 days' "
                f"GROUP BY nc.cell_name",
                filter_val,
            )
        elif compare_level == "district":
            ndvi_rows = await conn.fetch(
                "SELECT nc.district_name AS area_name, AVG(nc.mean_ndvi) AS avg_ndvi "
                "FROM ndvi_cell_cache nc "
                "WHERE nc.computed_at > NOW() - INTERVAL '30 days' "
                "GROUP BY nc.district_name",
            )
        else:
            ndvi_rows = []
        for nr in ndvi_rows:
            ndvi_by_area[nr["area_name"].lower()] = round(float(nr["avg_ndvi"]), 4)
    except Exception:
        logger.debug("NDVI cache lookup for comparison failed", exc_info=True)

    # Fetch all signals in parallel for each area
    async def _fetch_one(row: asyncpg.Record) -> dict[str, Any]:
        lat = float(row["lat"])
        lon = float(row["lon"])
        name = row["name"]
        d_name = row["district_name"] if "district_name" in row.keys() else district
        signals = await _fetch_area_signals(
            lat, lon, planting_date, today, season, district=d_name,
        )
        # Merge cached NDVI
        ndvi = ndvi_by_area.get(name.lower())
        if ndvi is not None:
            signals["ndvi"] = ndvi
        signals["name"] = name
        signals["lat"] = lat
        signals["lon"] = lon
        return signals

    area_results = await asyncio.gather(
        *[_fetch_one(r) for r in rows],
        return_exceptions=True,
    )

    comparison = []
    for r in area_results:
        if isinstance(r, BaseException):
            logger.debug("Compare area fetch failed: %s", r)
            continue
        comparison.append(r)

    # Sort by rainfall descending (most intuitive default for "who gets more rain")
    comparison.sort(key=lambda x: x.get("rainfall_mm", 0), reverse=True)

    # Collect which signals are present across all areas
    all_signals = set()
    for c in comparison:
        all_signals.update(k for k in c if k not in ("name", "lat", "lon"))

    return {
        "status": "ok",
        "mode": "comparison",
        "parent": parent_name,
        "parent_level": parent_level,
        "compare_level": compare_level,
        "crop": crop,
        "season": season,
        "growth_phase": growth_phase,
        "days_after_planting": dap,
        "period": f"{planting_date.strftime('%Y-%m-%d')} to {today.strftime('%Y-%m-%d')}",
        "area_count": len(comparison),
        "signals_available": sorted(all_signals),
        "areas": comparison,
        "sources": "CHIRPS v2.0, WaPOR v3, Sentinel-1 SAR, Sentinel-2 NDVI, Open-Meteo/ERA5",
    }


# ---------------------------------------------------------------------------
# 8b. Composite orchestrator — THE MAIN ENTRY POINT
# ---------------------------------------------------------------------------

async def compute_insurance_intelligence(
    conn: asyncpg.Connection,
    crop: str = "maize",
    season: Optional[str] = None,
    district: Optional[str] = None,
    sector: Optional[str] = None,
    cell: Optional[str] = None,
    village: Optional[str] = None,
    audience: str = "farmer",
    ref_date: Optional[date] = None,
    compare_level: Optional[str] = None,
) -> dict[str, Any]:
    """One call, all signals, any audience, any admin level.

    Returns dict with 'status', 'report' (formatted string), 'data' (raw dict),
    and 'geometry' (GeoJSON for Brain persistence).

    When compare_level is set (e.g. "sector", "cell", "district"), discovers all
    child admin units at that level within the parent area and returns a
    comparison table with all signals for each.
    """
    if compare_level:
        return await _compare_areas(
            conn, crop=crop, season=season,
            district=district, sector=sector, cell=cell,
            compare_level=compare_level, ref_date=ref_date,
        )

    from src.services.dssat_service import detect_current_season

    if not any([district, sector, cell, village]):
        return {
            "status": "error",
            "error": "At least one location parameter (district, sector, cell, or village) is required.",
        }

    today = ref_date or date.today()
    crop = crop.lower().strip()
    _original_crop = crop
    _crop_was_substituted = crop not in _GROWTH_PHASES
    if _crop_was_substituted:
        crop = _default_crop_for_district(district)
    if audience not in _VALID_AUDIENCES:
        audience = "farmer"

    if season is None:
        season = detect_current_season(crop, datetime(today.year, today.month, today.day))

    # Resolve admin level name for display
    location_name, admin_level = _resolve_location_name(district, sector, cell, village)
    if not location_name:
        return {"status": "error", "error": "Specify at least one of: district, sector, cell, or village"}

    # Determine planting date and current DAP
    planting_year = today.year if season == "B" or today.month >= 9 else today.year - 1
    if season == "A" and today.month <= 2:
        planting_year = today.year - 1

    planting_date = _get_planting_date(crop, season, planting_year)
    dap = (today - planting_date).days
    if dap < 0:
        planting_year -= 1
        planting_date = _get_planting_date(crop, season, planting_year)
        dap = (today - planting_date).days
    harvest_dap = _get_harvest_dap(crop, season)
    dap = max(0, min(dap, harvest_dap + 30))

    growth_phase = _current_growth_phase(crop, dap)

    # Get geometry and centroid for CHIRPS/WaPOR
    from src.services.admin_boundaries import lookup_admin_geometry
    geometry = await lookup_admin_geometry(
        district=district, sector=sector, cell=cell, village=village,
    )
    if geometry:
        lat, lon = _centroid_from_geojson(geometry)
    else:
        lat, lon = _RWANDA_CENTER

    # --- PARALLEL DATA FETCH ---
    # Network-only fetches (no shared conn) run in parallel.
    # DB-dependent fetches run sequentially on `conn` — asyncpg connections
    # are not safe for concurrent use (raises InterfaceError).

    async def fetch_sar_backscatter():
        return await _fetch_sar_backscatter(
            lat, lon,
            planting_date.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"),
        )

    async def fetch_chirps():
        """Fetch CHIRPS daily precip covering the last 90 days (for SPI-3)
        plus sparse season samples for cumulative totals."""
        try:
            from src.services.forecast_fusion import _fetch_chirps_precip
            spi_window_start = today - timedelta(days=89)
            fetch_start = min(planting_date, spi_window_start)

            all_dates: list[str] = []
            d = fetch_start
            while d <= today:
                all_dates.append(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)
            if not all_dates:
                return {}

            spi_start_str = spi_window_start.strftime("%Y-%m-%d")
            recent = [d for d in all_dates if d >= spi_start_str]
            earlier = [d for d in all_dates if d < spi_start_str]

            max_total = 90
            recent_budget = min(len(recent), max_total)
            earlier_budget = max(0, max_total - recent_budget)

            dates_to_fetch = list(recent[:recent_budget])
            if earlier and earlier_budget > 0:
                step = max(1, len(earlier) / earlier_budget)
                for i in range(min(earlier_budget, len(earlier))):
                    dates_to_fetch.append(earlier[int(i * step)])

            dates_to_fetch.sort()
            return await asyncio.to_thread(_fetch_chirps_precip, lat, lon, dates_to_fetch)
        except Exception:
            logger.debug("chirps fetch failed", exc_info=True)
            return {}

    async def fetch_wapor_et():
        try:
            from src.services.wapor_service import query_et
            return await asyncio.to_thread(
                query_et, lat, lon, planting_date, today,
            )
        except Exception:
            logger.debug("wapor ET fetch failed", exc_info=True)
            return None

    async def fetch_wapor_soil():
        try:
            from src.services.wapor_service import query_soil_moisture
            return await asyncio.to_thread(
                query_soil_moisture, lat, lon, planting_date, today,
            )
        except Exception:
            logger.debug("wapor soil moisture fetch failed", exc_info=True)
            return None

    async def fetch_forecast():
        try:
            from src.services.forecast_openmeteo import fetch_openmeteo_multimodel
            days_left = max(0, harvest_dap - dap)
            forecast_days = min(days_left, 16)
            if forecast_days < 1:
                return None
            return await asyncio.to_thread(
                fetch_openmeteo_multimodel, lat, lon, forecast_days,
            )
        except Exception:
            logger.debug("forecast fetch failed", exc_info=True)
            return None

    # Network-only fetches: safe to parallelize (return_exceptions prevents
    # one failure from cancelling the others)
    network_results = await asyncio.gather(
        fetch_sar_backscatter(),
        fetch_chirps(),
        fetch_wapor_et(),
        fetch_wapor_soil(),
        fetch_forecast(),
        return_exceptions=True,
    )
    sar_result = network_results[0] if not isinstance(network_results[0], BaseException) else None
    chirps_daily = network_results[1] if not isinstance(network_results[1], BaseException) else {}
    et_result = network_results[2] if not isinstance(network_results[2], BaseException) else None
    soil_result = network_results[3] if not isinstance(network_results[3], BaseException) else None
    forecast_result = network_results[4] if not isinstance(network_results[4], BaseException) else None

    # DB-dependent fetches: sequential on the shared connection
    try:
        accuracy_result = await compute_insurance_accuracy_safe(conn, district, season)
    except Exception:
        logger.debug("insurance_accuracy fetch failed", exc_info=True)
        accuracy_result = None

    try:
        from src.services.weather_accuracy import detect_dry_spells
        dry_spells_result = await detect_dry_spells(
            conn, district=district,
            date_from=planting_date.strftime("%Y-%m-%d"),
            date_to=today.strftime("%Y-%m-%d"),
        )
    except Exception:
        logger.debug("dry_spells fetch failed", exc_info=True)
        dry_spells_result = None

    try:
        from src.services.weather_accuracy import compute_ndvi_concordance
        ndvi_conc_result = await compute_ndvi_concordance(
            conn, district=district,
            date_from=planting_date.strftime("%Y-%m-%d"),
            date_to=today.strftime("%Y-%m-%d"),
        )
    except Exception:
        logger.debug("ndvi_concordance fetch failed", exc_info=True)
        ndvi_conc_result = None

    ndvi_z = await _fetch_ndvi_with_sar_fallback(
        conn, lat, lon,
        planting_date.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"),
        district,
    )

    # --- PROCESS RESULTS ---
    sources = []

    # Rainfall + SPI
    phase_rainfall = _compute_phase_rainfall(chirps_daily, planting_date, crop, today)
    season_rainfall = sum(p.cumulative_mm for p in phase_rainfall)
    spi = _compute_spi(season_rainfall, season, district=district)
    if chirps_daily:
        _dates_with_data = sorted(k for k, v in chirps_daily.items() if v is not None)
        _spi_ref = date.fromisoformat(_dates_with_data[-1]) if _dates_with_data else today
        spi_pair = _compute_spi_pair(chirps_daily, _spi_ref, district)
    else:
        spi_pair = {"spi_1": None, "spi_3": None}
    spi_1 = spi_pair["spi_1"]
    spi_3 = spi_pair["spi_3"]
    if chirps_daily:
        sources.append("CHIRPS v2.0")

    # Dry spells
    max_dry_spell = 0
    active_dry_spell = 0
    if dry_spells_result and dry_spells_result.get("status") == "success":
        max_dry_spell = dry_spells_result.get("longest_spell_days", 0)
        spells = dry_spells_result.get("dry_spells", [])
        if spells:
            last_spell = spells[-1] if isinstance(spells[-1], dict) else {}
            if last_spell.get("ongoing"):
                active_dry_spell = last_spell.get("duration_days", 0)

    # NDVI
    ndvi_concordance_score = None
    if ndvi_conc_result and ndvi_conc_result.get("status") == "success":
        ndvi_concordance_score = ndvi_conc_result.get("concordance_score")
    if ndvi_z is not None:
        sources.append("Sentinel-2/SAR NDVI")

    # SAR backscatter (VH/VV ratio) — cloud-penetrating vegetation signal
    sar_vh_vv_ratio: Optional[float] = None
    if isinstance(sar_result, (int, float)):
        sar_vh_vv_ratio = float(sar_result)
        if "Sentinel-1 SAR" not in sources:
            sources.append("Sentinel-1 SAR")

    # ET and soil moisture
    et_anomaly = None
    if et_result and et_result.get("status") == "success":
        series = et_result.get("time_series", [])
        if series:
            values = [s.get("et_mm_per_day") for s in series if s.get("et_mm_per_day") is not None]
            if values:
                mean_et = sum(values) / len(values)
                et_anomaly = ((mean_et - _ET_LONG_TERM_MEAN) / _ET_LONG_TERM_MEAN) * 100
                sources.append("WaPOR v3 ET")

    soil_moisture = None
    if soil_result and soil_result.get("status") == "success":
        series = soil_result.get("time_series", [])
        if series:
            values = [s.get("relative_soil_moisture_pct") for s in series if s.get("relative_soil_moisture_pct") is not None]
            if values:
                soil_moisture = values[-1]  # most recent
                if "WaPOR v3 ET" not in sources:
                    sources.append("WaPOR v3")

    # SPI-SM divergence diagnostic
    drought_diagnostic = _classify_drought_state(spi_3, soil_moisture)
    drought_diagnostic_label = _DROUGHT_STATE_LABELS.get(drought_diagnostic, "")

    # --- TRIGGER EVALUATION ---
    trigger_defs = await _load_triggers(conn, crop, season, growth_phase, district)

    current_values: dict[str, Optional[float]] = {
        "rainfall_cumulative": season_rainfall,
        "spi": spi,
        "dry_spell_days": float(max_dry_spell),
        "ndvi_z_score": ndvi_z,
        "sar_backscatter": sar_vh_vv_ratio,
        "et_anomaly": et_anomaly,
        "soil_moisture": soil_moisture,
    }

    trigger_results = _evaluate_triggers(trigger_defs, current_values)
    triggers_activated = sum(1 for t in trigger_results if t.triggered)
    confidence_score, overall_status = _compute_confidence(
        trigger_results, expected_signals=len(trigger_defs),
    )
    recommendation = _generate_recommendation(overall_status, crop, growth_phase, trigger_results)

    # Merge with existing accuracy components if available
    accuracy_components = None
    if accuracy_result and accuracy_result.get("status") == "success":
        binary = (accuracy_result.get("components") or {}).get(
            "binary_accuracy", {}
        ).get("overall_binary", {})
        accuracy_components = {
            "confidence_rating": accuracy_result.get("confidence_rating"),
            "recommendation": accuracy_result.get("recommendation"),
            "pod": binary.get("pod"),
            "far": binary.get("far"),
            "hss": binary.get("hss"),
            "csi": binary.get("csi"),
        }

    # --- FORECAST OUTLOOK ---
    logger.info(
        "Forecast outlook input: forecast_result=%s, season_rainfall=%.1f, "
        "planting_date=%s, harvest_dap=%d, today=%s, crop=%s",
        type(forecast_result).__name__ if forecast_result else "None",
        season_rainfall,
        planting_date,
        harvest_dap,
        today,
        crop,
    )
    if forecast_result:
        fd = forecast_result.get("daily", [])
        logger.info(
            "Forecast data: %d daily entries, keys=%s, first_day_keys=%s",
            len(fd),
            list(forecast_result.keys())[:6],
            list(fd[0].keys()) if fd else "empty",
        )
    forecast_outlook = _compute_forecast_outlook(
        forecast_result, season_rainfall, planting_date, harvest_dap,
        today, crop, season, district,
    )
    logger.info("Forecast outlook result: %s", forecast_outlook)
    if forecast_outlook:
        sources.append(f"Multi-model forecast ({', '.join(forecast_outlook.get('models_used', []))})")

    # --- BUILD REPORT ---
    report = InsuranceReport(
        location_name=location_name,
        admin_level=admin_level,
        crop=crop,
        season=season,
        growth_phase=growth_phase,
        days_after_planting=dap,
        phase_rainfall=phase_rainfall,
        season_rainfall_mm=season_rainfall,
        spi=spi,
        spi_1=spi_1,
        spi_3=spi_3,
        drought_diagnostic=drought_diagnostic,
        drought_diagnostic_label=drought_diagnostic_label,
        ndvi_z_score=ndvi_z,
        ndvi_concordance_score=ndvi_concordance_score,
        et_anomaly_pct=et_anomaly,
        soil_moisture_pct=soil_moisture,
        max_dry_spell_days=max_dry_spell,
        active_dry_spell_days=active_dry_spell,
        triggers=trigger_results,
        triggers_activated=triggers_activated,
        triggers_total=len(trigger_results),
        confidence_score=confidence_score,
        overall_status=overall_status,
        recommendation=recommendation,
        accuracy_components=accuracy_components,
        forecast_outlook=forecast_outlook,
        sources=sources,
        period_start=planting_date.strftime("%Y-%m-%d"),
        period_end=today.strftime("%Y-%m-%d"),
        computed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        geometry=geometry,
    )

    formatted = format_for_audience(report, audience)

    result: dict[str, Any] = {
        "status": "ok",
        "report": formatted,
        "data": report.to_dict(),
        "audience": audience,
        "geometry": geometry,
        "slug": f"insurance-{crop}-{location_name.lower().replace(' ', '-')}-{season}-{today.strftime('%Y%m%d')}",
    }
    if _crop_was_substituted:
        result["crop_warning"] = (
            f"'{_original_crop}' is not in the supported crop list. "
            f"Used maize growth phases as fallback. "
            f"Supported crops: {', '.join(sorted(_GROWTH_PHASES.keys()))}"
        )
    return result


async def compute_insurance_accuracy_safe(
    conn: asyncpg.Connection,
    district: Optional[str],
    season: Optional[str],
) -> Optional[dict]:
    """Safe wrapper around existing compute_insurance_accuracy."""
    try:
        from src.services.weather_accuracy import compute_insurance_accuracy
        return await compute_insurance_accuracy(conn, district=district, season=season)
    except Exception:
        logger.debug("compute_insurance_accuracy failed", exc_info=True)
        return None


def _resolve_location_name(
    district: Optional[str] = None,
    sector: Optional[str] = None,
    cell: Optional[str] = None,
    village: Optional[str] = None,
) -> tuple[str, str]:
    """Return (display_name, admin_level) from the most specific provided."""
    if village:
        return village.strip(), "village"
    if cell:
        return cell.strip(), "cell"
    if sector:
        return sector.strip(), "sector"
    if district:
        return district.strip(), "district"
    return "", ""
