"""Unit tests for brain_sources registry helpers.

Covers the error-sanitizer that strips credentials out of last_error before
it's written to a table readable by anyone with source-table read.
"""

from __future__ import annotations

from src.services.brain_ingestion.registry import _sanitize_error


def test_sanitize_error_strips_query_string():
    raw = "fetch failed for https://api.example.com/v1/docs?token=SECRET123&foo=bar"
    out = _sanitize_error(raw)
    assert "SECRET123" not in out
    assert "token" not in out
    assert "https://api.example.com/v1/docs" in out


def test_sanitize_error_strips_basic_auth_userinfo():
    raw = "401 at https://alice:hunter2@internal.partner.io/feed.json"
    out = _sanitize_error(raw)
    assert "hunter2" not in out
    assert "alice" not in out
    assert "https://internal.partner.io/feed.json" in out


def test_sanitize_error_preserves_host_and_path():
    raw = "HTTPError('503 at https://example.org/path/here?x=1')"
    out = _sanitize_error(raw)
    assert "https://example.org/path/here" in out
    assert "x=1" not in out


def test_sanitize_error_handles_multiple_urls():
    raw = "redirect https://a.test/?k=1 -> https://b.test/?k=2 failed"
    out = _sanitize_error(raw)
    assert "k=1" not in out
    assert "k=2" not in out
    assert "https://a.test/" in out
    assert "https://b.test/" in out


def test_sanitize_error_noop_on_plain_text():
    raw = "RuntimeError: connection reset"
    assert _sanitize_error(raw) == raw


def test_sanitize_error_handles_empty():
    assert _sanitize_error("") == ""
