"""
PKI/X.509 certificate authentication module.

Handles authentication via client certificates (mutual TLS).
The reverse proxy (Nginx) handles TLS termination and passes
certificate information via headers.

Supports certificate revocation checking via:
- OCSP (Online Certificate Status Protocol) - preferred, real-time
- CRL (Certificate Revocation List) - fallback when OCSP unavailable

The revocation checks live in ``auth/pki_revocation.py`` and ``auth/pki_ocsp.py``,
certificate and DN parsing in ``auth/pki_certificate.py``, and user provisioning in
``auth/pki_provisioning.py``.
"""

import logging
from datetime import UTC
from urllib.parse import unquote

from cryptography import x509
from cryptography.hazmat.backends import default_backend

from app.auth.header_trust import header_assertion_permitted
from app.auth.header_trust import header_source_is_trusted
from app.auth.header_trust import immediate_peer_ip as _get_pki_client_ip
from app.auth.header_trust import (
    ip_in_networks as _is_pki_trusted_proxy,  # noqa: F401  # re-export: the PKI suite's membership tests import this name
)
from app.auth.header_trust import parse_trusted_proxies
from app.auth.pki_certificate import PKIUserData
from app.auth.pki_certificate import _extract_certificate_metadata
from app.auth.pki_certificate import _normalize_dn
from app.auth.pki_certificate import _parse_dn_components
from app.auth.pki_certificate import parse_certificate
from app.auth.pki_revocation import _check_revocation
from app.core.config import settings
from app.core.process_auth_settings import get_process_auth_settings

logger = logging.getLogger(__name__)

# Revocation settings are resolved through the process-level auth-settings layer,
# not read off ``settings`` directly (issue #498). The helpers below hold no
# session — they run deep inside certificate verification, on the login path —
# so ``get_process_auth_settings()`` is the right tier: it serves a cached
# DB-resolved value and falls back to the ``.env`` value, which is what these
# call sites used to read unconditionally.

# ===== Trusted Proxy Validation =====
#
# There is no PKI-specific trust machinery any more. `auth/header_trust.py` owns the
# allowlist parser, the immediate-peer resolver and the fail-closed refusal, and
# `auth/proxy/` — trusted-header authentication for an email rather than a subject DN
# — calls exactly the same functions. The names below are bindings, kept because they
# are what this module's own callers and its test suite refer to.


def _parse_pki_trusted_proxies(trusted_proxies_str: str) -> list:
    """Parse ``PKI_TRUSTED_PROXIES`` into IP networks (see ``auth/header_trust``)."""
    return parse_trusted_proxies(trusted_proxies_str, label="PKI trusted proxy")


# Parse trusted proxies at module load time for efficiency
_pki_trusted_proxy_networks = _parse_pki_trusted_proxies(settings.PKI_TRUSTED_PROXIES)


def extract_certificate_from_headers(request, cert_header: str | None = None) -> str | None:
    """
    Extract client certificate from request headers.

    The reverse proxy (Nginx) must be configured to pass the
    client certificate via headers.

    Args:
        request: FastAPI Request object
        cert_header: Header name to read (DB-backed ``auth_settings.pki_cert_header``
            takes precedence over ``.env``). Falls back to ``settings.PKI_CERT_HEADER``
            when not supplied, e.g. for callers with no DB session.

    Returns:
        PEM-encoded certificate string or None
    """
    header_name = cert_header or settings.PKI_CERT_HEADER
    cert_value = request.headers.get(header_name)
    if not cert_value:
        return None

    # Nginx URL-encodes the certificate
    return unquote(cert_value)


def extract_dn_from_headers(request, cert_dn_header: str | None = None) -> str | None:
    """
    Extract Distinguished Name from headers (if Nginx provides it).

    Args:
        request: FastAPI Request object
        cert_dn_header: Header name to read (DB-backed ``auth_settings.pki_cert_dn_header``
            takes precedence over ``.env``). Falls back to ``settings.PKI_CERT_DN_HEADER``
            when not supplied, e.g. for callers with no DB session.

    Returns:
        DN string or None
    """
    header_name = cert_dn_header or settings.PKI_CERT_DN_HEADER
    result = request.headers.get(header_name)
    return str(result) if result is not None else None


