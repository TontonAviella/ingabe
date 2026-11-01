"""Access requests: someone without an invitation asks to join Ingabe, and Ingabe staff say yes or no.

Ingabe is invite-only: sign-up is off in WorkOS, so a person with no invitation who types their email on WorkOS'
sign-in page is refused, WorkOS sends no code, and its page still asks for one (it never says whether an address
has an account). On 2026-11-01 someone waited for that code and nobody at Ingabe knew (Roger: "we didn't get any
notifications where we could decide"). So a request now reaches staff three ways:

- "form": the app's Request access page (/request-access), where the website's and the app's sign-up links lead;
- "sign_up_attempt": WorkOS' log of a refused sign-up (an authentication.*_failed event with code
  sign_up_not_allowed; that code is in WorkOS' log, not its docs, seen first 2026-11-01), read every POLL_SECONDS;
- "waitlist": a WorkOS waitlist entry (waitlist_user.created), if the waitlist is turned on in WorkOS.

One open request per address: asking again, or trying again, updates it (attempts + 1) instead of making another.
Staff get a phone push (src/services/phone_push.py) for a new request, and again only if the same person is still
waiting RENOTIFY_AFTER later. They approve on the Companies page (an invitation into a company, which WorkOS emails)
or decline (the person is not told automatically; staff write to them if they want). The answer to the form is the
same whether or not the address already has an account.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Optional

import asyncpg

from src.services import phone_push, workos_auth
from src.services.project_people import looks_like_email

Source = Literal["form", "sign_up_attempt", "waitlist"]
Decision = Literal["approved", "declined"]

POLL_SECONDS = 300  # how often WorkOS' log is read for refused sign-ups and waitlist entries
EVENT_LOOKBACK = timedelta(hours=48)  # the first read, when no event was folded in yet
RENOTIFY_AFTER = timedelta(hours=24)  # a person still waiting is pushed again after this long
RECENT_DECIDED = timedelta(days=30)  # decided requests stay on the page this long
LIMITS = {"email": 254, "name": 120, "organization": 160, "reason": 1000}
REFUSED_SIGN_UP_CODES = frozenset({"sign_up_not_allowed"})
FAILED_SIGN_IN_EVENTS = ("authentication.magic_auth_failed", "authentication.password_failed",
                         "authentication.oauth_failed")
WAITLIST_EVENT = "waitlist_user.created"
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class Submission:
    email: str
    name: Optional[str] = None
    organization: Optional[str] = None
    reason: Optional[str] = None


def _text(value: Optional[str], field: str) -> Optional[str]:
    if value is None:
        return None
    cleaned = _CONTROL.sub("", value).strip()
    if len(cleaned) > LIMITS[field]:
        raise ValueError(f"Please keep the {field} under {LIMITS[field]} characters")
    return cleaned or None


def clean_submission(email: str, name: Optional[str] = None, organization: Optional[str] = None,
                     reason: Optional[str] = None) -> Submission:
    """What a person typed, checked: ValueError (in words for them) for a bad address or text that is too long."""
    address = (_text(email, "email") or "").lower()
    if not looks_like_email(address):
        raise ValueError("Enter the email address you'd like to use")
    return Submission(address, _text(name, "name"), _text(organization, "organization"), _text(reason, "reason"))


async def record(conn: asyncpg.Connection, sub: Submission, source: Source,
                 seen_at: Optional[datetime] = None) -> dict[str, Any]:
    """Keep a request, or fold it into the address's open one: the row, with "notify" True when staff should be
    pushed now (a new request, or a person still waiting RENOTIFY_AFTER after the last push). `seen_at` is the WorkOS
    event's time (None for the form)."""
    now = datetime.now(timezone.utc)
    row = await conn.fetchrow(
        """
        INSERT INTO access_requests (email, name, organization, reason, source, last_event_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (email) WHERE status = 'open' DO UPDATE SET
            name = COALESCE(EXCLUDED.name, access_requests.name),
            organization = COALESCE(EXCLUDED.organization, access_requests.organization),
            reason = COALESCE(EXCLUDED.reason, access_requests.reason),
            -- a form filled in says more than an attempt: keep it as the request's source
            source = CASE WHEN EXCLUDED.source = 'form' THEN 'form' ELSE access_requests.source END,
            attempts = access_requests.attempts + 1,
            updated_at = NOW(),
            last_event_at = GREATEST(access_requests.last_event_at, EXCLUDED.last_event_at)
        RETURNING *
        """,
        sub.email, sub.name, sub.organization, sub.reason, source, seen_at,
    )
    if row is None:  # INSERT ... ON CONFLICT DO UPDATE always returns the row
        raise RuntimeError(f"access request for {sub.email} was not kept")
    out: dict[str, Any] = dict(row)
    out["notify"] = out["notified_at"] is None or now - out["notified_at"] >= RENOTIFY_AFTER
    return out


