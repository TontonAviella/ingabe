"""Unit tests for _ToolCallTextScrubber in src/routes/message_routes.py.

The scrubber removes `<tool_call>...</tool_call>` XML markup that Nemotron
3 Super 120B (and other thinking models) sometimes emit as visible text
content instead of routing through delta.tool_calls. Without it, BK users
see raw `<tool_call><function=display_satellite_layer>...` text in chat.

These tests are protection for a LIVE PROD scrubber — regressions here
mean prod regressions. Cover:
  - happy paths (no markup, complete tag in single delta)
  - tags split across deltas (lookback buffer correctness)
  - unclosed tags (flush behaviour)
  - multiple tags in a single stream
  - edge cases (tag at start/end of delta, partial OPEN/CLOSE in lookback)
"""
from __future__ import annotations

import pytest

from src.routes.message_routes import _ToolCallTextScrubber


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_empty_input_yields_empty_output():
    s = _ToolCallTextScrubber()
    assert s.feed("") == ""
    assert s.flush() == ""


def test_plain_text_no_markup_passes_through():
    s = _ToolCallTextScrubber()
    # Lookback buffer holds back the last `max(len(OPEN), len(CLOSE))` chars
    # until flush, so a single feed may not emit everything up front.
    chunk1 = s.feed("Hello, world! ")
    chunk2 = s.feed("This is plain text.")
    tail = s.flush()
    assert chunk1 + chunk2 + tail == "Hello, world! This is plain text."


def test_complete_tag_in_single_delta_is_dropped():
    s = _ToolCallTextScrubber()
    out = s.feed("before <tool_call>function=foo arg=1</tool_call> after")
    tail = s.flush()
    # Everything inside (and including) the tags is suppressed.
    assert (out + tail).replace(" ", "") == "beforeafter".replace(" ", "")
    # Allow whitespace tolerance — exact reconstruction:
    full = out + tail
    assert "<tool_call>" not in full
    assert "</tool_call>" not in full
    assert "function=foo" not in full
    assert full == "before  after"


# ---------------------------------------------------------------------------
# Tag split across multiple deltas (lookback buffer correctness)
# ---------------------------------------------------------------------------


def test_open_tag_split_across_two_deltas():
    s = _ToolCallTextScrubber()
    # Open tag "<tool_call>" arrives as "<tool" then "_call>"
    out1 = s.feed("OK <tool")
    out2 = s.feed("_call>secret payload</tool_call> done")
    tail = s.flush()
    full = out1 + out2 + tail
    assert "<tool_call>" not in full
    assert "secret payload" not in full
    assert full == "OK  done"


def test_close_tag_split_across_two_deltas():
    s = _ToolCallTextScrubber()
    out1 = s.feed("pre <tool_call>guts</tool")
    out2 = s.feed("_call> post")
    tail = s.flush()
    full = out1 + out2 + tail
    assert "guts" not in full
    assert "</tool_call>" not in full
    assert full == "pre  post"


def test_open_tag_split_into_three_deltas():
    s = _ToolCallTextScrubber()
    out1 = s.feed("X<")
    out2 = s.feed("too")
    out3 = s.feed("l_call>HIDDEN</tool_call>Y")
    tail = s.flush()
    full = out1 + out2 + out3 + tail
    assert "HIDDEN" not in full
    assert full == "XY"


def test_close_tag_split_at_every_character():
    s = _ToolCallTextScrubber()
    parts = ["start<tool_call>x"] + list("</tool_call>") + ["end"]
    full = "".join(s.feed(p) for p in parts) + s.flush()
    assert "</tool_call>" not in full
    assert "x" not in full
    assert full == "startend"


# ---------------------------------------------------------------------------
# Unclosed tags (flush behaviour)
# ---------------------------------------------------------------------------


def test_unclosed_tag_at_stream_end_drops_everything_after_open():
    s = _ToolCallTextScrubber()
    out1 = s.feed("kept <tool_call>partial content but no close")
    tail = s.flush()
    full = out1 + tail
    assert full == "kept "
    assert "partial content" not in full


def test_unclosed_tag_emits_nothing_at_flush():
    s = _ToolCallTextScrubber()
    s.feed("<tool_call>opened but never closed")
    # No close tag → flush returns empty, dropping the buffered noise.
    assert s.flush() == ""


# ---------------------------------------------------------------------------
# Multiple tags + tag positioning
# ---------------------------------------------------------------------------


def test_two_consecutive_tags_in_one_stream():
    s = _ToolCallTextScrubber()
    out = s.feed("a<tool_call>x</tool_call>b<tool_call>y</tool_call>c")
    tail = s.flush()
    full = out + tail
    assert "x" not in full
    assert "y" not in full
    assert "<tool_call>" not in full
    assert full == "abc"


def test_tag_at_start_of_stream():
    s = _ToolCallTextScrubber()
    out = s.feed("<tool_call>hidden</tool_call>visible")
    tail = s.flush()
    assert out + tail == "visible"


def test_tag_at_end_of_stream():
    s = _ToolCallTextScrubber()
    out = s.feed("visible<tool_call>hidden</tool_call>")
    tail = s.flush()
    assert out + tail == "visible"


def test_only_tag_no_surrounding_content():
    s = _ToolCallTextScrubber()
    out = s.feed("<tool_call>just the tag</tool_call>")
    tail = s.flush()
    assert out + tail == ""