def _is_pki_admin(subject_dn: str, admin_dns_config: str | None = None) -> bool:
    """
    Check if certificate DN is in the admin DNs list.

    Performs case-insensitive DN comparison with proper normalization.

    Args:
        subject_dn: Certificate subject DN
        admin_dns_config: Admin DN list (DB-backed config takes precedence
            over .env — callers with a session pass auth_settings.pki_admin_dns).
            Falls back to the PKI_ADMIN_DNS environment setting.

    Returns:
        True if user is an admin
    """
    effective_dns = admin_dns_config if admin_dns_config else settings.PKI_ADMIN_DNS
    if not effective_dns:
        return False

    normalized_subject = _normalize_dn(subject_dn)
    # Admin DNs use semicolons to separate entries because full DNs contain commas.
    # e.g. PKI_ADMIN_DNS=CN=Doe John jdoe,O=U.S. Government,C=US;CN=Smith Jane jsmith,...
    from app.auth.ldap_directory import _parse_group_list

    admin_dns = [_normalize_dn(dn) for dn in _parse_group_list(effective_dns)]

    return normalized_subject in admin_dns


def _pki_header_source_is_trusted(request, networks: list | None = None) -> bool:
    """
    Whether the peer that delivered this request may assert PKI headers.

    One rule, shared with trusted-header (``proxy``) authentication and implemented
    once in ``auth/header_trust.py``: the immediate peer must be in the configured
    ``PKI_TRUSTED_PROXIES`` allowlist, and an empty allowlist trusts nobody.

    Args:
        request: FastAPI Request object
        networks: DB-resolved trusted-proxy networks (``auth_settings.pki_trusted_proxies``,
            parsed by the caller). Falls back to the module-level global — parsed from
            ``.env`` at import time — when not supplied, e.g. for callers with no DB
            session. The module global stays patchable in tests via that fallback.

    Returns:
        True if the immediate peer is a configured trusted proxy
    """
    effective_networks = networks if networks is not None else _pki_trusted_proxy_networks
    return header_source_is_trusted(request, effective_networks)


def _validate_pki_headers_source(
    request,
    networks: list | None = None,
    cert_header: str | None = None,
    cert_dn_header: str | None = None,
) -> bool | None:
    """
    Validate that PKI headers come from a trusted proxy.

    PKI headers are only meaningful when a reverse proxy terminated mTLS and vouched
    for them. An unvouched header set is just an attacker-supplied string, and a DN
    listed in ``PKI_ADMIN_DNS`` would mint an admin session from it — so the
    empty-allowlist case FAILS CLOSED, unconditionally. ``main.py`` already refuses to
    start in a hardened environment with ``PKI_ENABLED`` and no allowlist; this is the
    defence in depth behind that guard, so a relaxed developer stack cannot be spoofed
    by accident either.

    Args:
        request: FastAPI Request object
        networks: DB-resolved trusted-proxy networks, see ``_pki_header_source_is_trusted``.
        cert_header: DB-resolved cert header name, see ``extract_certificate_from_headers``.
        cert_dn_header: DB-resolved DN header name, see ``extract_dn_from_headers``.

    Returns:
        True if valid/allowed, False if headers from untrusted source, None to continue
    """
    has_pki_headers = bool(
        request.headers.get(cert_header or settings.PKI_CERT_HEADER)
        or request.headers.get(cert_dn_header or settings.PKI_CERT_DN_HEADER)
    )
    effective_networks = networks if networks is not None else _pki_trusted_proxy_networks
    return header_assertion_permitted(
        request,
        effective_networks,
        asserted=has_pki_headers,
        method="PKI certificate",
        setting_name="PKI_TRUSTED_PROXIES",
    )


def _handle_revocation_check(cert: x509.Certificate | None) -> bool | None:
    """
    Handle certificate revocation checking if enabled.

    Args:
        cert: The parsed certificate (may be None)

    Returns:
        True if authentication should be denied, False if OK to proceed, None if revocation
        checking is disabled
    """
    if not get_process_auth_settings().pki_verify_revocation:
        return False  # Revocation checking disabled, proceed

    if not cert:
        logger.warning(
            "Revocation checking enabled but no certificate provided. "
            "Configure Nginx to pass full certificate via PKI_CERT_HEADER."
        )
        if not get_process_auth_settings().pki_revocation_soft_fail:
            logger.error("Denying authentication: revocation check required but no certificate")
            return True  # Deny
        logger.warning("Allowing authentication without revocation check (soft-fail)")
        return False  # Allow with soft-fail

    is_revoked, reason = _check_revocation(cert)
    if is_revoked:
        logger.warning(f"Certificate authentication denied: {reason}")
        return True  # Deny
    logger.debug(f"Revocation check passed: {reason}")
    return False  # Allow


