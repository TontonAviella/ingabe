"""Domain exceptions for the service layer.

Services should raise these instead of ``fastapi.HTTPException`` so they
remain framework-agnostic and testable without HTTP context.

Routes (or middleware) are responsible for catching domain exceptions and
converting them into HTTP responses.

Usage in services::

    from src.exceptions import NotFoundError, PermissionDeniedError

    async def get_map(map_id: str, user_id: str) -> ...:
        row = await conn.fetchrow("SELECT ... WHERE id=$1", map_id)
        if not row:
            raise NotFoundError("map", map_id)
        if row["owner_uuid"] != user_id:
            raise PermissionDeniedError("Not authorised for this map")

Usage in routes::

    from src.exceptions import DomainError

    try:
        result = await service.do_something(...)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain/service-layer exceptions."""

    status_code: int = 500

    def __init__(self, message: str = "Internal error"):
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


class NotFoundError(DomainError):
    """Raised when a requested resource does not exist."""

    status_code = 404

    def __init__(self, resource: str = "resource", identifier: str = ""):
        detail = f"{resource} not found"
        if identifier:
            detail = f"{resource} '{identifier}' not found"
        super().__init__(detail)


class PermissionDeniedError(DomainError):
    """Raised when the user lacks permission for the requested operation."""

    status_code = 403

    def __init__(self, message: str = "Permission denied"):
        super().__init__(message)


class ValidationError(DomainError):
    """Raised when input data fails domain-level validation."""

    status_code = 422

    def __init__(self, message: str = "Validation error"):
        super().__init__(message)


class ConflictError(DomainError):
    """Raised when the operation conflicts with current state (e.g. duplicate)."""

    status_code = 409

    def __init__(self, message: str = "Conflict"):
        super().__init__(message)


class ExternalServiceError(DomainError):
    """Raised when an external service (S3, QGIS, etc.) fails."""

    status_code = 502

    def __init__(self, service: str = "external service", message: str = ""):
        detail = f"{service} error"
        if message:
            detail = f"{service}: {message}"
        super().__init__(detail)
