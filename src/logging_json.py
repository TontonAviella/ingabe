"""Structured JSON log formatter for production observability.

Outputs one JSON object per log line with consistent fields:
  timestamp, level, logger, message, request_id (if available), exc_info
"""

import json
import logging
import traceback
from datetime import datetime, timezone

from opentelemetry import trace


class JsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # The trace this line belongs to (Tempo), so Grafana can jump from a log line to the request's trace.
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            log_entry["trace_id"] = format(span_context.trace_id, "032x")

        # Include request_id if set on the record (injected by middleware)
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id

        # Include exception info if present
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exception(*record.exc_info),
            }

        # Include extra fields (e.g. from logger.info("msg", extra={...}))
        for key in ("duration_ms", "status_code", "method", "path", "user_id"):
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)
        # A dict of named facts (logger.warning("...", extra={"fields": {...}})), e.g. a browser error's page.
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                log_entry.setdefault(str(key), value)

        return json.dumps(log_entry, default=str, ensure_ascii=False)
