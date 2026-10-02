"""Sage flight recorder: one Langfuse trace per Sage turn.

Grafana answers "is the service healthy" and PostHog "how do people use it";
this answers "what did Sage do on this turn, and where did it go wrong":
the routing decision and tool shortlist, every model call (fallbacks
included), every tool call with its arguments and result, and the
abdication guard. Problem turns carry tags (``abdication``,
``guard_recovered``, ``tool_error``, ``llm_error``, ``fallback_model``, ...)
so Langfuse's tag filter lists them directly.

Spans go to a self-hosted Langfuse over OTLP through a tracer provider of
their own, never the global one: Tempo and Langfuse do not see each other's
spans, and the infra trace id is kept as metadata to correlate the two.
Tracing is off unless ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` are
set; every method is then a no-op. Recording never raises into the caller.
"""
from __future__ import annotations

import base64
import functools
import json
import logging
import os
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, TypeVar

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from src.services.sage_tool_observability import summarize_tool_result

logger = logging.getLogger(__name__)

DEFAULT_HOST = "http://langfuse-web:3000"
# Per message / tool result / output, so one huge GeoJSON cannot bloat a trace.
_MAX_TEXT_CHARS = 4000
# Most recent messages kept as a model call's input.
_MAX_INPUT_MESSAGES = 16

_lock = threading.Lock()
_provider: Any = None
_tracer: trace.Tracer | None = None

_F = TypeVar("_F", bound=Callable[..., Any])


def _never_raises(method: _F) -> _F:
    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except Exception:
            logger.debug("sage flight recorder: %s failed", method.__name__, exc_info=True)
            return None

    return wrapper  # type: ignore[return-value]


def _settings() -> tuple[str, str, str] | None:
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not (public_key and secret_key):
        return None
    host = (os.environ.get("LANGFUSE_HOST") or DEFAULT_HOST).strip().rstrip("/")
    return host, public_key, secret_key


def get_tracer() -> trace.Tracer | None:
    """The Langfuse tracer, built on first use; None when tracing is off."""
    global _provider, _tracer
    if _tracer is not None:
        return _tracer
    settings = _settings()
    if settings is None:
        return None
    with _lock:
        if _tracer is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            host, public_key, secret_key = settings
            token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
            exporter = OTLPSpanExporter(
                endpoint=f"{host}/api/public/otel/v1/traces",
                headers={
                    "Authorization": f"Basic {token}",
                    "x-langfuse-ingestion-version": "4",
                },
            )
            provider = TracerProvider(resource=Resource.create({"service.name": "sage"}))
            provider.add_span_processor(BatchSpanProcessor(exporter))
            _provider = provider
            _tracer = provider.get_tracer("sage.flight_recorder")
            logger.info("sage flight recorder: tracing turns to Langfuse at %s", host)
    return _tracer


def flush(timeout_ms: int = 10_000) -> None:
    """Send buffered spans now; short-lived processes call this before exit."""
    if _provider is not None:
        _provider.force_flush(timeout_ms)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        return json.dumps(str(value))


