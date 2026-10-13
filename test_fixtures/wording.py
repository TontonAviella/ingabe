"""Wording the app never shows anyone (Roger, 2026-10-11): it works with whatever data arrives and says in data terms
what that data supports; it never gives flight, pilot or data-collection instructions. Tests of user-facing text
search it with COLLECTION_INSTRUCTIONS."""

import re

COLLECTION_INSTRUCTIONS = re.compile(
    r"\b(next visit|fly|flying|re-fly|pilot|take (more )?photos|use a drone|shoot again)\b", re.I)
