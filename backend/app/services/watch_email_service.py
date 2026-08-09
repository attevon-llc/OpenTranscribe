"""Multi-provider email delivery for DB-backed ``EmailNotificationConfig`` rows.

This is the deployment's real mail stack. Watch-source notifications are its
original caller; ``email_service`` now delegates auth mail (password reset,
invitation, verification, security notice) here too whenever a super_admin has
designated a config for it. Providers are selected by
``EmailNotificationConfig.provider``:

  - ``smtp``     — stdlib ``smtplib`` with STARTTLS (587) or implicit SSL (465).
  - ``m365``     — Microsoft Graph ``sendMail`` via an MSAL client-credentials
                   token (tenants with SMTP basic-auth disabled).
  - ``exchange`` — authenticated SMTP submission to an on-prem Exchange server.

Credentials are stored AES-256-GCM encrypted on the config row and decrypted
here at send time. All functions are best-effort and return ``(ok, message)``.
"""

from __future__ import annotations

import contextlib
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from app.utils.encryption import decrypt_api_key

if TYPE_CHECKING:
    from app.models.email_notification_config import EmailNotificationConfig

logger = logging.getLogger(__name__)

_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
_GRAPH_SENDMAIL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"

#: Default socket timeout. Fine for a background scan notification; auth mail
#: passes a shorter one because it blocks the thread servicing a login/reset.
DEFAULT_TIMEOUT = 30


def send_email(
    config: EmailNotificationConfig,
    recipients: list[str],
    subject: str,
    html_body: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Send an HTML email via the config's provider. Returns ``(ok, message)``."""
    if not recipients:
        return False, "No recipients"
    try:
        if config.provider in ("smtp", "exchange"):
            return _send_smtp(config, recipients, subject, html_body, timeout)
        if config.provider == "m365":
            return _send_m365(config, recipients, subject, html_body)
        return False, f"Unknown email provider: {config.provider}"
    except Exception as e:  # noqa: BLE001
        logger.error("Email send failed (%s): %s", config.provider, e)
        return False, str(e)


def test_connection(config: EmailNotificationConfig) -> tuple[bool, str]:
    """Validate the provider connection/auth without sending a real message."""
    try:
        if config.provider in ("smtp", "exchange"):
            return _test_smtp(config)
        if config.provider == "m365":
            return _test_m365(config)
        return False, f"Unknown email provider: {config.provider}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# --------------------------------------------------------------------------- #
# SMTP / Exchange
# --------------------------------------------------------------------------- #
def _smtp_params(
    config: EmailNotificationConfig,
) -> tuple[str | None, int, bool, str | None, str | None]:
    """Return (host, port, use_tls, username, password) for SMTP/Exchange."""
    if config.provider == "exchange":
        host = config.exchange_server
        port = config.smtp_port or 587
        use_tls = True
        username = config.exchange_username
        password = (
            decrypt_api_key(config.encrypted_exchange_password)
            if config.encrypted_exchange_password
            else None
        )
        if config.exchange_domain and username and "\\" not in username:
            username = f"{config.exchange_domain}\\{username}"
    else:
        host = config.smtp_host
        port = config.smtp_port or 587
        use_tls = bool(config.smtp_use_tls)
        username = config.smtp_username
        password = (
            decrypt_api_key(config.encrypted_smtp_password)
            if config.encrypted_smtp_password
            else None
        )
    return host, port, use_tls, username, password


def _smtp_connect(
    host: str, port: int, use_tls: bool, timeout: int = DEFAULT_TIMEOUT
) -> smtplib.SMTP:
    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    smtp = smtplib.SMTP(host, port, timeout=timeout)
    if use_tls:
        smtp.starttls(context=ssl.create_default_context())
    return smtp


def _send_smtp(
    config: EmailNotificationConfig,
    recipients: list[str],
    subject: str,
    html: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    host, port, use_tls, username, password = _smtp_params(config)
    if not host:
        return False, "SMTP host not configured"
    sender = config.from_address or username or "noreply@opentranscribe"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))

    smtp = _smtp_connect(host, port, use_tls, timeout)
    try:
        if username and password:
            smtp.login(username, password)
        smtp.sendmail(sender, recipients, msg.as_string())
    finally:
        with contextlib.suppress(Exception):
            smtp.quit()
    return True, f"Sent to {len(recipients)} recipient(s)"


def _test_smtp(config: EmailNotificationConfig) -> tuple[bool, str]:
    host, port, use_tls, username, password = _smtp_params(config)
    if not host:
        return False, "Host not configured"
    smtp = _smtp_connect(host, port, use_tls)
    try:
        if username and password:
            smtp.login(username, password)
        return True, f"Connected to {host}:{port}"
    finally:
        with contextlib.suppress(Exception):
            smtp.quit()


# --------------------------------------------------------------------------- #
# Microsoft 365 (Graph)
# --------------------------------------------------------------------------- #
def _m365_token(config: EmailNotificationConfig) -> str:
    import msal

    secret = (
        decrypt_api_key(config.encrypted_m365_client_secret)
        if config.encrypted_m365_client_secret
        else None
    )
    app = msal.ConfidentialClientApplication(
        client_id=config.m365_client_id,
        authority=f"https://login.microsoftonline.com/{config.m365_tenant_id}",
        client_credential=secret,
    )
    result = app.acquire_token_for_client(scopes=_GRAPH_SCOPE)
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", "MSAL token acquisition failed"))
    return str(result["access_token"])


def _send_m365(
    config: EmailNotificationConfig, recipients: list[str], subject: str, html: str
) -> tuple[bool, str]:
    import requests

    sender = config.from_address
    if not sender:
        return False, "M365 requires a from_address (the sending mailbox)"
    token = _m365_token(config)
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": a}} for a in recipients],
        },
        "saveToSentItems": False,
    }
    resp = requests.post(
        _GRAPH_SENDMAIL.format(sender=sender),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if resp.status_code in (200, 202):
        return True, f"Sent to {len(recipients)} recipient(s) via Graph"
    return False, f"Graph sendMail failed: {resp.status_code} {resp.text[:200]}"


def _test_m365(config: EmailNotificationConfig) -> tuple[bool, str]:
    _m365_token(config)
    return True, "Acquired Microsoft Graph token"


# --------------------------------------------------------------------------- #
# HTML builder
# --------------------------------------------------------------------------- #
def build_scan_summary_html(source_name: str, summary: dict) -> str:
    """Render a compact HTML scan-summary email body."""
    rows = "".join(
        f"<tr><td style='padding:4px 12px;'>{label}</td>"
        f"<td style='padding:4px 12px;text-align:right;'><b>{value}</b></td></tr>"
        for label, value in (
            ("Files found", summary.get("found", 0)),
            ("Imported", summary.get("imported", 0)),
            ("Skipped", summary.get("skipped", 0)),
            ("Stitched groups", summary.get("stitch_groups", 0)),
            ("Errors", summary.get("errors", 0)),
        )
    )
    return (
        "<div style='font-family:Arial,sans-serif;color:#222;'>"
        "<h2 style='margin:0 0 8px;'>OpenTranscribe — Watch Source Scan</h2>"
        f"<p style='margin:0 0 12px;'>Source: <b>{source_name}</b></p>"
        f"<table style='border-collapse:collapse;'>{rows}</table>"
        "<p style='color:#888;font-size:12px;margin-top:16px;'>"
        "This is an automated notification from OpenTranscribe.</p>"
        "</div>"
    )
