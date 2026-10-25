"""asked_questions: what a learned question may say, and how the model's reading is held to the rules."""

import pytest

from src.services import asked_questions


@pytest.mark.parametrize("question, ok", [
    ("Which antennas need fixing?", True),
    ("What will I find up there?", True),
    ("Is my maize ready to harvest?", True),
    ("Which antennas did Airtel add?", False),  # a company's name
    ("How many dishes on tower 12?", False),  # a number
    ("Is plot Kabarama dry?", False),  # a place
    ("Which plots are dry", False),  # not a question
    ("Why?", False),  # too short
    ("What " + "very " * 20 + "long question is this?", False),  # too long
])
def test_a_learned_question_names_nobody_and_counts_nothing(question, ok):
    assert asked_questions.is_general(question) is ok


def test_the_models_reading_is_held_to_the_rules():
    cards = {"equipment": "What is on the tower?"}
    learned = {7: "Which antennas need fixing?"}
    assert asked_questions._file({"kind": "card", "id": "equipment", "question": ""}, cards, learned) == (
        "card", "equipment", None, None)
    assert asked_questions._file({"kind": "learned", "id": "7", "question": ""}, cards, learned) == (
        "learned", None, 7, None)
    assert asked_questions._file({"kind": "new", "id": "", "question": "Is the  tower leaning?"}, cards, learned) == (
        "new", None, None, "Is the tower leaning?")
    # an id the model made up, or a rewrite that still names a company, is skipped
    assert asked_questions._file({"kind": "card", "id": "bolts", "question": ""}, cards, learned)[0] == "skipped"
    assert asked_questions._file({"kind": "learned", "id": "99", "question": ""}, cards, learned)[0] == "skipped"
    assert asked_questions._file({"kind": "new", "id": "", "question": "Did MTN fix it?"}, cards, learned)[0] == "skipped"
    assert asked_questions._file({}, cards, learned)[0] == "skipped"


@pytest.mark.parametrize("industry", list(asked_questions.INDUSTRIES))
def test_every_industry_has_its_card_questions(industry):
    questions = asked_questions.card_questions(industry)
    assert questions and all(q.endswith("?") for q in questions.values())


@pytest.mark.anyio
async def test_a_learned_question_is_offered_only_once_three_people_asked_it(monkeypatch) -> None:
    import uuid

    from src.database.pool import get_async_db_connection

    tag = "".join(chr(ord("a") + int(c, 16) % 26) for c in uuid.uuid4().hex[:8])  # letters only: no digits allowed
    general = f"Is the {tag} leaning?"

    async def reading(industry, question, cards, learned):
        return {"kind": "new", "id": "", "question": general}

    monkeypatch.setattr(asked_questions, "_read", reading)
    users = [f"user-{tag}-{n}" for n in range(3)]
    try:
        for i, user in enumerate(users):
            asked_id = await asked_questions.record(user_id=user, org_id=None, industry="power_grid", role="owner",
                                                    layer_id="Ltest", question=f"Is pylon {i} at Rubavu leaning?")
            assert await asked_questions.learn(asked_id) == "new"
            offered = await asked_questions.offered("power_grid", "reader", "Ltest")
            assert (general in offered) is (i == 2)  # two people are not enough; the third makes it common
        async with get_async_db_connection() as conn:
            kept = await conn.fetch("SELECT question, common_id FROM asked_questions WHERE user_id = ANY($1)", users)
        assert len({r["common_id"] for r in kept}) == 1  # the same learned question for all three
        assert all("Rubavu" in r["question"] for r in kept)  # their own words stay theirs, never offered
        assert await asked_questions.record(user_id=None, org_id=None, industry="power_grid", role=None,
                                            layer_id="Ltest", question="Anything?") is None
    finally:
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM asked_questions WHERE user_id = ANY($1)", users)
            await conn.execute("DELETE FROM common_questions WHERE question = $1", general)
