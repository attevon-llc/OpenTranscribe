"""Transactional auth email: password reset, invitation, verification, security notice.

Every credential-bearing message in the product is sent from here, so this module
delegates to the deployment's real mail configuration rather than carrying its own
minimal one. Transport resolution, per send:

1. **The designated ``EmailNotificationConfig``** — the DB-backed, admin-managed,
   AES-256-GCM-encrypted provider stack (``smtp`` / ``m365`` Graph OAuth2 /
   ``exchange``) already used by watch-source notifications, driven through
   :mod:`app.services.watch_email_service`. Which row carries auth mail is a
   deliberate super_admin act recorded in the ``SystemSettings`` key
   ``email.auth_config_uuid`` — never "whichever config exists", because those
   rows are created for specific notification purposes and mailing password
   resets out of an unrelated mailbox is both a credential leak and a
   deliverability problem.
2. **Env SMTP** (``SMTP_HOST`` …) when no config is designated. Supports STARTTLS
   *and* implicit SSL on port 465, matching the DB path.
3. **Nothing** — a sensitive send then fails loudly instead of pretending.

Failure posture: a send that did not happen raises :class:`EmailDeliveryError`.
Silently returning is what let a deployment with no ``SMTP_HOST`` (the default in
all 23 compose files) tell users "check your email" forever. The exception message
is scrubbed of email addresses so it stays safe to surface — see
``send_password_reset`` for the anti-enumeration constraint its caller carries.
"""

from __future__ import annotations

import contextlib
import logging
import re
import smtplib
import ssl
from collections.abc import Callable
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from app.auth.utils import mask_identifier as _mask_email
from app.core.config import settings
from app.core.constants import AUTH_EMAIL_CONFIG_SETTING_KEY
from app.core.constants import DEFAULT_AUTH_EMAIL_CONFIG_UUID
from app.core.constants import DEFAULT_FRONTEND_URL
from app.core.exceptions import EmailDeliveryError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.email_notification_config import EmailNotificationConfig

logger = logging.getLogger(__name__)

#: Socket timeout for every SMTP operation. Without one, an unreachable MTA
#: blocks the worker thread that is servicing a login/reset request.
SMTP_TIMEOUT = 10

#: Implicit-SSL submission port. Unlike STARTTLS the session is wrapped in TLS
#: from the first byte, so it needs SMTP_SSL rather than SMTP + starttls().
SMTP_IMPLICIT_SSL_PORT = 465

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")


#: Re-exported so the sender and its callers share one import site. The class
#: lives in ``core/exceptions`` with the rest of the hierarchy because
#: ``main.py`` maps it to a 503 — an undeliverable message is a service failure,
#: not a bug in the request.
__all__ = ["EmailDeliveryError", "EmailService", "email_service", "load_auth_mail_config"]


def _scrub(detail: str) -> str:
    """Mask any email address embedded in a provider/SMTP error string.

    ``SMTPRecipientsRefused`` and Graph error bodies quote the recipient back at
    us, which would put a real address into a log line or a 5xx body.
    """
    return _EMAIL_RE.sub(lambda m: _mask_email(m.group(0)), detail)


def load_auth_mail_config(db: Session) -> EmailNotificationConfig | None:
    """Return the ``EmailNotificationConfig`` designated to carry auth mail.

    The designation is a ``SystemSettings`` key rather than a column on the row:
    it needs no migration, it is admin-editable with no restart (the convention
    the rest of this codebase's DB-backed settings follow), and it keeps "this
    provider exists" separate from "this provider speaks for the deployment".

    Args:
        db: Open session; not committed or closed here.

    Returns:
        The designated, enabled config, or ``None`` to fall back to env SMTP.
    """
    from app.models.email_notification_config import EmailNotificationConfig
    from app.services.system_settings_service import get_setting

    designated = get_setting(db, AUTH_EMAIL_CONFIG_SETTING_KEY, DEFAULT_AUTH_EMAIL_CONFIG_UUID)
    if not designated:
        return None

    config = (
        db.query(EmailNotificationConfig).filter(EmailNotificationConfig.uuid == designated).first()
    )
    if config is None:
        logger.error(
            "%s designates email config %s, which does not exist — falling back to env SMTP.",
            AUTH_EMAIL_CONFIG_SETTING_KEY,
            designated,
        )
        return None
    if not config.is_enabled:
        logger.error(
            "Auth mail config %r is disabled — falling back to env SMTP.",
            config.name,
        )
        return None
    return config


