"""Out-of-band delivery workers for Sage-rendered map snapshots.

Senders subscribe to the 'mundi:render_snapshot' Redis channel published by
the render_map_snapshot tool, filter on delivery_channel and partner_id, and
perform the actual send.

Channels (peers — no primary/secondary):
- WhatsApp Business Cloud API (Meta Graph) — dominant channel in Rwanda
- Telegram Bot API — internal/ops/dev alerts

Run a single channel:
    python -m src.senders.whatsapp_sender
    python -m src.senders.telegram_sender

Both default to MUNDI_*_LIVE=0 (log-only stub). Live mode requires per-channel
credentials and is gated by an explicit env-var flip.
"""