# ---------------------------------------------------------------------------
# Lookback buffer edge cases
# ---------------------------------------------------------------------------


def test_lookback_holds_back_potential_partial_open():
    """The lookback buffer must hold the last `max(len(OPEN), len(CLOSE))`
    chars so we can detect a tag that starts at the very end of a delta."""
    s = _ToolCallTextScrubber()
    # "<tool" is 5 chars — a possible partial open. Scrubber should NOT
    # emit it yet (could be the start of "<tool_call>").
    out1 = s.feed("safe text<tool")
    # If the scrubber emits "<tool" prematurely, the user sees garbage when
    # the next delta is "_call>...</tool_call>". So this assertion checks:
    # whatever is emitted must not break the contract.
    out2 = s.feed("_call>SECRET</tool_call>tail")
    tail = s.flush()
    full = out1 + out2 + tail
    assert "SECRET" not in full
    assert full == "safe texttail"


def test_lookback_doesnt_swallow_legitimate_angle_brackets():
    """Plain `<` characters or HTML-ish text should still be emitted if no
    full `<tool_call>` follows. The lookback only HOLDS them temporarily."""
    s = _ToolCallTextScrubber()
    out = s.feed("Use ST_Within(geom, poly) — that's 5 < 10 always true. ")
    tail = s.flush()
    full = out + tail
    # The `< 10` survives because no `<tool_call>` ever shows up.
    assert "5 < 10" in full
    assert "ST_Within" in full


def test_text_with_partial_tag_lookalike_passes_through():
    """A string like `<toolbox>` (not `<tool_call>`) must not be mistakenly
    treated as the start of a tool call."""
    s = _ToolCallTextScrubber()
    out = s.feed("Open the <toolbox> for me.")
    tail = s.flush()
    full = out + tail
    assert full == "Open the <toolbox> for me."


# ---------------------------------------------------------------------------
# Whitespace + complex content inside tags
# ---------------------------------------------------------------------------


def test_multiline_content_inside_tag_is_dropped():
    s = _ToolCallTextScrubber()
    payload = (
        "<tool_call>\n"
        "<function=display_satellite_layer>\n"
        "<parameter=bbox>30.4,-1.0,30.5,-0.9</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    out = s.feed(f"Before {payload} after")
    tail = s.flush()
    full = out + tail
    assert "display_satellite_layer" not in full
    assert "bbox" not in full
    assert "30.4" not in full
    assert full == "Before  after"


def test_real_world_nemotron_leak_pattern():
    """Reconstruction of the actual leak from BK testing on 2026-05-13.
    Validates that the exact pattern Nemotron emitted is fully suppressed."""
    s = _ToolCallTextScrubber()
    chunks = [
        "Let me search for that.\n\n",
        "<tool_call> <function=display_satellite_layer> ",
        "<parameter=bbox> 30.439362,-1.067327,30.449362,-1.057327 </parameter> ",
        "<parameter=date_from> 2026-04-29 </parameter> ",
        "<parameter=date_to> 2026-05-13 </parameter> ",
        "<parameter=layer_name> Satellite Image Last Two Weeks Point </parameter> ",
        "<parameter=max_cloud_pct> 80 </parameter> ",
        "</function> </tool_call>",
        "\n\nResult shown.",
    ]
    parts = [s.feed(c) for c in chunks]
    tail = s.flush()
    full = "".join(parts) + tail
    assert "tool_call" not in full
    assert "display_satellite_layer" not in full
    assert "max_cloud_pct" not in full
    assert "30.439362" not in full
    # User-visible text only:
    assert full == "Let me search for that.\n\n\n\nResult shown."


# ---------------------------------------------------------------------------
# State machine invariants
# ---------------------------------------------------------------------------


def test_scrubber_can_be_reused_after_flush():
    """A fresh stream should start with clean state. (We create a new
    scrubber per attempt in prod, so this is defence-in-depth.)"""
    s = _ToolCallTextScrubber()
    s.feed("first<tool_call>noise</tool_call>stream")
    s.flush()
    out = s.feed("second clean stream")
    tail = s.flush()
    assert out + tail == "second clean stream"


def test_flush_clears_buffer_even_when_inside_tag():
    s = _ToolCallTextScrubber()
    s.feed("<tool_call>opened")
    s.flush()  # drops the unclosed content
    # After flush, scrubber should be in NORMAL state again, ready for new content
    out = s.feed("clean text")
    assert s.flush() + out == "clean text" or out + s.flush() != "clean text"
    # Allow ordering: clean text should fully come through across feed + flush
    s2 = _ToolCallTextScrubber()
    s2.feed("<tool_call>noise")
    s2.flush()
    out2 = s2.feed("after reset")
    assert out2 + s2.flush() == "after reset"


# ---------------------------------------------------------------------------
# Performance characteristics (smoke — not a benchmark)
# ---------------------------------------------------------------------------


def test_large_clean_input_doesnt_quadratic_blowup():
    """Sanity: 100KB of clean text should process quickly, not O(N^2).
    Just verifies we don't accidentally introduce repeated string concat
    inside the loop."""
    import time
    s = _ToolCallTextScrubber()
    big = "x" * 100_000
    t0 = time.monotonic()
    out = s.feed(big)
    out += s.flush()
    elapsed = time.monotonic() - t0
    assert out == big
    assert elapsed < 1.0, f"Scrubber too slow on 100KB input: {elapsed:.3f}s"
