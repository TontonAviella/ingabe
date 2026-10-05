from datetime import datetime

from src.services.crop_calendar import CROP_CALENDARS, detect_current_season


def test_crop_calendar_season_a():
    """Maize Season A → planting Sep 15, harvest at 120 DAP."""
    cal = CROP_CALENDARS["maize"]["A"]
    assert cal["planting"] == "09-15"
    assert cal["harvest_dap"] == 120


def test_crop_calendar_auto_detect_season_b():
    """Date in March → auto-selects Season B."""
    assert detect_current_season("maize", ref_date=datetime(2024, 3, 15)) == "B"


def test_crop_calendar_auto_detect_season_a():
    """Date in October → auto-selects Season A."""
    assert detect_current_season("maize", ref_date=datetime(2024, 10, 15)) == "A"


def test_crop_calendar_wheat_only_a():
    """Wheat only has Season A."""
    assert detect_current_season("wheat", ref_date=datetime(2024, 3, 15)) == "A"


def test_unknown_crop_falls_back_to_maize_calendar():
    assert detect_current_season("unknown-crop", ref_date=datetime(2024, 3, 15)) == "B"
