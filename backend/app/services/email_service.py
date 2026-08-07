"""Email service for sending transactional emails (password reset, etc.)."""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.auth.utils import mask_identifier as _mask_email
from app.core.config import settings

logger = logging.getLogger(__name__)

#: Socket timeout for every SMTP operation. Without one, an unreachable MTA
#: blocks the worker thread that is servicing a login/reset request.
SMTP_TIMEOUT = 10


class EmailService:
    """Sends transactional emails via SMTP.

    Falls back to logging the email content when SMTP is not configured,
    which is useful during development.
    """

    def send_password_reset(self, to_email: str, reset_url: str) -> None:
        """Send a password reset email.

        Args:
            to_email: Recipient email address.
            reset_url: Full URL with token for the password reset page.
        """
        subject = f"{settings.PROJECT_NAME} - Password Reset Request"
        html_body = f"""
        <html>
        <body style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
            <h2>Password Reset Request</h2>
            <p>You requested a password reset for your {settings.PROJECT_NAME} account.</p>
            <p>Click the link below to reset your password. This link expires in 1 hour.</p>
            <p><a href="{reset_url}" style="display: inline-block; padding: 12px 24px;
                background-color: #4f46e5; color: white; text-decoration: none;
                border-radius: 6px;">Reset Password</a></p>
            <p>If you didn't request this, you can safely ignore this email.</p>
            <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 24px 0;">
            <p style="color: #6b7280; font-size: 12px;">
                This is an automated message from {settings.PROJECT_NAME}.
            </p>
        </body>
        </html>
        """
        text_body = (
            f"Password Reset Request\n\n"
            f"You requested a password reset for your {settings.PROJECT_NAME} account.\n\n"
            f"Visit this link to reset your password (expires in 1 hour):\n{reset_url}\n\n"
            f"If you didn't request this, you can safely ignore this email."
        )

        self._send_email(to_email, subject, html_body, text_body, sensitive=True)

    def send_security_notice(self, to_email: str, subject: str, message: str) -> None:
        """Notify a user that a security-relevant change was made to their account.

        Used for changes the account owner must be able to notice even if they did
        not make them — an email change, for instance, is otherwise a silent
        account-takeover step (change the address, then request a password reset).

        Args:
            to_email: Recipient — for an email change this is the OLD address.
            subject: Short subject line.
            message: Plain-text body; rendered into the HTML template as-is.
        """
        html_body = f"""
        <html>
        <body style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
            <h2>{subject}</h2>
            <p>{message}</p>
            <p>If you did not make this change, contact your administrator immediately.</p>
            <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 24px 0;">
            <p style="color: #6b7280; font-size: 12px;">
                This is an automated message from {settings.PROJECT_NAME}.
            </p>
        </body>
        </html>
        """
        text_body = (
            f"{subject}\n\n{message}\n\n"
            "If you did not make this change, contact your administrator immediately."
        )
        self._send_email(to_email, f"{settings.PROJECT_NAME} - {subject}", html_body, text_body)

    def _send_email(
        self,
        to: str,
        subject: str,
        html_body: str,
        text_body: str,
        sensitive: bool = False,
    ) -> None:
        """Send an email via SMTP, or log that it could not be sent.

        Args:
            sensitive: True when the body carries a credential (a password-reset
                URL is a single-use credential). Such a body is NEVER logged —
                doing so put live reset links into container stdout and from
                there into the log aggregator. Without SMTP configured the send
                is simply reported as failed, which is the honest outcome.
        """
        if not settings.SMTP_HOST:
            if sensitive:
                logger.error(
                    "SMTP is not configured (SMTP_HOST empty) — could not deliver %r to %s. "
                    "Configure SMTP so password resets reach users.",
                    subject,
                    _mask_email(to),
                )
            else:
                logger.info("[DEV] Email to %s: %s\n%s", _mask_email(to), subject, text_body)
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.SMTP_FROM
        msg["To"] = to
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        try:
            # An unreachable or hanging MTA must not hold the request thread open.
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=SMTP_TIMEOUT)
            if settings.SMTP_USE_TLS:
                server.ehlo()
                server.starttls()
                server.ehlo()

            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)

            server.sendmail(settings.SMTP_FROM, [to], msg.as_string())
            server.quit()
            logger.info("Sent %r to %s", subject, _mask_email(to))
        except Exception as e:
            # Never fall back to logging the body: for a password reset that body
            # is a live single-use credential.
            logger.error("Failed to send %r to %s: %s", subject, _mask_email(to), e)


email_service = EmailService()
