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

import base64
import contextlib
import html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate
from email.utils import make_msgid
from email.utils import parseaddr
from typing import TYPE_CHECKING
from urllib.parse import quote

from app.utils.encryption import SecretDecryptionError
from app.utils.encryption import decrypt_api_key
from app.utils.smtp_transport import SMTP_DEFAULT_PORT
from app.utils.smtp_transport import SMTP_IMPLICIT_SSL_PORT
from app.utils.smtp_transport import smtp_transport_problem

if TYPE_CHECKING:
    from app.models.email_notification_config import EmailNotificationConfig

logger = logging.getLogger(__name__)

_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
_GRAPH_SENDMAIL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"

#: Default socket timeout. Fine for a background scan notification; auth mail
#: passes a shorter one because it blocks the thread servicing a login/reset.
DEFAULT_TIMEOUT = 30


def build_message(
    sender: str, recipient: str, subject: str, html_body: str, text_body: str | None = None
) -> EmailMessage:
    """Build one outbound message addressed to a single recipient.

    ``multipart/alternative`` (plain text first, HTML second) when a text body is given,
    otherwise a single HTML part. ``Date`` and a ``Message-ID`` in the sender's domain
    are always set; filters score their absence as spam.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg["Message-ID"] = make_msgid(domain=parseaddr(sender)[1].rpartition("@")[2] or None)
    if text_body is None:
        msg.set_content(html_body, subtype="html")
    else:
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
    return msg


def send_email(
    config: EmailNotificationConfig,
    recipients: list[str],
    subject: str,
    html_body: str,
    timeout: int = DEFAULT_TIMEOUT,
    text_body: str | None = None,
) -> tuple[bool, str]:
    """Send an email via the config's provider. Returns ``(ok, message)``.

    Each recipient gets their own message, addressed only to them, so no recipient sees
    the others and one refused address does not stop the rest.
    """
    if not recipients:
        return False, "No recipients"
    try:
        if config.provider in ("smtp", "exchange"):
            return _send_smtp(config, recipients, subject, html_body, text_body, timeout)
        if config.provider == "m365":
            return _send_m365(config, recipients, subject, html_body, text_body)
        return False, f"Unknown email provider: {config.provider}"
    except (smtplib.SMTPException, OSError, ValueError, SecretDecryptionError) as e:
        logger.exception("Email send failed (%s): %s", config.provider, e)
        return False, str(e)


def test_connection(config: EmailNotificationConfig) -> tuple[bool, str]:
    """Validate the provider connection/auth without sending a real message."""
    try:
        if config.provider in ("smtp", "exchange"):
            return _test_smtp(config)
        if config.provider == "m365":
            return _test_m365(config)
        return False, f"Unknown email provider: {config.provider}"
    except (smtplib.SMTPException, OSError, ValueError, SecretDecryptionError) as e:
        # Returned as EmailTestResponse.message AND persisted to
        # cfg.test_message (watch_sources_email_configs.py) -- an smtplib/msal exception can
        # quote the server/credential, so only the class of failure survives.
        logger.exception("Email connection test failed (%s)", config.provider)
        return False, f"Connection failed ({type(e).__name__})"


# --------------------------------------------------------------------------- #
# SMTP / Exchange
# --------------------------------------------------------------------------- #
def _smtp_params(
    config: EmailNotificationConfig,
) -> tuple[str | None, int, bool, str | None, str | None]:
    """Return (host, port, use_tls, username, password) for SMTP/Exchange."""
    if config.provider == "exchange":
        host = config.exchange_server
        port = config.smtp_port or SMTP_DEFAULT_PORT
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
        port = config.smtp_port or SMTP_DEFAULT_PORT
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
    if port == SMTP_IMPLICIT_SSL_PORT:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    smtp = smtplib.SMTP(host, port, timeout=timeout)
    if use_tls:
        smtp.starttls(context=ssl.create_default_context())
    return smtp


def _send_smtp(
    config: EmailNotificationConfig,
    recipients: list[str],
    subject: str,
    html_body: str,
    text_body: str | None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    host, port, use_tls, username, password = _smtp_params(config)
    if not host:
        return False, "SMTP host not configured"
    problem = smtp_transport_problem(host, port, use_tls, bool(username and password))
    if problem:
        return False, problem
    sender = config.from_address or username or "noreply@opentranscribe"
    envelope_sender = parseaddr(sender)[1] or sender

    smtp = _smtp_connect(host, port, use_tls, timeout)
    refused = 0
    try:
        if username and password:
            smtp.login(username, password)
        for recipient in recipients:
            msg = build_message(sender, recipient, subject, html_body, text_body)
            try:
                smtp.send_message(msg, from_addr=envelope_sender, to_addrs=[recipient])
            except smtplib.SMTPRecipientsRefused:
                refused += 1
    finally:
        with contextlib.suppress(Exception):
            smtp.quit()
    if refused:
        return False, f"{refused} of {len(recipients)} recipient(s) were refused"
    return True, f"Sent to {len(recipients)} recipient(s)"


def _test_smtp(config: EmailNotificationConfig) -> tuple[bool, str]:
    host, port, use_tls, username, password = _smtp_params(config)
    if not host:
        return False, "Host not configured"
    problem = smtp_transport_problem(host, port, use_tls, bool(username and password))
    if problem:
        return False, problem
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
    config: EmailNotificationConfig,
    recipients: list[str],
    subject: str,
    html_body: str,
    text_body: str | None,
) -> tuple[bool, str]:
    import requests

    sender = config.from_address
    if not sender:
        return False, "M365 requires a from_address (the sending mailbox)"
    token = _m365_token(config)
    # Graph's JSON message cannot carry an alternative text part or our own Message-ID, so
    # each message goes as MIME (documented: POST sendMail with Content-Type text/plain and a
    # base64-encoded MIME body). The sender goes in the URL path and is percent-encoded.
    url = _GRAPH_SENDMAIL.format(sender=quote(sender, safe=""))
    rejected = 0
    last_status = 0
    for recipient in recipients:
        msg = build_message(sender, recipient, subject, html_body, text_body)
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
            data=base64.b64encode(msg.as_bytes()),
            timeout=30,
        )
        if resp.status_code not in (200, 202):
            # Status only: the response body can quote recipients and tenant details.
            logger.warning("Microsoft Graph sendMail was rejected: HTTP %s", resp.status_code)
            rejected += 1
            last_status = resp.status_code
    if rejected:
        return (
            False,
            f"Graph sendMail failed for {rejected} of {len(recipients)}: HTTP {last_status}",
        )
    return True, f"Sent to {len(recipients)} recipient(s) via Graph"


def _test_m365(config: EmailNotificationConfig) -> tuple[bool, str]:
    _m365_token(config)
    return True, "Acquired Microsoft Graph token"


# --------------------------------------------------------------------------- #
# HTML builder
# --------------------------------------------------------------------------- #
def build_scan_summary_text(source_name: str, summary: dict) -> str:
    """Render the plain-text alternative to :func:`build_scan_summary_html`."""
    lines = [
        "OpenTranscribe - Watch Source Scan",
        f"Source: {source_name}",
        "",
        f"Files found: {summary.get('found', 0)}",
        f"Imported: {summary.get('imported', 0)}",
        f"Skipped: {summary.get('skipped', 0)}",
        f"Stitched groups: {summary.get('stitch_groups', 0)}",
        f"Errors: {summary.get('errors', 0)}",
        "",
        "This is an automated notification from OpenTranscribe.",
    ]
    return "\n".join(lines)


def build_scan_summary_html(source_name: str, summary: dict) -> str:
    """Render a compact HTML scan-summary email body."""
    rows = "".join(
        f"<tr><td style='padding:4px 12px;'>{html.escape(label)}</td>"
        f"<td style='padding:4px 12px;text-align:right;'><b>{html.escape(str(value))}</b></td></tr>"
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
        f"<p style='margin:0 0 12px;'>Source: <b>{html.escape(source_name)}</b></p>"
        f"<table style='border-collapse:collapse;'>{rows}</table>"
        "<p style='color:#888;font-size:12px;margin-top:16px;'>"
        "This is an automated notification from OpenTranscribe.</p>"
        "</div>"
    )
