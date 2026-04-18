"""Pydantic models for Rwanda Brain ingestion."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, HttpUrl


SourceTier = Literal["T1", "T2", "T3", "T4"]
"""Tier taxonomy (from plan):
    T1 — Partner / authoritative internal (insurance contracts, RAB extension docs)
    T2 — Rwanda government / institutional (RAB, NISR, MINAGRI, Nasho, Gabiro Hub)
    T3 — Global research / NGO proxies (FAO, IFPRI, CGIAR, World Bank)
    T4 — Open web / news / social (flagged lower trust, kept for recall)
"""

SourceStatus = Literal[
    "active", "paused", "requires_auth", "opted_out", "broken"
]

AccessScope = Literal["public", "partner_internal", "mundi_only"]


class SourceConfig(BaseModel):
    """One ingestion source. Mirrors brain_sources table row.

    A source is the smallest unit the scheduler operates on: one URL or API
    endpoint, one tier, one rate budget. Multi-page sites become one source
    per seed URL with crawl_depth.
    """
    source_id: str = Field(..., min_length=1, max_length=128)
    name: str
    tier: SourceTier
    status: SourceStatus = "active"

    seed_url: HttpUrl
    crawl_depth: int = Field(default=0, ge=0, le=5)
    content_types: list[str] = Field(default_factory=lambda: ["text/html"])

    access_scope: AccessScope = "public"
    partner_id: Optional[str] = None
    license: Optional[str] = None
    language: str = "en"

    schedule_cron: Optional[str] = None
    max_requests_per_hour: int = 60
    max_requests_per_day: int = 500

    ocr_enabled: bool = False
    ocr_max_pages_per_doc: int = 200

    last_fetched_at: Optional[datetime] = None
    last_error: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None


class FetchedContent(BaseModel):
    """One fetched item before it becomes a brain_page.

    The fetcher returns these; a downstream normalizer converts them to
    PageInput + TimelineInput and calls BrainService.put_page. Keeping the
    two stages separate lets us swap normalizers (per-source) without
    changing fetchers.
    """
    source_id: str
    url: HttpUrl
    fetched_at: datetime
    content_type: str
    status_code: int

    raw_bytes_len: int
    text: Optional[str] = None
    markdown: Optional[str] = None

    etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_hash: Optional[str] = None

    title: Optional[str] = None
    language: Optional[str] = None

    # Tier/scope/partner inherited from SourceConfig at fetch time so the
    # normalizer doesn't have to re-look it up.
    tier: SourceTier
    access_scope: AccessScope
    partner_id: Optional[str] = None
    license: Optional[str] = None


class FetchResult(BaseModel):
    """Outcome of one fetcher run against one source."""
    source_id: str
    started_at: datetime
    finished_at: datetime
    items_fetched: int = 0
    items_skipped_unchanged: int = 0
    items_failed: int = 0
    bytes_downloaded: int = 0
    ocr_pages_processed: int = 0
    estimated_cost_usd: float = 0.0
    error: Optional[str] = None