def _extract_user_info_from_request(
    request,
    cert: x509.Certificate | None,
    cert_pem: str | None,
    require_certificate: bool = False,
    networks: list | None = None,
    cert_dn_header: str | None = None,
) -> tuple[str, str, str] | None:
    """
    Extract user info (subject_dn, common_name, email) from request.

    Args:
        request: FastAPI Request object
        cert: Parsed certificate (may be None)
        cert_pem: PEM-encoded certificate string (may be None)
        require_certificate: ``pki_mode == "mutual_tls"``. Refuses a DN header
            that is not accompanied by the certificate itself, even from a
            trusted proxy — see below.
        networks: DB-resolved trusted-proxy networks, see ``_pki_header_source_is_trusted``.
        cert_dn_header: DB-resolved DN header name, see ``extract_dn_from_headers``.

    Returns:
        Tuple of (subject_dn, common_name, email) or None if extraction fails
    """
    # Try to get DN directly from header (more efficient)
    subject_dn = extract_dn_from_headers(request, cert_dn_header)

    if subject_dn:
        # pki_mode == "mutual_tls": the proxy's word is not enough. In "header"
        # mode a trusted proxy may vouch for a DN it validated, which is one
        # trusted component away from the evidence; in "mutual_tls" mode the
        # certificate must be forwarded so THIS process parses it, checks its
        # validity window and (optionally) its revocation status. That is the
        # whole difference between the two modes, and the reason the setting is
        # worth having at all.
        if cert is None and require_certificate:
            logger.error(
                "Refusing DN-only PKI authentication from %s: pki_mode is 'mutual_tls', "
                "which requires the client certificate itself (configure the proxy to "
                "forward it in the PKI_CERT_HEADER).",
                _get_pki_client_ip(request),
            )
            return None

        # The DN header is a *claim*, not evidence. It is trustworthy in exactly two cases:
        #
        # 1. A certificate was also presented. `pki_authenticate` has already parsed it and
        #    rejected it if it is outside its validity window, so by the time we get here
        #    the identity is backed by a certificate we checked. The header DN still wins
        #    over the certificate's rfc4514 form because the proxy emits the canonical
        #    string deployments configure PKI_ADMIN_DNS with.
        # 2. No certificate, but the request came from a configured trusted proxy — the
        #    component that terminated mTLS and is therefore vouching for the DN.
        #
        # Anything else is an unverified string from an unauthenticated peer, and
        # `_is_pki_admin` would happily grant admin on it.
        if cert is None and not _pki_header_source_is_trusted(request, networks):
            logger.error(
                "Refusing DN-only PKI authentication from untrusted peer "
                f"{_get_pki_client_ip(request)}: no certificate was presented and no "
                "trusted proxy vouched for the DN header."
            )
            return None
        common_name, email = _parse_dn_components(subject_dn)
        return (subject_dn, common_name, email)

    if cert and cert_pem:
        cert_info = parse_certificate(cert_pem)
        if not cert_info:
            return None
        return (cert_info.subject_dn, cert_info.common_name, cert_info.email or "")

    # No DN header and no certificate
    logger.warning("No client certificate or DN header provided")
    return None


#: The only two PKI transports this module implements. ``pki_mode`` is validated
#: against the same pair in ``schemas/auth_config.py``; an unrecognised stored
#: value is treated as ``header``, the pre-existing behaviour.
PKI_MODE_HEADER = "header"
PKI_MODE_MUTUAL_TLS = "mutual_tls"

#: Whether the address in ``PKIUserData["email"]`` is a *verified* address that may be
#: used to take over a pre-existing account. It is not, and the reason is visible in
#: ``_extract_user_info_from_request``: whenever a DN header is present — the deployed
#: ``header`` mode, and even ``mutual_tls`` — the address is parsed out of that header
#: string with ``_parse_dn_components``, so it is a value the proxy supplied, not one
#: this process read out of a certificate. Nothing downstream records which of the two
#: extraction paths produced it, so the guard fails closed for both. Accounts already
#: carrying a ``pki_subject_dn`` are unaffected, and so is a new PKI user whose address
#: collides with no existing account.
#:
#: Turning this into a genuine per-identity signal (rather than a constant) means
#: carrying "this address came from the validated certificate's SAN" through
#: ``PKIUserData`` — a deliberate change, not a default.
PKI_ASSERTS_EMAIL_VERIFIED = False


