"""Ratings of Sage's answers: a person's 👍 or 👎 on one answer, with an optional note on what was wrong.

One rating per person and answer (rating again replaces it). Only Sage's own answers on the map they belong to can
be rated. The Ingabe Health page shows the counts and each rated answer next to the turn that wrote it (the Sage turn
log, src/services/sage_turn_log.py): its steps, rules and errors, so a 👎 can be traced to the tool or step behind it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from src.services import sage_turn_log
from src.structures import async_conn

RATINGS = ("up", "down")
COMMENT_CHARS = 1000
_ANSWER_CHARS = 200


async def save(*, map_id: str, message_id: int, user_id: str, rating: str, comment: Optional[str]) -> bool:
    """Store a rating. False when that message is not one of Sage's answers on that map."""
    if rating not in RATINGS:
        raise ValueError(f"rating must be one of {RATINGS}")
    note = (comment or "").strip()[:COMMENT_CHARS] or None
    async with async_conn("sage_answer_ratings.save", user_id=user_id) as conn:
        message = await conn.fetchrow(
            "SELECT conversation_id, message_json->>'role' AS role FROM chat_completion_messages "
            "WHERE id = $1 AND map_id = $2", message_id, map_id)
        if message is None or message["role"] != "assistant":
            return False
        await conn.execute(
            """
            INSERT INTO sage_answer_ratings (message_id, conversation_id, map_id, user_id, rating, comment)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (message_id, user_id)
            DO UPDATE SET rating = EXCLUDED.rating, comment = EXCLUDED.comment, updated_at = NOW()
            """,
            message_id, message["conversation_id"], map_id, str(user_id), rating, note)
    return True


def _first_line(text: Any) -> Optional[str]:
    if not text:
        return None
    line = next((ln.strip(" *#") for ln in str(text).splitlines() if ln.strip()), "")
    return line if len(line) <= _ANSWER_CHARS else line[: _ANSWER_CHARS - 1] + "…"


async def recent(days: int = 7) -> dict[str, Any]:
    """Counts of 👍 and 👎 over `days`, and each rated answer (👎 first, newest first) with the turn that wrote it."""
    async with async_conn("sage_answer_ratings.recent") as conn:
        rows = await conn.fetch(
            """
            SELECT r.rating, r.comment, r.updated_at, r.map_id, r.conversation_id, m.created_at AS answered_at,
                   m.message_json->>'content' AS answer, p.project_id, t.record::text AS turn
            FROM sage_answer_ratings r
            JOIN chat_completion_messages m ON m.id = r.message_id
            LEFT JOIN user_mundiai_maps p ON p.id = r.map_id
            LEFT JOIN LATERAL (
                SELECT record FROM sage_turn_log
                WHERE conversation_id = r.conversation_id::text AND started_at <= m.created_at
                ORDER BY started_at DESC LIMIT 1
            ) t ON true
            WHERE r.updated_at > now() - make_interval(days => $1)
            ORDER BY (r.rating = 'down') DESC, r.updated_at DESC
            LIMIT 200
            """, days)
        counts = {r["rating"]: r["n"] for r in await conn.fetch(
            "SELECT rating, count(*) AS n FROM sage_answer_ratings WHERE updated_at > now() - make_interval(days => $1) "
            "GROUP BY rating", days)}
    rated = []
    for r in rows:
        turn = sage_turn_log.turn_row(json.loads(r["turn"])) if r["turn"] else {}
        rated.append({
            "rated_at": r["updated_at"].isoformat(timespec="seconds"), "rating": r["rating"], "comment": r["comment"],
            "answer": _first_line(r["answer"]), "answered_at": r["answered_at"].isoformat(timespec="seconds"),
            "map_id": r["map_id"], "project_id": r["project_id"], "conversation_id": r["conversation_id"],
            "steps": turn.get("steps"), "rules": turn.get("rules"), "error": turn.get("error"),
            "turn_seconds": turn.get("seconds"), "trace_id": turn.get("trace_id"),
        })
    return {"up": counts.get("up", 0), "down": counts.get("down", 0), "rated": rated}
