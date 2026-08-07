"""Shared authentication utility functions.

Provides common helpers used across multiple auth modules to avoid
code duplication.
"""

from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.constants import AUTH_TYPES_NO_LOCAL_FALLBACK
from app.auth.constants import AUTH_TYPES_SUPPORT_LOCAL_FALLBACK


def local_password_allowed(
    auth_type: str | None, allow_local_fallback: bool
) -> tuple[bool, str | None]:
    """Whether this account may authenticate with a local password.

    **The single definition of that rule.** It previously existed twice, in
    ``direct_auth._validate_user_can_authenticate`` and
    ``core.security.authenticate_user``, and the two disagreed: only the former
    hard-blocked LDAP, so an LDAP account with ``allow_local_fallback`` set fell
    through the direct path and authenticated against a local bcrypt hash via the
    ORM path — breaking the "LDAP users never have a local password" invariant.

    The policy, keyed off the declared constants rather than inline literals:

    * ``local`` — always allowed; that is what the type means.
    * ``ldap`` (``AUTH_TYPES_NO_LOCAL_FALLBACK``) — never allowed. No local
      password is stored for these users, and the flag must not override that.
    * ``pki`` / ``keycloak`` (``AUTH_TYPES_SUPPORT_LOCAL_FALLBACK``) — allowed
      only with the per-user opt-in, which is a super_admin-only field.
    * anything else — refused, so an unrecognised ``auth_type`` fails closed.

    Args:
        auth_type: The account's ``User.auth_type``.
        allow_local_fallback: The account's per-user opt-in flag.

    Returns:
        ``(allowed, reason)`` — *reason* is a short, non-sensitive explanation
        suitable for a log line, and is ``None`` when allowed.
    """
    if auth_type == AUTH_TYPE_LOCAL:
        return True, None

    if auth_type in AUTH_TYPES_NO_LOCAL_FALLBACK:
        return False, f"auth_type={auth_type!r} never has a local password"

    if auth_type in AUTH_TYPES_SUPPORT_LOCAL_FALLBACK:
        if allow_local_fallback:
            return True, None
        return False, f"auth_type={auth_type!r} without local-fallback permission"

    return False, f"unrecognised auth_type={auth_type!r}"


def local_fallback_permitted_for(auth_type: str | None) -> bool:
    """Whether ``allow_local_fallback`` is meaningful for this ``auth_type``.

    Used to reject the flag at write time. Without this check a super_admin could
    set it on an LDAP account, which the UI hides but the API accepted — see
    :func:`local_password_allowed`.
    """
    return auth_type in AUTH_TYPES_SUPPORT_LOCAL_FALLBACK


def mask_identifier(identifier: str) -> str:
    """Mask identifier for safe logging to prevent sensitive data exposure.

    For emails (contains @): shows first char + *** + @domain
        e.g., "john.doe@example.com" -> "j***@example.com"
    For usernames: shows first 2 chars + ***
        e.g., "johndoe" -> "jo***"

    Args:
        identifier: Email or username to mask

    Returns:
        Masked identifier string
    """
    if not identifier:
        return "***"

    identifier = identifier.strip()

    if "@" in identifier:
        # Email format: show first char + *** + @domain
        local_part, domain = identifier.split("@", 1)
        if len(local_part) >= 1:
            return f"{local_part[0]}***@{domain}"
        return f"***@{domain}"
    else:
        # Username format: show first 2 chars + ***
        if len(identifier) >= 2:
            return f"{identifier[:2]}***"
        elif len(identifier) == 1:
            return f"{identifier[0]}***"
        return "***"
