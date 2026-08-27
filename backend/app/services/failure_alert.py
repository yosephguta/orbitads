"""
Failure-rate alerting.

Three decoupled pieces so a future send_alert_sms() can plug in alongside the
email one WITHOUT touching detection logic:

  a) check_failure_rate()  — pure detection. Reads AdEvent, returns
     (breached, rate, sample_size). Knows nothing about email.
  b) send_alert_email()    — pure notification. Sends one email. Knows nothing
     about failure rates. This is the seam for send_alert_sms() later.
  c) run_failure_check()   — the tiny orchestration that wires (a) → (b),
     invoked periodically by cron (same pattern as send_weekly_reports).

Cron (EC2), mirroring the weekly-report entry (see CLAUDE.md):
    */20 * * * * cd ~/orbitads/backend && venv/bin/python -m app.services.failure_alert
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Tuple

import resend
from sqlalchemy import func
from sqlmodel import select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.ad_event import AdEvent

settings = get_settings()


# ── (a) Detection — no email dependency at all ────────────────
async def check_failure_rate(
    window_minutes: int = 60,
    threshold: float = None,
    min_sample_size: int = None,
) -> Tuple[bool, float, int]:
    """
    Compute generation_failed / (generated + generation_failed) over the
    trailing `window_minutes` from AdEvent.

    Returns (breached, rate, sample_size). `breached` is False whenever the
    sample is below `min_sample_size` — a 100% failure rate over 2 events is
    noise, not a signal, so we never fire on it.

    threshold / min_sample_size default to the env-overridable settings.
    """
    if threshold is None:
        threshold = settings.alert_failure_rate_threshold
    if min_sample_size is None:
        min_sample_size = settings.alert_min_sample_size

    # NAIVE UTC cutoff. It's bound as a filter param against ad_events.created_at,
    # which SQLAlchemy casts to TIMESTAMP WITHOUT TIME ZONE — asyncpg cannot
    # encode an AWARE datetime there and raises "can't subtract offset-naive and
    # offset-aware datetimes" (prod-only; dev SQLite tolerates aware). CLAUDE.md
    # bug #24: bind naive datetime.utcnow()-based values for these columns.
    cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)

    async with AsyncSessionLocal() as session:
        generated = (
            await session.exec(
                select(func.count())
                .select_from(AdEvent)
                .where(
                    AdEvent.event_type == "generated",
                    AdEvent.created_at >= cutoff,
                )
            )
        ).one()
        failed = (
            await session.exec(
                select(func.count())
                .select_from(AdEvent)
                .where(
                    AdEvent.event_type == "generation_failed",
                    AdEvent.created_at >= cutoff,
                )
            )
        ).one()

    sample_size = generated + failed
    if sample_size < min_sample_size:
        # Not enough signal — report the rate for visibility but don't breach.
        rate = (failed / sample_size) if sample_size else 0.0
        return (False, round(rate, 4), sample_size)

    rate = failed / sample_size
    breached = rate >= threshold
    return (breached, round(rate, 4), sample_size)


# ── (b) Notification — no failure-rate knowledge at all ───────
def send_alert_email(subject: str, body: str) -> bool:
    """
    Send an internal alert email to mail@dealersorbit.com, prefixing the
    subject with "🚨 ALERT: ". Uses the SAME Resend sender config as the
    dealership-signup notification in auth.py.

    Generic on purpose: it just sends an email. A future send_alert_sms(subject,
    body) plugs in right beside this without any detection code changing.
    Returns True on success, False on failure (never raises — alerting must
    not crash the caller).
    """
    try:
        resend.api_key = settings.resend_api_key
        resend.Emails.send({
            "from":    "DealersOrbit <notifications@mail.dealersorbit.com>",
            "to":      ["mail@dealersorbit.com"],
            "subject": f"🚨 ALERT: {subject}",
            "html":    body,
        })
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[alert] failed to send alert email: {e}")
        return False


# ── (c) Orchestration — wires (a) → (b), run by cron ──────────
async def run_failure_check(window_minutes: int = 60) -> dict:
    """
    Detect, then notify if breached. The only place that knows both halves.
    Returns a summary dict (also handy for the test + logs).
    """
    breached, rate, sample_size = await check_failure_rate(window_minutes=window_minutes)
    result = {
        "breached":        breached,
        "rate":            rate,
        "sample_size":     sample_size,
        "window_minutes":  window_minutes,
        "threshold":       settings.alert_failure_rate_threshold,
        "alert_sent":      False,
    }

    if breached:
        pct = round(rate * 100)
        subject = f"Generation failure rate {pct}% (last {window_minutes} min)"
        body = f"""
        <div style="font-family:-apple-system,sans-serif;max-width:520px;margin:0 auto">
          <h2 style="color:#dc2626">🚨 High generation failure rate</h2>
          <p>The ad-generation pipeline is failing above the alert threshold.</p>
          <table style="width:100%;border-collapse:collapse;margin-top:12px">
            <tr><td style="padding:8px 10px;color:#374151">Failure rate</td>
                <td style="padding:8px 10px;font-weight:700;text-align:right;color:#dc2626">{pct}%</td></tr>
            <tr style="background:#f9fafb"><td style="padding:8px 10px;color:#374151">Threshold</td>
                <td style="padding:8px 10px;text-align:right">{round(settings.alert_failure_rate_threshold*100)}%</td></tr>
            <tr><td style="padding:8px 10px;color:#374151">Sample size</td>
                <td style="padding:8px 10px;text-align:right">{sample_size} generations</td></tr>
            <tr style="background:#f9fafb"><td style="padding:8px 10px;color:#374151">Window</td>
                <td style="padding:8px 10px;text-align:right">last {window_minutes} min</td></tr>
          </table>
          <p style="margin-top:16px;color:#6b7280;font-size:13px">
            Check the backend logs (<code>journalctl -u orbitads</code>) and Shotstack/ElevenLabs status.
          </p>
        </div>
        """
        result["alert_sent"] = send_alert_email(subject, body)

    print(f"[alert] failure check: {result}")
    return result


if __name__ == "__main__":
    asyncio.run(run_failure_check())
