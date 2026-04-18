"""Feature flags for Brain ingestion — runtime gates for in-flight capabilities.

BRAIN_PARTNER_INTERNAL_ENABLED:
  Default off. When off, two write-path sites refuse partner_internal traffic:
    1. Scheduler — skips sources whose config resolves to access_scope=
       partner_internal, so no partner-owned URL is ever fetched.
    2. Normalizer write_page — refuses to persist a FetchedContent with
       access_scope=partner_internal.

  Retrieval is NOT gated by this flag. Instead, the app.partner_id GUC
  and _PARTNER_FILTER in brain_service.py handle read-path isolation
  (defense-in-depth alongside RLS policies).

  Gate defaults to OFF. Partner onboarding flips it on only after the
  Phase 0 hard gates (child-table RLS, session GUC wiring, scheduler/CLI
  audit, integration tests) are green.
"""

from __future__ import annotations

import os


def partner_internal_enabled() -> bool:
    """Return True iff BRAIN_PARTNER_INTERNAL_ENABLED=true in env.

    Default: False. Any value other than the literal string "true"
    (case-insensitive) is treated as off.
    """
    return os.environ.get("BRAIN_PARTNER_INTERNAL_ENABLED", "false").lower() == "true"
