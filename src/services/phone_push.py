"""Pushes from the app to Ingabe staff's phones, on the same private ntfy channel as the alarms.

NTFY_TOPIC is the private channel (nothing is sent without it); NTFY_URL defaults to https://ntfy.sh. PUBLIC_APP_URL
(e.g. https://gis.nozalabs.rw) makes the push open the page it names; without it the push has no link. The alarm path
(scripts/alert_notifier.py) keeps its own sender: it runs alone in its own container with the standard library only.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def app_link(path: str) -> Optional[str]:
    """`path` on the public app (PUBLIC_APP_URL), or None when that is not set."""
    base = os.environ.get("PUBLIC_APP_URL", "").strip().rstrip("/")
    return f"{base}{path}" if base else None


async def send(title: str, message: str, click_path: Optional[str] = None, priority: int = 3,
               tags: Optional[list[str]] = None) -> bool:
    """Push to staff; True when ntfy took it. A push that cannot be sent is logged, never raised: the caller's work
    (a request kept, for one) stands either way."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        logger.warning("NTFY_TOPIC is not set; push not sent: %s", title)
        return False
    body: dict = {"topic": topic, "title": title, "message": message, "priority": priority}
    if tags:
        body["tags"] = tags
    link = app_link(click_path) if click_path else None
    if link:
        body["click"] = link
        body["actions"] = [{"action": "view", "label": "Open", "url": link}]
    url = os.environ.get("NTFY_URL", "https://ntfy.sh").strip().rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=body)
        if resp.status_code == 200:
            return True
        logger.warning("ntfy refused the push %r: HTTP %s", title, resp.status_code)
    except httpx.HTTPError as e:
        logger.warning("ntfy push %r failed: %s", title, e)
    return False
