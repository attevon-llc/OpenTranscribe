"""Policy for which SMTP connections may be used.

Shared by the mail senders (``services/email_service``, ``services/watch_email_service``)
and the email-config schemas, so a configuration is refused when it is saved and again
when a message is about to be sent, by the same rule.
"""

from __future__ import annotations

import ipaddress

#: Implicit-TLS submission port: the session is encrypted from the first byte.
SMTP_IMPLICIT_SSL_PORT = 465

#: Port used when a config does not name one (STARTTLS submission).
SMTP_DEFAULT_PORT = 587


def is_loopback_host(host: str | None) -> bool:
    """Whether ``host`` is ``localhost`` or a loopback IP literal."""
    if not host:
        return False
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def smtp_transport_problem(
    host: str | None, port: int, starttls: bool, has_credentials: bool
) -> str | None:
    """Why this SMTP connection must not be used, or ``None`` when it is acceptable.

    A connection is encrypted when it is implicit TLS (port 465) or upgraded with
    STARTTLS. Credentials are never sent over anything else, and a message (which can
    carry a single-use link) only goes out unencrypted to a relay on the same machine.
    The returned text is safe to show to an administrator: it names no host or user.
    """
    if port == SMTP_IMPLICIT_SSL_PORT or starttls:
        return None
    if has_credentials:
        return (
            "SMTP credentials are only sent over implicit TLS (port 465) or STARTTLS; "
            "enable TLS for this server"
        )
    if not is_loopback_host(host):
        return (
            "Plaintext SMTP is only allowed to a loopback relay; enable TLS for this server "
            "(implicit TLS on port 465 or STARTTLS)"
        )
    return None
