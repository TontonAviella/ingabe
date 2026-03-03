"""Circuit breaker and retry utilities for external service calls.

Uses tenacity (already in requirements.txt) for retry with exponential backoff
and a simple circuit breaker state machine to prevent cascading failures.
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation, requests pass through
    OPEN = "open"           # Failures exceeded threshold, requests blocked
    HALF_OPEN = "half_open" # Testing if service recovered


class CircuitBreaker:
    """Simple circuit breaker for external service calls.

    Usage:
        cb = CircuitBreaker("open-meteo", failure_threshold=5, recovery_timeout=60)

        if cb.can_execute():
            try:
                result = call_external_service()
                cb.record_success()
            except Exception:
                cb.record_failure()
                raise
        else:
            raise ServiceUnavailableError("Circuit open for open-meteo")
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._success_count_in_half_open = 0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            # Check if recovery timeout has elapsed
            if self._last_failure_time and (time.monotonic() - self._last_failure_time) >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._success_count_in_half_open = 0
                logger.info("Circuit breaker '%s' → HALF_OPEN (recovery timeout elapsed)", self.name)
        return self._state

    def can_execute(self) -> bool:
        return self.state != CircuitState.OPEN

    def record_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._success_count_in_half_open += 1
            if self._success_count_in_half_open >= 2:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                logger.info("Circuit breaker '%s' → CLOSED (service recovered)", self.name)
        elif self._state == CircuitState.CLOSED:
            self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning(
                "Circuit breaker '%s' → OPEN (failures=%d, threshold=%d, recovery=%ds)",
                self.name, self._failure_count, self.failure_threshold, self.recovery_timeout,
            )
        elif self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning("Circuit breaker '%s' → OPEN (failure during half-open test)", self.name)


# Pre-configured circuit breakers for external services
open_meteo_cb = CircuitBreaker("open-meteo", failure_threshold=3, recovery_timeout=120)
sentinel_hub_cb = CircuitBreaker("sentinel-hub", failure_threshold=3, recovery_timeout=120)
isdasoil_cb = CircuitBreaker("isdasoil", failure_threshold=3, recovery_timeout=120)
