"""Reading — and refusing — an identity a reverse proxy asserts in headers.

Everything in this module happens before a `User` row is touched. It answers three
questions in order, and every one of them can end the login:

1. **May this peer assert anything at all?** ``auth/header_trust.py``, shared with
   PKI. No allowlist means no.
2. **Does it know the shared secret**, if one is configured? Constant-time compare.
   This is the defence against a proxy that is trusted by address but has been
   misconfigured to pass client-supplied headers straight through.
3. **Does this deployment admit this identity?** ``proxy_allowed_domains``, with the
   same "empty admits everyone" reading ``oidc_allowed_groups`` uses.

Every outcome is audited, including a refusal from an untrusted peer — that event
is the only trace a header-injection attempt leaves, and it is the reason the
refusal path logs rather than silently returning ``None``.

The response never varies
-------------------------
:data:`REFUSAL_DETAIL` is returned for all of them. A distinct message would tell
an unauthenticated caller which of "you are not a trusted proxy", "your secret is
wrong" and "that address exists" applies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import AUTH_TYPE_PROXY
from app.auth.header_trust import header_assertion_permitted
from app.auth.header_trust import immediate_peer_ip
from app.auth.header_trust import shared_secret_matches
from app.auth.proxy.config import SHARED_SECRET_HEADER
from app.auth.proxy.config import ProxyConfig
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_USER

logger = logging.getLogger(__name__)

#: The single response for every refusal on this path.
REFUSAL_DETAIL = "Invalid or missing proxy authentication headers"

#: Audit ``error_code`` values. Separable in the audit index precisely because the
#: HTTP response is not.
REFUSED_UNTRUSTED_PEER = "PROXY_UNTRUSTED_PEER"
REFUSED_BAD_SECRET = "PROXY_SHARED_SECRET_MISMATCH"  # noqa: S105 # nosec B105
REFUSED_NO_EMAIL = "PROXY_NO_EMAIL_HEADER"
REFUSED_DOMAIN = "PROXY_DOMAIN_NOT_ALLOWED"

#: The only two roles a proxy header may name. ``super_admin`` is absent by design
#: and adding it would be a privilege-escalation bug: an external identity provider
#: grants at most ``admin`` everywhere else in this codebase
#: (``services/idp_group_mapping_service.GRANTABLE_ROLES``), and the break-glass
#: account must not be mintable by whoever can reach the proxy.
PROXY_GRANTABLE_ROLES = (ROLE_USER, ROLE_ADMIN)


@dataclass(frozen=True)
class ProxyAssertion:
    """What a trusted proxy claimed about the caller."""

    email: str
    full_name: str
    #: Group names read from ``proxy_groups_header``.
    groups: tuple[str, ...] = ()
    #: Whether the groups header was **present**. Absent and empty are different
    #: instructions — absent means "I am not managing your groups", empty means
    #: "this user is in no groups" and must reconcile memberships away. Conflating
    #: them is how a directory-sync typo turns into silent total privilege loss.
    groups_asserted: bool = False
    #: The role the header named, already capped. ``None`` when the role header is
    #: not configured or carried an unrecognised value.
    role: str | None = None

    @property
    def is_admin(self) -> bool:
        """Whether the assertion grants ``admin``."""
        return self.role == ROLE_ADMIN


def parse_domain_list(raw: str | None) -> list[str]:
    """Split ``proxy_allowed_domains`` into lower-cased entries."""
    if not raw:
        return []
    return [entry.strip().lower().lstrip("@") for entry in raw.split(",") if entry.strip()]


def domain_admitted(email: str, allowed_domains: str | None) -> bool:
    """Whether *email*'s domain passes the allowlist.

    An empty allowlist admits everyone — the upgrade-safe reading, and the same one
    ``oidc_allowed_groups`` uses. Only a non-empty list restricts.
    """
    allowed = parse_domain_list(allowed_domains)
    if not allowed:
        return True
    _, _, domain = email.rpartition("@")
    return domain.strip().lower() in allowed


def _role_from_header(raw: str | None) -> str | None:
    """Cap a role header value at ``admin``.

    Returns ``None`` — meaning "the proxy said nothing about privilege" — for an
    absent header and for any value outside :data:`PROXY_GRANTABLE_ROLES`, which
    notably includes ``super_admin``. An unrecognised value leaves the account's
    current role alone rather than demoting it, and is logged.
    """
    if not raw:
        return None
    value = raw.strip().lower()
    if value in PROXY_GRANTABLE_ROLES:
        return value
    logger.warning(
        "Ignoring proxy role header value %r: an identity provider may grant at most "
        "'%s' here, and 'super_admin' is local-only by design.",
        raw,
        ROLE_ADMIN,
    )
    return None


def _audit_refusal(request, error_code: str, username: str, **details) -> None:
    """Record a refused assertion as an ordinary login failure."""
    audit_logger.log(
        event_type=AuditEventType.AUTH_LOGIN_FAILURE,
        outcome=AuditOutcome.FAILURE,
        username=username,
        source_ip=immediate_peer_ip(request),
        error_code=error_code,
        details={"auth_method": AUTH_TYPE_PROXY, **details},
    )


def extract_proxy_assertion(request, cfg: ProxyConfig) -> ProxyAssertion | None:
    """Read and validate the identity a proxy asserted on *request*.

    Args:
        request: The incoming request.
        cfg: Resolved :class:`~app.auth.proxy.config.ProxyConfig`.

    Returns:
        The validated assertion, or ``None`` when the request carries no proxy
        identity at all (fall through to the other auth methods) or when it is
        refused. Refusals are audited; a bare absence is not.
    """
    raw_email = request.headers.get(cfg.email_header)
    asserted = bool(raw_email)

    if not header_assertion_permitted(
        request,
        cfg.networks,
        asserted=asserted,
        method="proxy",
        setting_name="proxy_trusted_proxies",
    ):
        _audit_refusal(
            request,
            REFUSED_UNTRUSTED_PEER,
            str(raw_email or "unknown"),
            allowlist_configured=bool(cfg.networks),
        )
        return None

    if not asserted:
        logger.debug("No proxy identity header on this request; falling through")
        return None

    if not shared_secret_matches(request.headers.get(SHARED_SECRET_HEADER), cfg.shared_secret):
        logger.warning(
            "SECURITY: proxy assertion from %s carried a missing or incorrect shared "
            "secret. The peer is allowlisted, so this is a proxy misconfiguration or a "
            "request that reached the backend without traversing it.",
            immediate_peer_ip(request),
        )
        _audit_refusal(request, REFUSED_BAD_SECRET, str(raw_email))
        return None

    email = str(raw_email).strip().lower()
    if "@" not in email:
        logger.warning("Refusing proxy assertion: %r is not an email address", raw_email)
        _audit_refusal(request, REFUSED_NO_EMAIL, str(raw_email))
        return None

    if not domain_admitted(email, cfg.allowed_domains):
        logger.warning(
            "Refusing proxy assertion for %s: domain not in proxy_allowed_domains", email
        )
        _audit_refusal(request, REFUSED_DOMAIN, email)
        return None

    raw_groups = request.headers.get(cfg.groups_header) if cfg.groups_header else None
    groups = tuple(
        entry.strip()
        for entry in (raw_groups or "").split(cfg.groups_separator or ",")
        if entry.strip()
    )

    return ProxyAssertion(
        email=email,
        full_name=str(request.headers.get(cfg.name_header) or "").strip(),
        groups=groups,
        groups_asserted=raw_groups is not None,
        role=_role_from_header(request.headers.get(cfg.role_header) if cfg.role_header else None),
    )
