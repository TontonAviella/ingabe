"""What people ask Sage about their photos, and the questions learned from it (Roger, 2026-10-25).

A person can ask Sage their own question on a photo (the drone cards' panel, the tower photo screen). Each question is
kept with the industry, the role and who asked (asked_questions), and Sage's model reads it once:
- the same as one of the industry's cards: counted for that card, nothing new to show;
- the same as a question learned before: counted for it;
- a question others could ask about their own photos: rewritten in general words (no names of people, companies,
  places, plots or sites; no numbers, dates or file names) and kept as a learned question (common_questions);
- anything else (a greeting, a test, one person's own account): skipped.
A learned question is shown to everyone in that industry, across companies, once MIN_ASKERS different people asked
it (Roger: "Whole industry, rewritten"); each reader sees SHOWN of them, a different few each day. The words a person
typed are never shown to anyone else and are dropped after RAW_DAYS; the counts stay.
"""

from __future__ import annotations

import json
import logging
import random
import re
from typing import Any, Optional

from src.llm_defaults import thinking_kwargs
from src.services import card_memory, drone_cards, drone_vision, llm_cache, tower_cards
from src.structures import async_conn

logger = logging.getLogger(__name__)

INDUSTRIES = {"agriculture": "drone photos of farms", "telecom": "photos of telecom towers"}
QUESTION_CHARS = 300  # the longest question a person can ask here
LEARNED_WORDS = 15  # a learned question is at most this many words
MIN_ASKERS = 3  # different people who asked it before a learned question is shown to others
SHOWN = 3  # learned questions offered to one reader at a time
RAW_DAYS = 90  # a person's own words are kept this long, for reading the questions again
_LEARNED_SHOWN_TO_MODEL = 100  # the most asked learned questions the model compares a new one with

_PROMPT = """People look at {photos} in Ingabe and ask questions about them. You read one question and say what it \
asks in general, so that other people in the same industry can be offered it.

Answer with one of:
- "card": it asks the same thing as one of the CARDS below; give the card's id.
- "learned": it asks the same thing as one of the LEARNED questions below; give its id.
- "new": a sensible question anyone could ask about their own {photos}. Rewrite it as one short question in plain \
words, under {words} words, ending with "?". Leave out every name of a person, company, place, plot, site or tower, \
and every number, date, id and file name.
- "skip": anything else (a greeting, a test, a question about one person's account, money owed, a complaint, or not \
about the photos).

Give "" for id unless the kind is card or learned, and "" for question unless the kind is new."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["card", "learned", "new", "skip"]},
        "id": {"type": "string"},
        "question": {"type": "string"},
    },
    "required": ["kind", "id", "question"],
    "additionalProperties": False,
}


def card_questions(industry: str) -> dict[str, str]:
    """Each card's question (its general wording) in an industry: card id -> question."""
    if industry == "agriculture":
        return drone_cards.general_questions()
    return tower_cards.general_questions(industry)


def is_general(question: str) -> bool:
    """Whether a rewritten question keeps to the rules: one short question, no number, and no capitalised word
    after the first (a name of a person, company or place) except "I"."""
    words = question.split()
    if not question.endswith("?") or not 2 <= len(words) <= LEARNED_WORDS or re.search(r"\d", question):
        return False
    return all(not w[:1].isupper() or w.strip("?,.'") == "I" for w in words[1:])


