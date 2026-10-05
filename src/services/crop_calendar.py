"""Rwanda crop calendar (RAB — Rwanda Agriculture Board).

Planting date and days-to-harvest per crop and season, and the current-season
lookup. Season A runs Sep-Feb, Season B Feb-Jul; some crops (wheat and the
perennials) have only Season A.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

CROP_CALENDARS: Dict[str, Dict[str, Dict[str, Any]]] = {
    # --- Cereals ---
    "maize": {
        "A": {"planting": "09-15", "harvest_dap": 120},
        "B": {"planting": "02-15", "harvest_dap": 120},
    },
    "rice": {
        "A": {"planting": "09-01", "harvest_dap": 150},
        "B": {"planting": "03-01", "harvest_dap": 150},
    },
    "beans": {
        "A": {"planting": "09-15", "harvest_dap": 90},
        "B": {"planting": "02-15", "harvest_dap": 90},
    },
    "sorghum": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
    "wheat": {
        "A": {"planting": "06-01", "harvest_dap": 120},
    },
    "finger_millet": {
        "A": {"planting": "09-15", "harvest_dap": 105},
        "B": {"planting": "02-15", "harvest_dap": 105},
    },
    # --- Tubers & roots ---
    "potato": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
    "sweet_potato": {
        "A": {"planting": "09-15", "harvest_dap": 150},
        "B": {"planting": "02-15", "harvest_dap": 150},
    },
    "cassava": {
        "A": {"planting": "09-01", "harvest_dap": 365},
        "B": {"planting": "02-15", "harvest_dap": 365},
    },
    "yam": {
        "A": {"planting": "09-01", "harvest_dap": 270},
        "B": {"planting": "02-15", "harvest_dap": 270},
    },
    "taro": {
        "A": {"planting": "09-01", "harvest_dap": 270},
        "B": {"planting": "02-15", "harvest_dap": 270},
    },
    # --- Legumes ---
    "soybean": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
    "groundnut": {
        "A": {"planting": "09-15", "harvest_dap": 120},
        "B": {"planting": "02-15", "harvest_dap": 120},
    },
    "peas": {
        "A": {"planting": "09-15", "harvest_dap": 85},
        "B": {"planting": "02-15", "harvest_dap": 85},
    },
    "cowpea": {
        "A": {"planting": "09-15", "harvest_dap": 80},
        "B": {"planting": "02-15", "harvest_dap": 80},
    },
    "pigeon_pea": {
        "A": {"planting": "09-15", "harvest_dap": 170},
        "B": {"planting": "02-15", "harvest_dap": 170},
    },
    # --- Vegetables ---
    "tomato": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
    "onion": {
        "A": {"planting": "09-15", "harvest_dap": 130},
        "B": {"planting": "02-15", "harvest_dap": 130},
    },
    "cabbage": {
        "A": {"planting": "09-15", "harvest_dap": 95},
        "B": {"planting": "02-15", "harvest_dap": 95},
    },
    "carrot": {
        "A": {"planting": "09-15", "harvest_dap": 100},
        "B": {"planting": "02-15", "harvest_dap": 100},
    },
    "chili": {
        "A": {"planting": "09-15", "harvest_dap": 130},
        "B": {"planting": "02-15", "harvest_dap": 130},
    },
    "eggplant": {
        "A": {"planting": "09-15", "harvest_dap": 130},
        "B": {"planting": "02-15", "harvest_dap": 130},
    },
    "green_pepper": {
        "A": {"planting": "09-15", "harvest_dap": 120},
        "B": {"planting": "02-15", "harvest_dap": 120},
    },
    "garlic": {
        "A": {"planting": "09-15", "harvest_dap": 150},
        "B": {"planting": "02-15", "harvest_dap": 150},
    },
    "amaranth": {
        "A": {"planting": "09-15", "harvest_dap": 75},
        "B": {"planting": "02-15", "harvest_dap": 75},
    },
    "leek": {
        "A": {"planting": "09-15", "harvest_dap": 150},
        "B": {"planting": "02-15", "harvest_dap": 150},
    },
    "lettuce": {
        "A": {"planting": "09-15", "harvest_dap": 60},
        "B": {"planting": "02-15", "harvest_dap": 60},
    },
    "spinach": {
        "A": {"planting": "09-15", "harvest_dap": 55},
        "B": {"planting": "02-15", "harvest_dap": 55},
    },
    "cucumber": {
        "A": {"planting": "09-15", "harvest_dap": 70},
        "B": {"planting": "02-15", "harvest_dap": 70},
    },
    "watermelon": {
        "A": {"planting": "09-15", "harvest_dap": 90},
        "B": {"planting": "02-15", "harvest_dap": 90},
    },
    "pumpkin": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
    # --- Fruits (perennial — use Season A for main growing/fruiting cycle) ---
    "banana": {
        "A": {"planting": "09-01", "harvest_dap": 365},
        "B": {"planting": "02-15", "harvest_dap": 365},
    },
    "avocado": {
        "A": {"planting": "09-01", "harvest_dap": 730},
    },
    "mango": {
        "A": {"planting": "09-01", "harvest_dap": 545},
    },
    "passion_fruit": {
        "A": {"planting": "09-01", "harvest_dap": 270},
        "B": {"planting": "02-15", "harvest_dap": 270},
    },
    "pineapple": {
        "A": {"planting": "09-01", "harvest_dap": 540},
    },
    "papaya": {
        "A": {"planting": "09-01", "harvest_dap": 330},
        "B": {"planting": "02-15", "harvest_dap": 330},
    },
    "citrus": {
        "A": {"planting": "09-01", "harvest_dap": 600},
    },
    "strawberry": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
    "tree_tomato": {
        "A": {"planting": "09-01", "harvest_dap": 365},
        "B": {"planting": "02-15", "harvest_dap": 365},
    },
    "guava": {
        "A": {"planting": "09-01", "harvest_dap": 480},
    },
    "cape_gooseberry": {
        "A": {"planting": "09-15", "harvest_dap": 150},
        "B": {"planting": "02-15", "harvest_dap": 150},
    },
    # --- Cash & industrial crops ---
    "coffee": {
        "A": {"planting": "09-01", "harvest_dap": 640},
    },
    "tea": {
        "A": {"planting": "09-01", "harvest_dap": 730},
    },
    "sugarcane": {
        "A": {"planting": "09-01", "harvest_dap": 420},
        "B": {"planting": "02-15", "harvest_dap": 420},
    },
    "pyrethrum": {
        "A": {"planting": "09-15", "harvest_dap": 210},
        "B": {"planting": "02-15", "harvest_dap": 210},
    },
    "tobacco": {
        "A": {"planting": "09-15", "harvest_dap": 120},
        "B": {"planting": "02-15", "harvest_dap": 120},
    },
    "sunflower": {
        "A": {"planting": "09-15", "harvest_dap": 105},
        "B": {"planting": "02-15", "harvest_dap": 105},
    },
    "macadamia": {
        "A": {"planting": "09-01", "harvest_dap": 730},
    },
    "sesame": {
        "A": {"planting": "09-15", "harvest_dap": 95},
        "B": {"planting": "02-15", "harvest_dap": 95},
    },
    # --- Oil crops ---
    "oil_palm": {
        "A": {"planting": "09-01", "harvest_dap": 730},
    },
    "soya": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
}


def detect_current_season(crop_type: str = "maize", ref_date: Optional[datetime] = None) -> str:
    """Auto-detect the current growing season from the date.

    Season A: Sep-Feb (planting Sep)
    Season B: Feb-Jul (planting Feb)

    Returns "A" or "B".
    """
    if ref_date is None:
        ref_date = datetime.utcnow()

    month = ref_date.month

    calendar = CROP_CALENDARS.get(crop_type, CROP_CALENDARS["maize"])

    # Wheat only has Season A
    if "B" not in calendar:
        return "A"

    # Season A: Sep(9) through Feb(2)
    if month >= 9 or month <= 2:
        return "A"
    # Season B: Mar(3) through Aug(8)
    return "B"
