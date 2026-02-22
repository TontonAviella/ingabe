"""Tests for domain exception hierarchy."""

import pytest

from src.exceptions import (
    ConflictError,
    DomainError,
    ExternalServiceError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)


class TestDomainExceptionHierarchy:
    """All domain exceptions inherit from DomainError and have correct status codes."""

    @pytest.mark.parametrize(
        "exc_class,expected_code",
        [
            (DomainError, 500),
            (NotFoundError, 404),
            (PermissionDeniedError, 403),
            (ValidationError, 422),
            (ConflictError, 409),
            (ExternalServiceError, 502),
        ],
        ids=["domain", "not_found", "permission", "validation", "conflict", "external"],
    )
    def test_status_codes(self, exc_class, expected_code):
        exc = exc_class()
        assert exc.status_code == expected_code
        assert isinstance(exc, DomainError)
        assert isinstance(exc, Exception)

    def test_not_found_with_resource(self):
        exc = NotFoundError("map", "M12345")
        assert str(exc) == "map 'M12345' not found"

    def test_not_found_without_identifier(self):
        exc = NotFoundError("layer")
        assert str(exc) == "layer not found"

    def test_external_service_with_message(self):
        exc = ExternalServiceError("S3", "connection timeout")
        assert str(exc) == "S3: connection timeout"

    def test_external_service_without_message(self):
        exc = ExternalServiceError("QGIS")
        assert str(exc) == "QGIS error"

    def test_domain_error_caught_as_exception(self):
        with pytest.raises(Exception):
            raise NotFoundError("test")

    def test_domain_error_caught_as_domain_error(self):
        with pytest.raises(DomainError):
            raise PermissionDeniedError("no access")