def pki_authenticate(
    request,
    admin_dns_config: str | None = None,
    pki_mode: str | None = None,
    trusted_proxies_config: str | None = None,
    cert_header: str | None = None,
    cert_dn_header: str | None = None,
) -> PKIUserData | None:
    """
    Authenticate user via X.509 client certificate.

    Performs certificate validation including optional revocation checking
    via OCSP (preferred) and CRL (fallback) when PKI_VERIFY_REVOCATION is enabled.

    ``admin_dns_config`` carries the DB-backed admin DN list (admin UI takes
    precedence over .env); without it the PKI_ADMIN_DNS env setting is used.
    ``trusted_proxies_config``, ``cert_header`` and ``cert_dn_header`` are the same
    kind of DB-backed override, mirroring ``ProxyConfig.from_db`` for trusted-header
    (``proxy``) authentication — without them the module falls back to whatever
    ``.env`` had at import time.

    Args:
        request: FastAPI Request object
        admin_dns_config: DB-backed ``pki_admin_dns`` value, if available.
        pki_mode: ``header`` (default) or ``mutual_tls``. In ``mutual_tls`` the
            certificate itself must be forwarded — a DN header alone is refused
            even from a trusted proxy. Until this parameter existed, ``pki_mode``
            was a stored string no code read.
        trusted_proxies_config: DB-backed ``auth_settings.pki_trusted_proxies``
            value, if available. Passing this is what makes a Settings UI change
            to the trusted-proxy allowlist take effect without a process restart —
            without it, only the ``PKI_TRUSTED_PROXIES`` env value (parsed once at
            import) is enforced, no matter what is saved to the database.
        cert_header: DB-backed ``auth_settings.pki_cert_header`` value, if available.
        cert_dn_header: DB-backed ``auth_settings.pki_cert_dn_header`` value, if available.

    Returns:
        PKIUserData with certificate metadata or None if authentication fails
    """
    # Note: PKI_ENABLED check is done by the caller (pki_login endpoint)
    # which checks the database setting first, then falls back to env.
    # We skip the env-only check here to support database-driven config.

    networks = (
        parse_trusted_proxies(trusted_proxies_config, label="PKI trusted proxy")
        if trusted_proxies_config is not None
        else None
    )

    # Validate that PKI headers come from trusted proxies
    if _validate_pki_headers_source(request, networks, cert_header, cert_dn_header) is False:
        return None

    # Extract certificate from headers - needed for revocation checking.
    # ORDER IS LOAD-BEARING: the certificate is parsed and its validity window checked
    # BEFORE `_extract_user_info_from_request` is allowed to trust the DN header. A DN
    # header sent alongside an expired certificate must not short-circuit that check.
    cert_pem = extract_certificate_from_headers(request, cert_header)
    cert = None
    cert_metadata: dict | None = None

    if cert_pem:
        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())
            # Extract comprehensive certificate metadata
            cert_metadata = _extract_certificate_metadata(cert)

            # Validate certificate validity period
            from datetime import datetime

            now = datetime.now(UTC)

            # Check not_before (certificate not yet valid)
            if cert_metadata.get("not_before"):
                not_before = datetime.fromisoformat(
                    cert_metadata["not_before"].replace("Z", "+00:00")
                )
                if now < not_before:
                    logger.warning(
                        f"Certificate not yet valid: valid from {cert_metadata['not_before']}"
                    )
                    return None

            # Check not_after (certificate expired)
            if cert_metadata.get("not_after"):
                not_after = datetime.fromisoformat(
                    cert_metadata["not_after"].replace("Z", "+00:00")
                )
                if now > not_after:
                    logger.warning(f"Certificate expired: expired at {cert_metadata['not_after']}")
                    return None

        except Exception as e:
            logger.error(f"Failed to parse certificate: {e}")
            return None

    # Check revocation status if enabled
    if _handle_revocation_check(cert):
        return None  # Revocation check failed or certificate revoked

    # Extract user information from request
    user_info = _extract_user_info_from_request(
        request,
        cert,
        cert_pem,
        require_certificate=pki_mode == PKI_MODE_MUTUAL_TLS,
        networks=networks,
        cert_dn_header=cert_dn_header,
    )
    if user_info is None:
        return None

    subject_dn, common_name, email = user_info

    # Check if admin DN (DB-backed config wins over .env when provided)
    is_admin = _is_pki_admin(subject_dn, admin_dns_config)

    logger.info(f"PKI authentication successful for: {subject_dn}")

    return PKIUserData(
        subject_dn=subject_dn,
        common_name=common_name,
        email=email,
        is_admin=is_admin,
        # Include certificate metadata if available
        serial_number=cert_metadata["serial_number"] if cert_metadata else None,
        issuer_dn=cert_metadata["issuer_dn"] if cert_metadata else None,
        organization=cert_metadata["organization"] if cert_metadata else None,
        organizational_unit=cert_metadata["organizational_unit"] if cert_metadata else None,
        not_before=cert_metadata["not_before"] if cert_metadata else None,
        not_after=cert_metadata["not_after"] if cert_metadata else None,
        fingerprint=cert_metadata["fingerprint"] if cert_metadata else None,
    )
