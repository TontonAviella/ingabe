from decimal import Decimal

from src.services.numbers import round_or_none


def test_zero_is_a_value():
    assert round_or_none(0, 4) == 0.0
    assert round_or_none(0.0) == 0.0


def test_none_is_missing():
    assert round_or_none(None, 2) is None


def test_rounds_and_converts_decimals_to_float():
    assert round_or_none(Decimal("0.123456"), 4) == 0.1235
    assert isinstance(round_or_none(Decimal("1.5"), 1), float)
