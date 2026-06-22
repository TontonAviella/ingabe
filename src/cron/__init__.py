"""Phase 3: Proactive Sage — cron-fired alerts.

Workers in this package wake on a schedule, render+publish snapshot payloads
to Redis, and let the existing WhatsApp/Telegram senders deliver them. No
LLM is in the loop: recurring reports are deterministic and auditable.

Run a single worker:
    python -m src.cron.sage_alerts

The worker reads `alert_subscriptions` rows whose next_fire_at <= now AND
enabled = true, fires each one, then advances next_fire_at via the cron
expression. Failures (render error, redis publish failure) are logged but
do NOT roll back next_fire_at — we'd rather skip an alert than spam.
"""
