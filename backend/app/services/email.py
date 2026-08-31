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
    first_name = (full_name or "").split(" ")[0] or full_name
    # Link to the on-site player page (HTML5 <video>) so it plays in-browser
    # instead of downloading the raw file.
    tutorial_url = "https://dealersorbit.com/tutorial"
    chrome_url = "https://chromewebstore.google.com/detail/dealersorbit/kmlogmgghaohcanigabfccbnpepemglb"
    edge_url = "https://microsoftedge.microsoft.com/addons/detail/dealersorbit/fjmgbihljfmkgpnlkjjokahcpmnlfhgi"
    _send(
        to=to,
        subject="You're all set! Welcome to DealersOrbit 🎉",
        html=f"""
        <div style="font-family:-apple-system,sans-serif;max-width:520px;margin:0 auto;padding:20px">
          <h1 style="color:#111827">Welcome to DealersOrbit, {first_name}!</h1>
          <p style="color:#6b7280;line-height:1.6">
            Your account is verified and your 7-day free trial has started. Here's how to get going:
          </p>

          <div style="background:#f9fafb;border-radius:12px;padding:20px;margin:20px 0">
            <h3 style="margin-top:0">📹 Watch the quick tutorial</h3>
            <a href="{tutorial_url}" style="display:inline-block;background:#1a56db;color:white;padding:10px 20px;
                      border-radius:8px;text-decoration:none;font-weight:700">
              ▶️ Watch Now
            </a>
          </div>

          <div style="margin:24px 0">
            <h3>1. Install the extension</h3>
            <p style="color:#6b7280">
              <a href="{chrome_url}">Chrome</a>
              &nbsp;·&nbsp;
              <a href="{edge_url}">Edge</a>
            </p>

            <h3>2. Sign in</h3>
            <p style="color:#6b7280">Use the email and password you just created.</p>

            <h3>3. Import your first vehicle</h3>
            <p style="color:#6b7280">
              Search "[your dealership] Cars.com" or "CarGurus" on Google, open your dealership's
              page, and click Import on any vehicle.
            </p>

            <h3>4. Generate your first ad</h3>
            <p style="color:#6b7280">
              Click Generate Ad, pick a theme, and watch DealersOrbit build your video in under a minute.
            </p>
          </div>

          <p style="color:#9ca3af;font-size:12px">
            Questions? Just reply to this email or reach us at mail@dealersorbit.com
          </p>
        </div>
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


def send_dealer_config_ready_email(to: str, full_name: str, domain: str) -> None:
    """
    Sent when an admin approves a dealer's site config (Part 5). The extension
    fetches the active config fresh from the backend each time it loads a page
    (GET /dealer-configs/domain/{domain}), so a page refresh is enough — no
    re-login required in the normal case. We still mention sign-out as a fallback
    in case a stale cached `user` blocks the domain-restriction check.
    """
    first_name = (full_name or "").split(" ")[0] or (full_name or "there")
    _send(
        to=to,
        subject="Your DealersOrbit dealer site config is ready! 🎉",
        html=f"""
        <div style="font-family:-apple-system,sans-serif;max-width:520px;margin:0 auto;padding:20px">
          <h1 style="color:#111827">You're all set, {first_name}!</h1>
          <p style="color:#374151;line-height:1.6">
            Good news — your dealership site
            <strong>{domain}</strong> is now supported by DealersOrbit. You can
            import vehicles straight from your own inventory pages.
          </p>
          <div style="background:#f9fafb;border-radius:12px;padding:20px;margin:20px 0;color:#374151;line-height:1.6">
            <h3 style="margin-top:0">How to start using it</h3>
            <ol style="padding-left:18px;margin:0">
              <li>Open your dealership inventory page in your browser.</li>
              <li><strong>Refresh the page</strong> — the DealersOrbit <em>Import</em> buttons
                  will appear on each vehicle.</li>
              <li>Don't see them right away? Sign out of the DealersOrbit extension and
                  sign back in, then refresh the page.</li>
            </ol>
          </div>
          <p style="color:#6b7280;font-size:14px">
            Questions? Just reply to this email or use the Help panel in the extension.
          </p>
        </div>
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
