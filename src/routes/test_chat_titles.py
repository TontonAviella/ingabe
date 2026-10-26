"""Chat names (Roger, 2026-10-26: every chat read "title pending"): Sage's answer is cleaned into a list name."""

import pytest

from src.routes.message_routes import _clean_title


@pytest.mark.parametrize("raw, name", [
    ("Rainfall deficit for Gatsibo maize", "Rainfall deficit for Gatsibo maize"),
    ('"Soil phosphorus at the farm."', "Soil phosphorus at the farm"),
    ("**Counting houses  on the\northophoto**", "Counting houses on the orthophoto"),
    ("  ", ""),
    ("x" * 200, "x" * 80),
])
def test_a_chat_name_is_one_clean_line(raw, name):
    assert _clean_title(raw) == name