def _clip(value: Any, limit: int = _MAX_TEXT_CHARS) -> str:
    text = value if isinstance(value, str) else _json(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [+{len(text) - limit} chars]"


def _clip_message(message: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(message)
    content = out.get("content")
    if out.get("role") == "system":
        out["content"] = f"[system prompt, {len(str(content or ''))} chars]"
    elif content is not None:
        out["content"] = _clip(content)
    return out


def _tool_names(tools: Sequence[Mapping[str, Any]] | None) -> list[str]:
    return [str((tool.get("function") or tool).get("name", "")) for tool in tools or []]


def _attribute_value(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)):
        return value
    return _json(value)


class Observation:
    """One step inside a turn (a span, model call, tool call or guardrail)."""

    def __init__(self, span: Any = None) -> None:
        self._span = span
        self._first_token_seen = False
        self.ended = span is None

    @_never_raises
    def first_token(self) -> None:
        """Mark time-to-first-token on a streamed model call (once)."""
        if self.ended or self._first_token_seen:
            return
        self._first_token_seen = True
        self._span.set_attribute(
            "langfuse.observation.completion_start_time",
            datetime.now(timezone.utc).isoformat(),
        )

    @_never_raises
    def end(
        self,
        *,
        output: Any = None,
        level: str | None = None,
        status: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self.ended:
            return
        self.ended = True
        span = self._span
        if output is not None:
            span.set_attribute("langfuse.observation.output", _clip(output, 4 * _MAX_TEXT_CHARS))
        for key, value in (metadata or {}).items():
            span.set_attribute(f"langfuse.observation.metadata.{key}", _attribute_value(value))
        if level:
            span.set_attribute("langfuse.observation.level", level)
        if status:
            span.set_attribute("langfuse.observation.status_message", _clip(status, 1000))
        if level == "ERROR":
            span.set_status(Status(StatusCode.ERROR, _clip(status or "error", 1000)))
        span.end()


class TurnTrace:
    """The trace of one Sage turn; the root span is the turn itself."""

    def __init__(
        self,
        tracer: trace.Tracer | None,
        *,
        name: str,
        session_id: str | None,
        user_id: str | None,
        metadata: Mapping[str, Any] | None = None,
        tags: Sequence[str] = (),
        environment: str | None = None,
    ) -> None:
        self._tracer = tracer
        self._tags: set[str] = set(tags)
        self._open: list[Observation] = []
        self._tools: dict[str, Observation] = {}
        self._input_set = False
        self._output: Any = None
        self._environment = environment or os.environ.get("LANGFUSE_TRACING_ENVIRONMENT") or "local"
        self._root: Any = None
        self._context: Any = None
        if tracer is None:
            return
        try:
            infra = trace.get_current_span().get_span_context()
            # A fresh, empty context: the turn starts its own trace rather
            # than nesting under whatever infra span is current.
            self._root = tracer.start_span(name, context=otel_context.Context())
            self._context = trace.set_span_in_context(self._root)
            self._root.set_attribute("langfuse.trace.name", name)
            self._root.set_attribute("langfuse.environment", self._environment)
            if session_id:
                self._root.set_attribute("langfuse.session.id", str(session_id))
            if user_id:
                self._root.set_attribute("langfuse.user.id", str(user_id))
            meta = dict(metadata or {})
            if infra.is_valid:
                meta["infra_trace_id"] = format(infra.trace_id, "032x")
            for key, value in meta.items():
                if value is not None:
                    self._root.set_attribute(f"langfuse.trace.metadata.{key}", _attribute_value(value))
        except Exception:
            logger.debug("sage flight recorder: could not start turn trace", exc_info=True)
            self._root = None

    @property
    def enabled(self) -> bool:
        return self._root is not None

    @property
    def tags(self) -> frozenset[str]:
        return frozenset(self._tags)

    def flag(self, *tags: str) -> None:
        """Tag the turn; tags are how problem turns are found in Langfuse."""
        self._tags.update(tag for tag in tags if tag)

    @_never_raises
    def set_input(self, text: Any) -> None:
        if not self.enabled or self._input_set:
            return
        self._input_set = True
        self._root.set_attribute("langfuse.observation.input", _clip(text))

    def set_output(self, value: Any) -> None:
        self._output = value

    def observe(
        self,
        name: str,
        *,
        kind: str = "span",
        input: Any = None,
        metadata: Mapping[str, Any] | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Observation:
        """Start a child observation; call ``end`` on the result."""
        if not self.enabled:
            return Observation()
        try:
            span = self._tracer.start_span(name, context=self._context)  # type: ignore[union-attr]
            span.set_attribute("langfuse.observation.type", kind)
            span.set_attribute("langfuse.environment", self._environment)
            if input is not None:
                span.set_attribute("langfuse.observation.input", _clip(input, 4 * _MAX_TEXT_CHARS))
            for key, value in (metadata or {}).items():
                if value is not None:
                    span.set_attribute(f"langfuse.observation.metadata.{key}", _attribute_value(value))
            for key, value in (attributes or {}).items():
                span.set_attribute(key, value)
        except Exception:
            logger.debug("sage flight recorder: could not start %s", name, exc_info=True)
            return Observation()
        observation = Observation(span)
        self._open.append(observation)
        return observation

    def event(
        self,
        name: str,
        *,
        input: Any = None,
        output: Any = None,
        level: str | None = None,
        status: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.observe(name, input=input, metadata=metadata).end(output=output, level=level, status=status)

    def routing(
        self,
        *,
        user_text: str,
        reason: str,
        categories: Sequence[str],
        small_talk: bool,
        tools: Sequence[Mapping[str, Any]] | None,
        shortlist: str | None,
        model: str | None,
    ) -> None:
        """Record which tools the model will be offered this turn, and why."""
        self.set_input(user_text)
        names = _tool_names(tools)
        if small_talk:
            self.flag("small_talk")
        if shortlist == "bm25":
            # The embedding half of the shortlist failed; recall drops.
            self.flag("shortlist_bm25_fallback")
        self.event(
            "routing",
            input=user_text,
            output={
                "reason": reason,
                "categories": sorted(categories),
                "small_talk": small_talk,
                "shortlist": shortlist,
                "tool_count": len(names),
                "tools": names,
            },
            level="WARNING" if shortlist == "bm25" else None,
            metadata={"routing_reason": reason, "tool_count": len(names), "model": model},
        )

    def fast_path(self, handler: str, *, user_text: str | None = None) -> None:
        """A deterministic fast path answered the turn without the model."""
        if user_text is not None:
            self.set_input(user_text)
        self.flag(f"fast_path:{handler}")
        self.event("fast_path", output={"handler": handler}, metadata={"handler": handler})

    def generation(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        step: int,
        attempt: int = 0,
        parameters: Mapping[str, Any] | None = None,
        name: str = "model_call",
    ) -> Observation:
        """Start a model call; ``end`` it with the reply or the error."""
        if not self.enabled:
            return Observation()
        names = _tool_names(tools)
        system = next((m for m in messages if m.get("role") == "system"), None)
        recent = [m for m in messages if m is not system][-_MAX_INPUT_MESSAGES:]
        shown = ([system] if system is not None else []) + recent
        return self.observe(
            name,
            kind="generation",
            input=[_clip_message(m) for m in shown],
            metadata={
                "step": step,
                "attempt": attempt,
                "tool_count": len(names),
                "tool_names": ",".join(names),
                "message_count": len(messages),
            },
            attributes={
                "langfuse.observation.model.name": model,
                "langfuse.observation.model.parameters": _json(dict(parameters or {})),
            },
        )

    @_never_raises
    def tool_started(self, call_id: str, name: str, args: Any) -> None:
        if not self.enabled or not call_id:
            return
        self._tools[call_id] = self.observe(
            name,
            kind="tool",
            input=args,
            metadata={"tool_name": name, "tool_call_id": call_id},
        )

    @_never_raises
    def tool_finished(self, call_id: str, content: Any) -> None:
        """Close a tool call with its result; failed results flag the turn."""
        observation = self._tools.pop(str(call_id or ""), None)
        if observation is None:
            return
        summary = summarize_tool_result(content)
        error_text = None
        if summary["tool_has_error"]:
            self.flag("tool_error")
            parsed = content
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                except Exception:
                    parsed = content
            error_text = str(parsed.get("error") or summary["tool_status"]) if isinstance(parsed, Mapping) else str(parsed)
        observation.end(
            output=content,
            level="WARNING" if summary["tool_has_error"] else None,
            status=error_text,
            metadata={"tool_status": summary["tool_status"], "result_size_chars": summary["result_size_chars"]},
        )

    @_never_raises
    def finish(self, *, error: str | None = None) -> None:
        """End the turn; observations still open are closed and flagged."""
        if not self.enabled:
            return
        for observation in self._open:
            if not observation.ended:
                self.flag("unfinished_step")
                observation.end(level="WARNING", status="turn ended before this step finished")
        if error:
            self.flag("turn_error")
        root = self._root
        if self._output is not None:
            root.set_attribute("langfuse.observation.output", _clip(self._output))
        if self._tags:
            root.set_attribute("langfuse.trace.tags", sorted(self._tags))
        if error:
            root.set_attribute("langfuse.observation.level", "ERROR")
            root.set_attribute("langfuse.observation.status_message", _clip(error, 1000))
            root.set_status(Status(StatusCode.ERROR, _clip(error, 1000)))
        elif self._tags & PROBLEM_TAGS:
            root.set_attribute("langfuse.observation.level", "WARNING")
        root.end()
        self._root = None


# Tags that mark a turn as worth a look; the turn's level becomes WARNING.
PROBLEM_TAGS = frozenset({
    "abdication",
    "guard_fired",
    "tool_error",
    "llm_error",
    "fallback_model",
    "step_limit",
    "shortlist_bm25_fallback",
    "unfinished_step",
})


@contextmanager
def sage_turn_trace(
    *,
    session_id: str | None,
    user_id: str | None,
    metadata: Mapping[str, Any] | None = None,
    tags: Sequence[str] = (),
    name: str = "sage.turn",
    environment: str | None = None,
    tracer: trace.Tracer | None = None,
) -> Iterator[TurnTrace]:
    """Trace one turn; the turn ends (and errors are recorded) on exit."""
    turn = TurnTrace(
        tracer if tracer is not None else get_tracer(),
        name=name,
        session_id=session_id,
        user_id=user_id,
        metadata=metadata,
        tags=tags,
        environment=environment,
    )
    try:
        yield turn
    except BaseException as exc:
        if type(exc).__name__ == "CancelledError":
            turn.flag("cancelled")
            turn.finish()
        else:
            turn.finish(error=f"{type(exc).__name__}: {exc}")
        raise
    else:
        turn.finish()
