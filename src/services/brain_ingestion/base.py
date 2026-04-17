"""BaseFetcher — abstract fetcher with retry, backoff, structured logs.

Every concrete fetcher (RAB extension PDFs, NISR statistical bulletins,
Nasho scheme docs, FAO country briefs, partner_internal contract PDFs...)
subclasses BaseFetcher and implements .fetch_one(). The base class handles:

- structured logging with source_id + tier + run_uuid
- exponential backoff on transient errors (5xx, timeouts, connection reset)
- ETag / If-Modified-Since conditional requests
- content_hash-based dedupe (won't re-write unchanged pages)
- error classification — what goes to last_error on brain_sources vs raised

Concrete fetchers live next to this file: html_fetcher.py, pdf_fetcher.py,
api_fetcher.py. Phase 1 adds the first three; Phase 2 adds authenticated
(partner_internal) variants.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import httpx

from src.services.brain_ingestion.models import (
    FetchedContent,
    FetchResult,
    SourceConfig,
)

logger = logging.getLogger(__name__)


TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
MAX_RETRIES = 5
INITIAL_BACKOFF_SEC = 1.0
MAX_BACKOFF_SEC = 60.0


class FetchSkipped(Exception):
    """Non-error skip reason (304 Not Modified, content_hash match)."""


class BaseFetcher(ABC):
    """Subclass contract: implement .fetch_one(url). The class iterates
    seed + crawl_depth and wraps retry/backoff/logging around each call.
    """

    def __init__(
        self,
        source: SourceConfig,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        self.source = source
        self._owns_client = http_client is None
        self.client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "mundi.ai-brain-ingest/0.1 "
                    "(+https://mundi.ai ; Ingabe SAS, Rwanda)"
                ),
            },
        )
        self.run_uuid = str(uuid.uuid4())

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @abstractmethod
    async def fetch_one(self, url: str) -> FetchedContent:
        """Fetch a single URL. Subclass implements parsing/OCR/etc.

        Should raise FetchSkipped for conditional-304 / unchanged-content,
        httpx.HTTPStatusError for HTTP errors, asyncio.TimeoutError for
        timeouts. Any other Exception is treated as fatal for the run.
        """
        ...

    async def discover(self) -> AsyncIterator[str]:
        """Yield URLs to fetch. Default = seed URL only.

        Override for crawl_depth > 0 (sitemap walks, link follow, API
        pagination). Must respect robots.txt — subclass responsibility.
        """
        yield str(self.source.seed_url)

    async def run(self) -> FetchResult:
        """Run one ingest cycle. Returns aggregated FetchResult."""
        started = datetime.now(timezone.utc)
        result = FetchResult(
            source_id=self.source.source_id,
            started_at=started,
            finished_at=started,
        )
        log = logger.getChild(self.source.source_id)
        log.info(
            "fetch_run_start",
            extra={
                "source_id": self.source.source_id,
                "tier": self.source.tier,
                "run_uuid": self.run_uuid,
                "seed_url": str(self.source.seed_url),
            },
        )

        try:
            async for url in self.discover():
                try:
                    item = await self._fetch_with_retry(url)
                    if item is None:
                        result.items_skipped_unchanged += 1
                        continue
                    result.items_fetched += 1
                    result.bytes_downloaded += item.raw_bytes_len
                    # Hand off to normalizer/writer. Left unset here — the
                    # caller (scheduler/worker) owns persistence so base
                    # stays pure and testable.
                    await self.on_item(item)
                except FetchSkipped:
                    result.items_skipped_unchanged += 1
                except Exception:
                    result.items_failed += 1
                    log.exception("fetch_item_failed", extra={"url": url})
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
            log.exception("fetch_run_failed")
        finally:
            result.finished_at = datetime.now(timezone.utc)
            log.info(
                "fetch_run_end",
                extra={
                    "source_id": self.source.source_id,
                    "run_uuid": self.run_uuid,
                    "items_fetched": result.items_fetched,
                    "items_failed": result.items_failed,
                    "items_skipped": result.items_skipped_unchanged,
                    "duration_sec": (
                        result.finished_at - result.started_at
                    ).total_seconds(),
                },
            )
            await self.close()

        return result

    async def on_item(self, item: FetchedContent) -> None:
        """Override to persist the fetched item. Default: no-op.

        Real implementations call brain_service.put_page + TimelineInput.
        Left as a hook so the scheduler can inject its own writer with
        the right access_scope/partner_id enforcement.
        """

    async def _fetch_with_retry(
        self, url: str
    ) -> Optional[FetchedContent]:
        backoff = INITIAL_BACKOFF_SEC
        last_exc: Optional[BaseException] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                item = await self.fetch_one(url)
                return item
            except FetchSkipped:
                return None
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status not in TRANSIENT_STATUS:
                    raise
                last_exc = e
            except (httpx.TransportError, asyncio.TimeoutError) as e:
                last_exc = e

            if attempt == MAX_RETRIES:
                break
            logger.warning(
                "fetch_retry",
                extra={
                    "source_id": self.source.source_id,
                    "url": url,
                    "attempt": attempt,
                    "backoff_sec": backoff,
                    "error": repr(last_exc),
                },
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SEC)

        assert last_exc is not None
        raise last_exc

    @staticmethod
    def content_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def build_conditional_headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self.source.etag:
            h["If-None-Match"] = self.source.etag
        if self.source.last_modified:
            h["If-Modified-Since"] = self.source.last_modified
        return h
