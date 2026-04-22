"""Tests for rwanda_lakehouse input validation (SQL injection defense)."""

import pytest
from fastapi import HTTPException

from src.services.rwanda_lakehouse import (
    _RE_ALPHA_LABEL,
    _RE_DATE,
    _RE_H3_INDEX,
    _RE_IDENTIFIER,
    _safe_str,
)


class TestSafeStrH3Index:
    def test_valid_15_char_hex(self):
        assert _safe_str("8a2a1072b59ffff", _RE_H3_INDEX, "h3_index") == "8a2a1072b59ffff"

    def test_valid_uppercase_hex(self):
        assert _safe_str("8A2A1072B59FFFF", _RE_H3_INDEX, "h3_index") == "8A2A1072B59FFFF"

    def test_rejects_sql_injection(self):
        with pytest.raises(HTTPException) as exc:
            _safe_str("'; DROP TABLE --", _RE_H3_INDEX, "h3_index")
        assert exc.value.status_code == 400

    def test_rejects_empty(self):
        with pytest.raises(HTTPException):
            _safe_str("", _RE_H3_INDEX, "h3_index")

    def test_rejects_wrong_length(self):
        with pytest.raises(HTTPException):
            _safe_str("abcdef", _RE_H3_INDEX, "h3_index")

    def test_rejects_non_hex(self):
        with pytest.raises(HTTPException):
            _safe_str("8a2a1072b59ggg!", _RE_H3_INDEX, "h3_index")


class TestSafeStrIdentifier:
    def test_valid_alphanumeric(self):
        assert _safe_str("parcel-123_abc", _RE_IDENTIFIER, "parcel_id") == "parcel-123_abc"

    def test_rejects_sql_injection(self):
        with pytest.raises(HTTPException) as exc:
            _safe_str("x' OR '1'='1", _RE_IDENTIFIER, "parcel_id")
        assert exc.value.status_code == 400

    def test_rejects_empty(self):
        with pytest.raises(HTTPException):
            _safe_str("", _RE_IDENTIFIER, "parcel_id")

    def test_rejects_spaces(self):
        with pytest.raises(HTTPException):
            _safe_str("has space", _RE_IDENTIFIER, "parcel_id")


class TestSafeStrAlphaLabel:
    def test_valid_single_word(self):
        assert _safe_str("Kigali", _RE_ALPHA_LABEL, "province") == "Kigali"

    def test_valid_multi_word(self):
        assert _safe_str("Northern Province", _RE_ALPHA_LABEL, "province") == "Northern Province"

    def test_valid_hyphenated(self):
        assert _safe_str("Musanze-East", _RE_ALPHA_LABEL, "district") == "Musanze-East"

    def test_rejects_single_quote(self):
        with pytest.raises(HTTPException):
            _safe_str("O'Malley", _RE_ALPHA_LABEL, "province")

    def test_rejects_sql_injection(self):
        with pytest.raises(HTTPException):
            _safe_str("Kigali'; DROP TABLE parcels;--", _RE_ALPHA_LABEL, "province")

    def test_rejects_or_injection(self):
        with pytest.raises(HTTPException):
            _safe_str("A' OR TRUE OR 'a", _RE_ALPHA_LABEL, "province")

    def test_rejects_comment_injection(self):
        with pytest.raises(HTTPException):
            _safe_str("A'--", _RE_ALPHA_LABEL, "province")

    def test_rejects_empty(self):
        with pytest.raises(HTTPException):
            _safe_str("", _RE_ALPHA_LABEL, "province")

    def test_rejects_leading_space(self):
        with pytest.raises(HTTPException):
            _safe_str(" Kigali", _RE_ALPHA_LABEL, "province")

    def test_rejects_digits(self):
        with pytest.raises(HTTPException):
            _safe_str("District123", _RE_ALPHA_LABEL, "province")


class TestSafeStrDate:
    def test_valid_date(self):
        assert _safe_str("2024-01-15", _RE_DATE, "date_from") == "2024-01-15"

    def test_valid_end_of_year(self):
        assert _safe_str("2025-12-31", _RE_DATE, "date_from") == "2025-12-31"

    def test_rejects_sql_injection(self):
        with pytest.raises(HTTPException):
            _safe_str("2024-01-01' OR '1'='1", _RE_DATE, "date_from")

    def test_rejects_impossible_month(self):
        with pytest.raises(HTTPException):
            _safe_str("2024-13-01", _RE_DATE, "date_from")

    def test_rejects_impossible_day(self):
        with pytest.raises(HTTPException):
            _safe_str("2024-01-45", _RE_DATE, "date_from")

    def test_rejects_zero_month(self):
        with pytest.raises(HTTPException):
            _safe_str("2024-00-15", _RE_DATE, "date_from")

    def test_rejects_zero_day(self):
        with pytest.raises(HTTPException):
            _safe_str("2024-01-00", _RE_DATE, "date_from")

    def test_rejects_wrong_format(self):
        with pytest.raises(HTTPException):
            _safe_str("01-15-2024", _RE_DATE, "date_from")

    def test_rejects_empty(self):
        with pytest.raises(HTTPException):
            _safe_str("", _RE_DATE, "date_from")


class TestSafeStrErrorMessage:
    def test_does_not_leak_input(self):
        with pytest.raises(HTTPException) as exc:
            _safe_str("'; DROP TABLE --", _RE_H3_INDEX, "h3_index")
        assert "DROP TABLE" not in exc.value.detail
        assert "h3_index" in exc.value.detail
