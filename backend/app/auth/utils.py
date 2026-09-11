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
    * ``pki`` / ``oidc`` (``AUTH_TYPES_SUPPORT_LOCAL_FALLBACK``) — allowed
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


def mask_email_for_display(email: str) -> str:
    """Mask an email address for DISPLAY in a directory/sharing picker (issue #904).

    Shows the first TWO local-part characters + ``***`` + ``@domain``, e.g.
    ``"jane@acme.com"`` -> ``"ja***@acme.com"``. A local part shorter than two
    characters masks to ``"***@domain"``. A value with no ``@`` (not really an
    email) falls back to the same first-2-chars-plus-``***`` rule ``mask_identifier``
    uses for usernames.

    ⚠️ **Do NOT merge this with :func:`mask_identifier`, and do not reuse it here.**
    That function's job is log safety: its email branch shows only the first
    character (``"j***@acme.com"``), which is deliberately coarse because a log
    line is not a UI a caller is choosing between two people with. This function's
    job is the opposite — the ``GET /users/search`` picker exists so a caller can
    tell two same-named accounts apart, and `mask_identifier`'s one-character rule
    collides exactly there: ``"jane@acme.com"`` and ``"john@acme.com"`` both render
    ``"j***@acme.com"`` under it. Two characters is the minimum that disambiguates
    the common case (most first-name collisions differ by the second letter) while
    still not handing back the full local part.

    The domain is kept in clear on purpose: within one tenant every account shares
    it, so showing it discloses nothing the caller does not already know, and across
    a multi-domain deployment it is what actually distinguishes two same-named
    people. Masking it would remove signal for zero privacy benefit.

    Args:
        email: The address to mask for display.

    Returns:
        The masked address string.
    """
    if not email:
        return "***"

    email = email.strip()

    if "@" not in email:
        if len(email) >= 2:
            return f"{email[:2]}***"
        if len(email) == 1:
            return f"{email[0]}***"
        return "***"

    local_part, domain = email.split("@", 1)
    if len(local_part) >= 2:
        return f"{local_part[:2]}***@{domain}"
    return f"***@{domain}"
