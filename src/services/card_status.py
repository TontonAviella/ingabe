"""The vocabulary every set of question cards shares: a card's status values and the shape of its "how sure" part.

The drone cards (src/services/drone_cards.py) and the tower cards (src/services/tower_cards.py) answer different
questions, but a reader's screen styles them the same way, so the status values and the "how sure" shape have one
home here. Each set of cards keeps its own status labels (a drone card "needs a second flight", a tower card "needs
a second inspection").
"""

from __future__ import annotations

from typing import Any, Optional

READY = "ready"
PARTLY = "partly"
NEEDS_FLIGHT = "needs_flight"
NEEDS_CAMERA = "needs_camera"
TO_BUILD = "to_build"
LEARN = "learn"
WORKING = "working"
CANT_TELL = "cant_tell"

_SURE_BARS = {"low": 1, "medium": 2, "high": 3}


def how_sure(level: str, because: list[str], surer: Optional[str]) -> dict[str, Any]:
    """How sure an answer is (low, medium or high), why, and what would make it surer."""
    return {"level": level, "label": level.capitalize(), "bars": _SURE_BARS[level], "because": because,
            "surer": surer}
