"""Absolute session timeout for tokens from a registered external identity provider.

Built-in sessions are capped by ``refresh_token.absolute_expires_at``
(``token_service._session_within_lifetime``). A registered external provider
(``auth.provider_registry``) owns its own browser session and keeps minting fresh
short-lived tokens for as long as that session lives, so without this check
``SESSION_ABSOLUTE_TIMEOUT_MINUTES`` never applied to those users (issue #1106;
NIST SP 800-63B-4 reauthentication, FedRAMP AC-12).

The clock is the token's ``auth_time`` — when the person actually authenticated —
never ``iat``, which a refreshed token always has recent. A verifier that supplies
no usable ``auth_time`` degrades to "not enforced" with one warning per provider
rather than refusing every request: failing closed on a missing claim would lock
out every user of a provider that simply does not emit it.

Idle timeout is not enforced here. Every request (polling, WebSocket keepalives,
background refresh) would count as activity; the browser-side idle guard owns it.
"""

import logging
import time

from app.auth.provider_registry import ExternalIdentity
from app.core.config import settings

logger = logging.getLogger(__name__)

#: ``detail.code`` of the 401. The SPA branches on it to end the provider's own
#: session too; a bare 401 would bounce to /login, where the still-live provider
#: session would sign the user straight back in.
ERROR_CODE_SESSION_EXPIRED = "session_expired"

#: Largest ``auth_time`` accepted as plausible epoch seconds (year ~2286). Anything
#: bigger is almost certainly milliseconds or garbage; treat it as absent.
_MAX_EPOCH_SECONDS = 10_000_000_000

_warned_providers: set[str] = set()


def _absolute_timeout_minutes() -> int:
    """Admin UI > ``.env`` > default, from the process-wide cache (hot path)."""
    from app.core.auth_settings import get_process_auth_settings

    return int(get_process_auth_settings().session_absolute_timeout_minutes)


def external_auth_time(identity: ExternalIdentity) -> int | None:
    """Return the identity's authentication time in epoch seconds, or None if unusable."""
    value = identity.auth_time
    if value is None:
        value = identity.raw_claims.get("auth_time")
    # bool is an int subclass; a boolean claim is not a timestamp.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0 or value >= _MAX_EPOCH_SECONDS:
        return None
    return int(value)


def external_session_expired(identity: ExternalIdentity, *, now: float | None = None) -> bool:
    """True when the identity authenticated longer ago than the absolute timeout."""
    if not settings.EXTERNAL_SESSION_ABSOLUTE_TIMEOUT_ENFORCED:
        return False
    limit_minutes = _absolute_timeout_minutes()
    if limit_minutes <= 0:
        return False

    auth_time = external_auth_time(identity)
    if auth_time is None:
        if identity.provider not in _warned_providers:
            _warned_providers.add(identity.provider)
            logger.warning(
                "External provider '%s' supplied no usable auth_time; the absolute "
                "session timeout is not enforced server-side for it.",
                identity.provider,
            )
        return False

    current = time.time() if now is None else now
    expired = current - auth_time > limit_minutes * 60
    if expired:
        logger.info(
            "Refusing external token for %s/%s: authenticated %d min ago, limit %d min",
            identity.provider,
            identity.external_id,
            int((current - auth_time) // 60),
            limit_minutes,
        )
    return expired