async def record(*, user_id: Optional[str], org_id: Optional[str], industry: str, role: Optional[str],
                 layer_id: str, question: str) -> Optional[int]:
    """Keep a question a person asked; its id, to learn from (None when nothing can be learned: no account, or an
    empty question). Words kept past RAW_DAYS are dropped here."""
    text = " ".join(question.split())[:QUESTION_CHARS]
    if industry not in INDUSTRIES:
        raise ValueError(f"industry must be one of {', '.join(INDUSTRIES)}")
    if not user_id or not text:
        return None
    async with async_conn("asked_questions.record", user_id=user_id) as conn:
        await conn.execute(f"UPDATE asked_questions SET question = NULL "
                           f"WHERE question IS NOT NULL AND asked_at < NOW() - INTERVAL '{RAW_DAYS} days'")
        return await conn.fetchval(
            "INSERT INTO asked_questions (user_id, org_id, industry, role, layer_id, question) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            str(user_id), org_id, industry, role, layer_id, text)


async def _read(industry: str, question: str, cards: dict[str, str], learned: dict[int, str]) -> dict[str, Any]:
    client, model = drone_vision.vision_client()
    system = _PROMPT.format(photos=INDUSTRIES[industry], words=LEARNED_WORDS)
    shown = ("CARDS:\n" + "\n".join(f"{k}: {v}" for k, v in cards.items())
             + "\n\nLEARNED:\n" + ("\n".join(f"{k}: {v}" for k, v in learned.items()) or "(none yet)")
             + f"\n\nQUESTION: {question}")

    async def ask() -> dict[str, Any]:
        response = await client.chat.completions.create(
            model=model, **thinking_kwargs(client.base_url, "low"),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": shown}],
            response_format={"type": "json_schema", "json_schema": {"name": "asked_question", "strict": True,
                                                                    "schema": _SCHEMA}},
        )
        llm_cache.record("asked_question", response.usage, model)
        return json.loads(response.choices[0].message.content or "{}")

    reading, _ = await llm_cache.answer("asked_question", llm_cache.key_of(model, system, shown, _SCHEMA), ask)
    return reading


async def learn(asked_id: int) -> str:
    """Read one asked question with the model and file it (card, learned, new or skipped); the status it got.
    A reading that breaks the rules (an unknown id, a rewrite with a name or number) is filed as skipped."""
    async with async_conn("asked_questions.learn") as conn:
        row = await conn.fetchrow("SELECT industry, question FROM asked_questions WHERE id = $1", asked_id)
        if row is None or not row["question"]:
            return "missing"
        learned = {r["id"]: r["question"] for r in await conn.fetch(
            "SELECT c.id, c.question FROM common_questions c LEFT JOIN asked_questions a ON a.common_id = c.id "
            "WHERE c.industry = $1 GROUP BY c.id ORDER BY count(DISTINCT a.user_id) DESC, c.id LIMIT $2",
            row["industry"], _LEARNED_SHOWN_TO_MODEL)}
    cards = card_questions(row["industry"])
    try:
        reading = await _read(row["industry"], row["question"], cards, learned)
    except Exception:  # noqa: BLE001 - kept as failed; the person's own question was already sent to Sage
        logger.warning("asked question %s could not be read", asked_id, exc_info=True)
        status, card_id, common_id, new = "failed", None, None, None
    else:
        status, card_id, common_id, new = _file(reading, cards, learned)
    async with async_conn("asked_questions.learn") as conn:
        if new is not None:
            common_id = await conn.fetchval(
                "INSERT INTO common_questions (industry, question) VALUES ($1, $2) "
                "ON CONFLICT (industry, lower(question)) DO UPDATE SET question = common_questions.question "
                "RETURNING id", row["industry"], new)
        await conn.execute("UPDATE asked_questions SET status = $2, card_id = $3, common_id = $4 WHERE id = $1",
                           asked_id, status, card_id, common_id)
    return status


def _file(reading: dict[str, Any], cards: dict[str, str],
          learned: dict[int, str]) -> tuple[str, Optional[str], Optional[int], Optional[str]]:
    """(status, card id, learned question id, new question) from the model's reading, held to the rules."""
    kind, ref, question = reading.get("kind"), str(reading.get("id") or ""), " ".join(str(reading.get("question") or "").split())
    if kind == "card" and ref in cards:
        return "card", ref, None, None
    if kind == "learned" and ref.isdigit() and int(ref) in learned:
        return "learned", None, int(ref), None
    if kind == "new" and is_general(question):
        return "new", None, None, question
    return "skipped", None, None, None


async def offered(industry: str, reader: str, target: str) -> list[str]:
    """The learned questions offered to one reader on one photo today: SHOWN of those asked by at least MIN_ASKERS
    people in the industry, the most asked the likeliest, a different few each day (card_memory.daily_seed)."""
    async with async_conn("asked_questions.offered") as conn:
        rows = await conn.fetch(
            "SELECT c.question, count(DISTINCT a.user_id) AS askers FROM common_questions c "
            "JOIN asked_questions a ON a.common_id = c.id WHERE c.industry = $1 "
            "GROUP BY c.id HAVING count(DISTINCT a.user_id) >= $2 ORDER BY askers DESC, c.id LIMIT 30",
            industry, MIN_ASKERS)
    pool = [(r["question"], int(r["askers"])) for r in rows]
    pick = random.Random(card_memory.daily_seed(reader, f"asked:{target}"))
    chosen: list[str] = []
    while pool and len(chosen) < SHOWN:
        i = pick.choices(range(len(pool)), weights=[askers for _, askers in pool])[0]
        chosen.append(pool.pop(i)[0])
    return chosen
