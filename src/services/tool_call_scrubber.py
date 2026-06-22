"""Streaming scrubber for models that emit tool-call XML as visible text."""

from __future__ import annotations


class ToolCallTextScrubber:
    """Drops `<tool_call>...</tool_call>` XML/markup from streamed text.

    Nemotron 3 Super 120B can emit tool calls as visible XML-ish text instead
    of OpenAI structured `tool_calls`. The real tool call should be routed via
    `delta.tool_calls`; this scrubber prevents the XML fallback text from
    leaking into the user's chat stream.
    """

    _OPEN = "<tool_call>"
    _CLOSE = "</tool_call>"

    def __init__(self) -> None:
        self._buffer: str = ""
        self._inside: bool = False
        self._lookback: int = max(len(self._OPEN), len(self._CLOSE))

    def feed(self, delta: str) -> str:
        """Append `delta`, return whatever is safe to stream to the user now."""

        self._buffer += delta
        out: list[str] = []
        while self._buffer:
            if self._inside:
                idx = self._buffer.find(self._CLOSE)
                if idx == -1:
                    keep = min(len(self._buffer), self._lookback)
                    self._buffer = self._buffer[-keep:] if keep else ""
                    break
                self._buffer = self._buffer[idx + len(self._CLOSE):]
                self._inside = False
            else:
                idx = self._buffer.find(self._OPEN)
                if idx == -1:
                    safe_len = max(0, len(self._buffer) - self._lookback)
                    out.append(self._buffer[:safe_len])
                    self._buffer = self._buffer[safe_len:]
                    break
                out.append(self._buffer[:idx])
                self._buffer = self._buffer[idx + len(self._OPEN):]
                self._inside = True
        return "".join(out)

    def flush(self) -> str:
        """End-of-stream flush. Returns any safe-to-emit remainder."""

        if self._inside:
            self._buffer = ""
            self._inside = False
            return ""
        out = self._buffer
        self._buffer = ""
        return out


# Backwards-compatible name used by the existing tests and route code.
_ToolCallTextScrubber = ToolCallTextScrubber