async def mark_notified(conn: asyncpg.Connection, request_id: int) -> None:
    await conn.execute("UPDATE access_requests SET notified_at = NOW() WHERE id = $1", request_id)


async def listing(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Open requests (oldest first: who has waited longest) then those decided in the last RECENT_DECIDED."""
    rows = await conn.fetch(
        """
        SELECT id, email, name, organization, reason, source, attempts, created_at, updated_at, status,
               decided_by, decided_at, decided_how
        FROM access_requests
        WHERE status = 'open' OR decided_at > NOW() - $1::interval
        ORDER BY status <> 'open', CASE WHEN status = 'open' THEN created_at END, decided_at DESC
        """,
        RECENT_DECIDED,
    )
    return [dict(r) for r in rows]


async def get_open(conn: asyncpg.Connection, request_id: int) -> Optional[dict[str, Any]]:
    row = await conn.fetchrow("SELECT * FROM access_requests WHERE id = $1 AND status = 'open'", request_id)
    return dict(row) if row else None


async def decide(conn: asyncpg.Connection, request_id: int, decision: Decision, by: str,
                 how: str) -> Optional[dict[str, Any]]:
    """Close an open request; None when it is not open (already decided, or no such request)."""
    row = await conn.fetchrow(
        """
        UPDATE access_requests SET status = $2, decided_by = $3, decided_at = NOW(), decided_how = $4
        WHERE id = $1 AND status = 'open'
        RETURNING id, email, status, decided_by, decided_at, decided_how
        """,
        request_id, decision, by, how,
    )
    return dict(row) if row else None


async def events_read_from(conn: asyncpg.Connection) -> datetime:
    """Where the next read of WorkOS' log starts: just after the newest event already folded in, or
    EVENT_LOOKBACK ago the first time."""
    newest = await conn.fetchval("SELECT MAX(last_event_at) FROM access_requests")
    return newest or datetime.now(timezone.utc) - EVENT_LOOKBACK


def attempts_from_events(events: Iterable[Any], after: datetime) -> list[tuple[str, datetime, Source]]:
    """(email, when, source) for each refused sign-up and waitlist entry newer than `after`, oldest first. `events`
    are WorkOS Event objects (event, created_at, data.email, data.error.code); other failures (a wrong code, a
    wrong password) are not requests."""
    found = []
    for ev in events:
        when = ev.created_at
        if when <= after:
            continue
        kind = ev.event
        data = ev.data
        email = (getattr(data, "email", None) or "").strip().lower()
        if not email:
            continue
        if kind == WAITLIST_EVENT:
            found.append((email, when, "waitlist"))
        elif kind in FAILED_SIGN_IN_EVENTS:
            error = getattr(data, "error", None)
            if getattr(error, "code", None) in REFUSED_SIGN_UP_CODES:
                found.append((email, when, "sign_up_attempt"))
    return sorted(found, key=lambda f: f[1])


_HOW_THEY_ASKED = {
    "form": "asked on the Request access page",
    "sign_up_attempt": "tried to sign up (no code was sent: sign-up is invite-only)",
    "waitlist": "joined the waitlist on the sign-in page",
}


def push_message(request: dict[str, Any]) -> dict[str, str]:
    """The phone push for a request: title and body in plain words."""
    who = request.get("name") or request["email"]
    lines = [f"{who} {_HOW_THEY_ASKED[request['source']]}."]
    if request.get("name"):
        lines.append(request["email"])
    if request.get("organization"):
        lines.append(f"From: {request['organization']}")
    if request.get("reason"):
        lines.append(f"Why: {request['reason']}")
    if request.get("attempts", 1) > 1:
        lines.append(f"Asked or tried {request['attempts']} times.")
    lines.append("Approve or decline on the Companies page.")
    return {"title": "New access request" if request.get("attempts", 1) == 1 else "Still waiting for access",
            "message": "\n".join(lines)}


async def notify(conn: asyncpg.Connection, request: dict[str, Any]) -> None:
    """Push staff about a request that `record` said to (and remember it was pushed, so the next ask within
    RENOTIFY_AFTER stays quiet). A push that cannot be sent is tried again with the person's next ask."""
    if not request["notify"]:
        return
    msg = push_message(request)
    if await phone_push.send(msg["title"], msg["message"], click_path="/admin/companies", priority=4, tags=["wave"]):
        await mark_notified(conn, request["id"])


async def read_workos_log(conn: asyncpg.Connection) -> int:
    """Fold refused sign-ups and waitlist entries from WorkOS' log into requests, pushing staff for new ones: how
    many were read."""
    after = await events_read_from(conn)
    events = await asyncio.to_thread(workos_auth.list_events, [*FAILED_SIGN_IN_EVENTS, WAITLIST_EVENT], after)
    found = attempts_from_events(events, after)
    for email, when, source in found:
        await notify(conn, await record(conn, Submission(email), source, seen_at=when))
    return len(found)
