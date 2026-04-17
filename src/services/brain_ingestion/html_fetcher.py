"""HTMLFetcher — concrete BaseFetcher for text/html sources.

Uses bs4 + lxml (already in requirements.txt, no new dep). The plan
originally specified selectolax but bs4+lxml is fast enough at our
volumes (≤500 req/hr) and avoids a dependency bump on customer 1's
infra. Swap for selectolax later if the fetcher budget gets tight.

Scope: single-URL fetch today. crawl_depth > 0 is handled by overriding
BaseFetcher.discover() in subclasses (e.g. RABPortalFetcher follows
sitemap.xml). That keeps the base HTML fetcher simple and reusable.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from src.services.brain_ingestion.base import BaseFetcher, FetchSkipped
from src.services.brain_ingestion.models import FetchedContent

logger = logging.getLogger(__name__)


_STRIPPED_TAGS = ("script", "style", "noscript", "iframe", "svg", "form")


class HTMLFetcher(BaseFetcher):
    async def fetch_one(self, url: str) -> FetchedContent:
        headers = self.build_conditional_headers()
        r = await self.client.get(url, headers=headers)

        if r.status_code == 304:
            raise FetchSkipped("304 Not Modified")
        r.raise_for_status()

        raw = r.content
        ch = hashlib.sha256(raw).hexdigest()

        soup = BeautifulSoup(raw, "lxml")
        for tag in soup(_STRIPPED_TAGS):
            tag.decompose()

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else None

        lang = None
        html_tag = soup.find("html")
        if html_tag and html_tag.get("lang"):
            lang = html_tag.get("lang").split("-")[0]

        # Extract main text. Simple approach: concatenate visible text
        # under <main>/<article>/<body>. Dedicated fetchers (per-source
        # subclasses) can override for cleaner extraction.
        body = soup.find("main") or soup.find("article") or soup.body
        text = body.get_text(separator="\n", strip=True) if body else ""

        return FetchedContent(
            source_id=self.source.source_id,
            url=url,
            fetched_at=datetime.now(timezone.utc),
            content_type=r.headers.get("content-type", "text/html"),
            status_code=r.status_code,
            raw_bytes_len=len(raw),
            text=text,
            markdown=None,
            etag=r.headers.get("etag"),
            last_modified=r.headers.get("last-modified"),
            content_hash=ch,
            title=title,
            language=lang or self.source.language,
            tier=self.source.tier,
            access_scope=self.source.access_scope,
            partner_id=self.source.partner_id,
            license=self.source.license,
        )
