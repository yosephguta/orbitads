from __future__ import annotations

import resend
from app.core.config import get_settings

settings = get_settings()
resend.api_key = settings.resend_api_key

FROM = "DealersOrbit <noreply@mail.dealersorbit.com>"


def _send(to: str, subject: str, html: str) -> None:
    """Fire-and-forget wrapper — logs on failure, never raises."""
    try:
        resend.Emails.send({
            "from": FROM,
            "to": [to],
            "subject": subject,
            "html": html,
        })
    except Exception as e:
        print(f"[email] Failed to send '{subject}' to {to}: {e}")


def send_verification_email(to: str, full_name: str, token: str) -> None:
    link = f"https://api.dealersorbit.com/api/v1/auth/verify?token={token}"
    _send(
        to=to,
        subject="Verify your DealersOrbit account",
        html=f"""
        <p>Hi {full_name},</p>
        <p>Welcome to DealersOrbit! Click the button below to verify your email address
        and activate your 7-day free trial.</p>
        <p><a href="{link}" style="
            display:inline-block;
            background:#1a56db;
            color:#fff;
            padding:12px 24px;
            border-radius:6px;
            text-decoration:none;
            font-weight:700;
        ">Verify my email</a></p>
        <p>Or paste this link in your browser:<br>{link}</p>
        <p>If you didn't create a DealersOrbit account, you can ignore this email.</p>
        """,
    )


def send_welcome_email(to: str, full_name: str) -> None:
    _send(
        to=to,
        subject="You're verified — welcome to DealersOrbit!",
        html=f"""
        <p>Hi {full_name},</p>
        <p>Your email is verified and your 7-day free trial has started.</p>
        <p>Install the <strong>DealersOrbit Chrome extension</strong>, import vehicles from your
        inventory, and start generating professional video ads and Facebook listings in minutes.</p>
        <p><a href="https://dealersorbit.com/orbitads/" style="
            display:inline-block;
            background:#1a56db;
            color:#fff;
            padding:12px 24px;
            border-radius:6px;
            text-decoration:none;
            font-weight:700;
        ">Get started</a></p>
        <p>Reply to this email if you have any questions — we're happy to help.</p>
        """,
    )


def send_trial_reminder_email(to: str, full_name: str, days_left: int) -> None:
    _send(
        to=to,
        subject=f"Your DealersOrbit trial ends in {days_left} day{'s' if days_left != 1 else ''}",
        html=f"""
        <p>Hi {full_name},</p>
        <p>Your DealersOrbit free trial ends in <strong>{days_left} day{'s' if days_left != 1 else ''}</strong>.
        Subscribe now to keep generating video ads and Facebook listings without interruption.</p>
        <p><a href="https://dealersorbit.com/orbitads/#pricing" style="
            display:inline-block;
            background:#1a56db;
            color:#fff;
            padding:12px 24px;
            border-radius:6px;
            text-decoration:none;
            font-weight:700;
        ">View plans</a></p>
        """,
    )


def send_payment_failed_email(to: str, full_name: str) -> None:
    _send(
        to=to,
        subject="DealersOrbit — payment failed",
        html=f"""
        <p>Hi {full_name},</p>
        <p>We were unable to process your last payment. Please update your payment method
        to keep your DealersOrbit account active.</p>
        <p><a href="https://api.dealersorbit.com/api/v1/billing/portal" style="
            display:inline-block;
            background:#dc2626;
            color:#fff;
            padding:12px 24px;
            border-radius:6px;
            text-decoration:none;
            font-weight:700;
        ">Update payment method</a></p>
        """,
    )
