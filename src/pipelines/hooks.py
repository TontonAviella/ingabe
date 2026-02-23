# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Dagster hooks for pipeline failure notifications.

Sends alerts when pipeline runs fail. Notifications go to:
1. Structured logs (always) — visible in Render logs and Grafana via OTEL
2. Email via aiosmtplib (if ALERT_EMAIL_TO is set)

Monitor failures in Grafana by filtering OTEL logs for:
  severity=ERROR AND dagster.run.status=FAILURE
"""

import logging
import os

from dagster import HookContext, failure_hook, success_hook

logger = logging.getLogger(__name__)


@failure_hook
def notify_on_failure(context: HookContext):
    """Log structured failure alert for Grafana/OTEL ingestion."""
    run_id = context.run_id
    job_name = context.op_exception  # will be None at job level

    # Structured log entry — picked up by OTEL exporter → Grafana
    logger.error(
        "PIPELINE_FAILURE | job=%s | run_id=%s | op=%s | error=%s",
        context.job_name,
        run_id,
        context.op.name if context.op else "N/A",
        str(context.op_exception)[:500] if context.op_exception else "unknown",
        extra={
            "dagster.run.id": run_id,
            "dagster.run.status": "FAILURE",
            "dagster.job.name": context.job_name,
            "alert": True,
        },
    )

    # Optional: send email alert
    email_to = os.environ.get("ALERT_EMAIL_TO")
    if email_to:
        _send_email_alert(context, email_to)


@success_hook
def log_on_success(context: HookContext):
    """Log structured success for Grafana dashboards."""
    logger.info(
        "PIPELINE_SUCCESS | job=%s | run_id=%s | op=%s",
        context.job_name,
        context.run_id,
        context.op.name if context.op else "N/A",
        extra={
            "dagster.run.id": context.run_id,
            "dagster.run.status": "SUCCESS",
            "dagster.job.name": context.job_name,
        },
    )


def _send_email_alert(context: HookContext, email_to: str):
    """Best-effort email alert (non-blocking)."""
    try:
        import smtplib
        from email.mime.text import MIMEText

        smtp_host = os.environ.get("SMTP_HOST", "")
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        smtp_user = os.environ.get("SMTP_USER", "")
        smtp_pass = os.environ.get("SMTP_PASS", "")

        if not smtp_host:
            logger.warning("ALERT_EMAIL_TO set but SMTP_HOST not configured")
            return

        subject = f"[Mundi.ai] Pipeline FAILED: {context.job_name}"
        body = (
            f"Job: {context.job_name}\n"
            f"Run ID: {context.run_id}\n"
            f"Op: {context.op.name if context.op else 'N/A'}\n"
            f"Error: {context.op_exception}\n"
        )

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = smtp_user or "alerts@mundi.ai"
        msg["To"] = email_to

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if smtp_user:
                server.starttls()
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        logger.info("Failure alert email sent to %s", email_to)
    except Exception as e:
        logger.warning("Failed to send alert email: %s", e)
