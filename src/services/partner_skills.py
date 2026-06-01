"""Partner skill registry.

Sage assembles a tool payload per chat turn. For partners that have an
explicit allowlist registered in `partner_skills`, the payload is filtered
to those entries before being sent to the LLM. Partners with zero rows
are unrestricted (legacy behaviour) — matches the historical default and
avoids breaking unscoped/test conversations.

Discoverability framing: a partner who only has 'render_map_snapshot',
'get_field_health', 'get_ndvi_stats' in their registry sees exactly those
three tools described in Sage's tool list. That sharper menu is the whole
point — it raises the chance the LLM picks the right tool and lowers the
chance it invents arguments to a tool the partner doesn't actually have.

Public API:
- `fetch_allowed_skills(conn, partner_id) -> Optional[Set[str]]`
  None means unrestricted, a set means filter to exactly this set.
- `filter_tools_payload(payload, allowed)` — applies the allowlist to a
  tool list shaped like the OpenAI tool_calls format.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Set

logger = logging.getLogger("mundi.partner_skills")


async def fetch_allowed_skills(conn, partner_id: Optional[str]) -> Optional[Set[str]]:
    """Return the allowlist set for a partner, or None if unrestricted.

    A partner is 'unrestricted' when:
    - partner_id is None or empty (anonymous / unscoped session)
    - no rows exist for that partner_id in partner_skills (legacy partners)
    """
    if not partner_id:
        return None
    rows = await conn.fetch(
        """
        SELECT skill_name
        FROM partner_skills
        WHERE partner_id = $1 AND enabled = true
        """,
        partner_id,
    )
    if not rows:
        return None
    return {r["skill_name"] for r in rows}


def filter_tools_payload(
    payload: Iterable[dict], allowed: Optional[Set[str]]
) -> List[dict]:
    """Drop any tool whose function.name is not in `allowed`.

    If `allowed` is None, returns the payload unchanged. If a tool has no
    function.name (malformed entry), it's kept — we don't want to silently
    strip oddly-shaped entries the caller added intentionally.
    """
    if allowed is None:
        return list(payload)
    out: List[dict] = []
    for tool in payload:
        name = (tool.get("function") or {}).get("name")
        if name is None or name in allowed:
            out.append(tool)
    return out


async def grant_skill(
    conn,
    partner_id: str,
    skill_name: str,
    *,
    note: Optional[str] = None,
) -> None:
    """Insert or re-enable a skill grant for a partner. Idempotent."""
    await conn.execute(
        """
        INSERT INTO partner_skills (partner_id, skill_name, enabled, note)
        VALUES ($1, $2, true, $3)
        ON CONFLICT (partner_id, skill_name)
        DO UPDATE SET enabled = true, note = COALESCE(EXCLUDED.note, partner_skills.note)
        """,
        partner_id,
        skill_name,
        note,
    )


async def revoke_skill(conn, partner_id: str, skill_name: str) -> None:
    """Disable a skill for a partner. Row is kept for audit."""
    await conn.execute(
        """
        UPDATE partner_skills
        SET enabled = false
        WHERE partner_id = $1 AND skill_name = $2
        """,
        partner_id,
        skill_name,
    )