def _shell(heading: str, inner: str) -> str:
    """Render the shared transactional-email HTML wrapper around ``inner``."""
    return f"""
        <html>
        <body style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
            <h2>{heading}</h2>
            {inner}
            <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 24px 0;">
            <p style="color: #6b7280; font-size: 12px;">
                This is an automated message from {settings.PROJECT_NAME}.
            </p>
        </body>
        </html>
        """


def _button(url: str, label: str) -> str:
    """Render the call-to-action button carrying a single-use link."""
    return (
        f'<p><a href="{url}" style="display: inline-block; padding: 12px 24px; '
        "background-color: #4f46e5; color: white; text-decoration: none; "
        f'border-radius: 6px;">{label}</a></p>'
    )


class EmailService:
    """Sends transactional auth email through the deployment's mail transport.

    Args:
        session_factory: Callable returning a DB session, used to resolve the
            designated mail config. Defaults to the app's ``SessionLocal``;
            injectable so the send path is testable without a database.
    """

    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        self._session_factory = session_factory

    def send_password_reset(self, to_email: str, reset_url: str) -> None:
        """Send a password reset email.

        Args:
            to_email: Recipient email address.
            reset_url: Full URL with token for the password reset page.

        Raises:
            EmailDeliveryError: The message was not handed to a transport. The
                error names no address, but note that this call is only reached
                for an account that exists — the caller
                (``auth/password_reset.request_password_reset``) owns keeping the
                HTTP response uniform so a mail outage cannot become an
                account-existence oracle.
        """
        inner = (
            f"<p>You requested a password reset for your {settings.PROJECT_NAME} account.</p>"
            "<p>Click the link below to reset your password. This link expires in 1 hour.</p>"
            + _button(reset_url, "Reset Password")
            + "<p>If you didn't request this, you can safely ignore this email.</p>"
        )
        text_body = (
            f"Password Reset Request\n\n"
            f"You requested a password reset for your {settings.PROJECT_NAME} account.\n\n"
            f"Visit this link to reset your password (expires in 1 hour):\n{reset_url}\n\n"
            f"If you didn't request this, you can safely ignore this email."
        )
        self._send_email(
            to_email,
            f"{settings.PROJECT_NAME} - Password Reset Request",
            _shell("Password Reset Request", inner),
            text_body,
            sensitive=True,
            credential_link=reset_url,
        )

    def send_invitation(
        self,
        to_email: str,
        accept_url: str,
        inviter: str,
        expires_in_hours: int,
        requires_password: bool,
    ) -> None:
        """Send an admin invitation to create an account.

        Args:
            to_email: Address the admin invited.
            accept_url: Full URL carrying the single-use invite token.
            inviter: Email of the admin who issued it, so the recipient can tell
                an expected invitation from a phishing attempt.
            expires_in_hours: Link lifetime, stated so an expired link is
                recognisable as expired rather than broken.
            requires_password: False for an LDAP/OIDC/PKI invitation — those
                accounts have no local password and the page bounces to the IdP.

        Raises:
            EmailDeliveryError: The invitation was not handed to a transport.
                The admin who issued it must learn that it never went out.
        """
        credential_line = (
            "You'll choose your own password on that page."
            if requires_password
            else "You'll sign in with your organization's identity provider — "
            "no password is stored here."
        )
        inner = (
            f"<p>{inviter} invited you to create an account.</p>"
            f"<p>{credential_line} This link expires in {expires_in_hours} hours and "
            "can only be used once.</p>"
            + _button(accept_url, "Accept Invitation")
            + "<p>If you weren't expecting this, you can safely ignore this email.</p>"
        )
        text_body = (
            f"You've been invited to {settings.PROJECT_NAME}\n\n"
            f"{inviter} invited you to create an account.\n"
            f"{credential_line}\n\n"
            f"Accept the invitation (expires in {expires_in_hours} hours, single use):\n"
            f"{accept_url}\n\n"
            "If you weren't expecting this, you can safely ignore this email."
        )
        # The URL is a single-use credential — never logged. See _send_email.
        self._send_email(
            to_email,
            f"{settings.PROJECT_NAME} - You've been invited",
            _shell(f"You've been invited to {settings.PROJECT_NAME}", inner),
            text_body,
            sensitive=True,
            credential_link=accept_url,
        )

    def send_email_verification(
        self, to_email: str, verify_url: str, expires_in_hours: int
    ) -> None:
        """Send an address-verification link.

        Args:
            to_email: Address to prove control of.
            verify_url: Full URL carrying the single-use verification token.
            expires_in_hours: Link lifetime.

        Raises:
            EmailDeliveryError: The message was not handed to a transport.
        """
        inner = (
            "<p>Confirm this address to finish setting up your "
            f"{settings.PROJECT_NAME} account.</p>"
            f"<p>This link expires in {expires_in_hours} hours.</p>"
            + _button(verify_url, "Verify Email")
            + "<p>If you didn't create this account, you can safely ignore this email.</p>"
        )
        text_body = (
            f"Verify your email address\n\n"
            f"Confirm this address to finish setting up your "
            f"{settings.PROJECT_NAME} account.\n\n"
            f"Visit this link (expires in {expires_in_hours} hours):\n{verify_url}\n\n"
            "If you didn't create this account, you can safely ignore this email."
        )
        self._send_email(
            to_email,
            f"{settings.PROJECT_NAME} - Verify your email address",
            _shell("Verify your email address", inner),
            text_body,
            sensitive=True,
            credential_link=verify_url,
        )

    def send_security_notice(self, to_email: str, subject: str, message: str) -> None:
        """Notify a user that a security-relevant change was made to their account.

        Used for changes the account owner must be able to notice even if they did
        not make them — an email change, for instance, is otherwise a silent
        account-takeover step (change the address, then request a password reset).

        Args:
            to_email: Recipient — for an email change this is the OLD address.
            subject: Short subject line.
            message: Plain-text body; rendered into the HTML template as-is.

        Raises:
            EmailDeliveryError: The notice was not handed to a transport.
                ``account_security_service.notify_email_changed`` catches this —
                a failed notice must never block the change it describes.
        """
        inner = (
            f"<p>{message}</p>"
            "<p>If you did not make this change, contact your administrator immediately.</p>"
        )
        text_body = (
            f"{subject}\n\n{message}\n\n"
            "If you did not make this change, contact your administrator immediately."
        )
        self._send_email(
            to_email,
            f"{settings.PROJECT_NAME} - {subject}",
            _shell(subject, inner),
            text_body,
        )

    def _designated_config(self) -> EmailNotificationConfig | None:
        """Resolve the designated auth-mail config, or ``None`` for env SMTP.

        Never raises: a database hiccup degrades to the env transport rather than
        taking down a login-adjacent request.
        """
        factory = self._session_factory
        if factory is None:
            from app.db.base import SessionLocal

            factory = SessionLocal
        db = None
        try:
            db = factory()
            return load_auth_mail_config(db)
        except Exception as exc:  # noqa: BLE001 - degrade to env SMTP, never fail the caller
            logger.warning("Could not resolve the designated auth mail config: %s", exc)
            return None
        finally:
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()

    def _send_email(
        self,
        to: str,
        subject: str,
        html_body: str,
        text_body: str,
        sensitive: bool = False,
        credential_link: str | None = None,
    ) -> None:
        """Deliver one message, or raise.

        Args:
            sensitive: True when the body carries a credential (a password-reset
                URL is a single-use credential). Such a body is NEVER logged —
                doing so put live reset links into container stdout and from
                there into the log aggregator.
            credential_link: The single-use URL in the body, checked against the
                ``FRONTEND_URL`` default before anything is sent.

        Raises:
            EmailDeliveryError: No transport, an unusable link, or a send that
                the transport rejected.
        """
        config = self._designated_config()

        if config is None and not settings.SMTP_HOST:
            if not sensitive:
                logger.info("[DEV] Email to %s: %s\n%s", _mask_email(to), subject, text_body)
                return
            logger.error(
                "No mail transport configured — %r was NOT delivered to %s. Designate an "
                "email config (%s) or set SMTP_HOST.",
                subject,
                _mask_email(to),
                AUTH_EMAIL_CONFIG_SETTING_KEY,
            )
            raise EmailDeliveryError(f"No mail transport is configured; {subject!r} was not sent")

        if credential_link:
            self._assert_link_is_deliverable(credential_link, subject)

        if config is not None:
            self._send_via_config(config, to, subject, html_body)
            return
        self._send_via_env_smtp(to, subject, html_body, text_body)

    @staticmethod
    def _assert_link_is_deliverable(link: str, subject: str) -> None:
        """Refuse to mail a credential link built from the ``FRONTEND_URL`` default.

        ``FRONTEND_URL`` defaults to ``http://localhost:5173`` and is set in none
        of the compose files, so a deployment that configures mail but forgets it
        mails every user a link to their own machine. Refusing beats sending: the
        token is single-use and rate-limited (3 resets/hour), so a useless link
        also burns the recipient's budget. Only reached once a transport exists,
        which is never true of the stock dev stack.
        """
        if not link.startswith(DEFAULT_FRONTEND_URL):
            return
        logger.critical(
            "FRONTEND_URL is still %s, so %r would carry a link to the recipient's own "
            "machine. Refusing to send. Set FRONTEND_URL to this deployment's public URL.",
            DEFAULT_FRONTEND_URL,
            subject,
        )
        raise EmailDeliveryError(
            "FRONTEND_URL is not configured for this deployment, so the link in "
            f"{subject!r} would be unusable; refusing to send it"
        )

    @staticmethod
    def _send_via_config(
        config: EmailNotificationConfig, to: str, subject: str, html_body: str
    ) -> None:
        """Send through the designated DB-backed provider."""
        from app.services import watch_email_service

        ok, detail = watch_email_service.send_email(
            config, [to], subject, html_body, timeout=SMTP_TIMEOUT
        )
        if ok:
            logger.info("Sent %r to %s via email config %r", subject, _mask_email(to), config.name)
            return
        reason = _scrub(detail)
        logger.error(
            "Failed to send %r to %s via email config %r: %s",
            subject,
            _mask_email(to),
            config.name,
            reason,
        )
        raise EmailDeliveryError(f"Could not send {subject!r}: {reason}")

    @staticmethod
    def _send_via_env_smtp(to: str, subject: str, html_body: str, text_body: str) -> None:
        """Send through the env-configured SMTP server (STARTTLS or implicit SSL)."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.SMTP_FROM
        msg["To"] = to
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        server = None
        try:
            server = _env_smtp_connect()
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.SMTP_FROM, [to], msg.as_string())
        except Exception as e:
            # Never fall back to logging the body: for a password reset that body
            # is a live single-use credential.
            reason = _scrub(str(e))
            logger.error("Failed to send %r to %s: %s", subject, _mask_email(to), reason)
            raise EmailDeliveryError(f"Could not send {subject!r}: {reason}") from e
        finally:
            if server is not None:
                with contextlib.suppress(Exception):
                    server.quit()
        logger.info("Sent %r to %s", subject, _mask_email(to))


def _env_smtp_connect() -> smtplib.SMTP:
    """Open an env-configured SMTP session.

    Port 465 is implicit SSL — the session is encrypted before the greeting, so
    ``STARTTLS`` never applies and the old plaintext-or-STARTTLS branch made 465
    unusable. Every other port uses STARTTLS when ``SMTP_USE_TLS`` is set. This
    mirrors ``watch_email_service._smtp_connect`` so both stacks agree.
    """
    host, port = settings.SMTP_HOST, settings.SMTP_PORT
    if port == SMTP_IMPLICIT_SSL_PORT:
        return smtplib.SMTP_SSL(
            host, port, timeout=SMTP_TIMEOUT, context=ssl.create_default_context()
        )
    server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
    if settings.SMTP_USE_TLS:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
    return server


email_service = EmailService()
