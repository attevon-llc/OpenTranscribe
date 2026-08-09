"""One trust check for every header-asserted identity (PKI DN, proxy email).

The question
-----------
A reverse proxy can assert an identity in a request header. The only thing that
makes such a header worth anything is *who delivered it*: an authenticating proxy
that terminated mTLS / ran the SSO flow, or an arbitrary client that typed the
header itself. This module answers that one question, and it is the only place in
``app/auth`` that answers it.

It was written twice before it was written once. ``pki_auth.py`` grew a parsed
CIDR allowlist, an immediate-peer resolver and a fail-closed refusal — all correct,
all PKI-shaped, and none of it reachable for an identity that is not an X.509
subject DN. Trusted-header (``auth_type='proxy'``) authentication needs exactly the
same rules for an email address, so the machinery moved here and both callers now
share it. ``pki_auth`` keeps its ``_pki_*`` names as thin bindings over these
functions; there is no second implementation to drift.

The rules, in full
------------------
1. **The immediate peer decides, never a forwarded header.** ``X-Forwarded-For``
   is asserted by the same party whose assertion we are trying to validate, so
   ``utils/client_ip.resolve_client_ip`` — which deliberately walks that chain to
   find the *originating* client — is the wrong tool here and is not used. Only the
   socket peer can answer "did the host that physically delivered this header
   terminate authentication for us?".
2. **An empty allowlist trusts nobody.** Not "trust everyone", not "warn and
   continue". Open WebUI's trusted-header mode is documented as trusting the
   network and nothing else, and its own hardening guide calls a proxy that fails
   to strip client-supplied headers "the most common misconfiguration". We refuse
   instead: an unvouched header is an attacker-supplied string, and both callers
   can turn one into an ``admin`` session.
3. **Nothing asserted is not a refusal.** A request carrying none of the headers
   falls through to the other auth methods untouched.
4. **An optional shared secret is compared in constant time**, so a proxy
   misconfiguration alone is not sufficient for takeover.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: What a parsed allowlist entry is. Single addresses are widened to /32 or /128 so
#: membership is one uniform ``in`` test.
TrustedNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

#: Returned when the transport exposes no peer address (ASGI test clients, unix
#: sockets). It matches no network, so it is refused like any other untrusted peer.
UNKNOWN_PEER = "unknown"


def parse_trusted_proxies(raw: str | None, *, label: str = "trusted proxy") -> list[TrustedNetwork]:
    """Parse a comma-separated allowlist of IPs / CIDR ranges.

    Args:
        raw: Comma-separated addresses or CIDR blocks. Empty/``None`` yields ``[]``,
            which is the fail-closed "trust nobody" state.
        label: Used only in the warning for an unparseable entry, so an operator
            can tell which setting they mistyped.

    Returns:
        Parsed networks, skipping (and logging) any entry that does not parse. A
        malformed entry is dropped rather than widening the allowlist.
    """
    if not raw:
        return []

    networks: list[TrustedNetwork] = []
    for entry in raw.split(","):
        proxy = entry.strip()
        if not proxy:
            continue
        try:
            if "/" in proxy:
                networks.append(ipaddress.ip_network(proxy, strict=False))
            else:
                address = ipaddress.ip_address(proxy)
                suffix = 32 if isinstance(address, ipaddress.IPv4Address) else 128
                networks.append(ipaddress.ip_network(f"{proxy}/{suffix}"))
        except ValueError as exc:
            logger.warning("Invalid %s address '%s': %s", label, proxy, exc)
    return networks


def ip_in_networks(ip: str, networks: Sequence[TrustedNetwork]) -> bool:
    """Whether *ip* falls inside any of *networks*.

    Args:
        ip: Address to test. A malformed value is not a member of anything.
        networks: Parsed allowlist. Empty means no.

    Returns:
        True only for a well-formed address inside a configured network.
    """
    if not networks:
        return False
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        logger.warning("Invalid IP address format: %s", ip)
        return False
    return any(parsed in network for network in networks)


def immediate_peer_ip(request) -> str:
    """Return the address of the host that opened this connection.

    Deliberately NOT ``utils/client_ip.resolve_client_ip`` — see rule 1 in the
    module docstring.

    Args:
        request: Starlette/FastAPI request (or any object exposing ``.client``).

    Returns:
        The peer address, or :data:`UNKNOWN_PEER` when the transport has none.
    """
    client = getattr(request, "client", None)
    if client is not None:
        return str(getattr(client, "host", "") or UNKNOWN_PEER)
    return UNKNOWN_PEER


def header_source_is_trusted(request, networks: Sequence[TrustedNetwork]) -> bool:
    """Whether the peer that delivered *request* may assert identity headers.

    One rule, with no environment-dependent relaxation: the immediate peer must be
    in the configured allowlist. An empty allowlist trusts nobody.
    """
    if not networks:
        return False
    return ip_in_networks(immediate_peer_ip(request), networks)


def header_assertion_permitted(
    request,
    networks: Sequence[TrustedNetwork],
    *,
    asserted: bool,
    method: str,
    setting_name: str,
) -> bool:
    """Decide whether to accept identity headers on *request*.

    Args:
        request: The incoming request.
        networks: Parsed trusted-proxy allowlist for this auth method.
        asserted: Whether the request actually carries any of the method's identity
            headers. When it does not there is nothing to refuse.
        method: Auth-method name, for the log line (``pki`` / ``proxy``).
        setting_name: The allowlist setting an operator would fix.

    Returns:
        True to continue (trusted peer, or nothing asserted), False to refuse.
    """
    if header_source_is_trusted(request, networks):
        return True

    if not asserted:
        # Nothing was claimed, so nothing is being refused — let the request fall
        # through to the other authentication methods.
        return True

    peer = immediate_peer_ip(request)
    if networks:
        logger.warning(
            "%s identity headers received from untrusted immediate peer %s (the socket "
            "peer, not necessarily the originating client). This may indicate a header "
            "injection attempt. Add legitimate proxy addresses to %s.",
            method,
            peer,
            setting_name,
        )
    else:
        logger.warning(
            "SECURITY: %s is not configured. Header-asserted %s authentication is "
            "refused outright — an unvouched header is an attacker-supplied string.",
            setting_name,
            method,
        )
    logger.error(
        "Refusing header-sourced %s authentication from %s: no configured trusted "
        "proxy can vouch for these headers.",
        method,
        peer,
    )
    return False


def shared_secret_matches(presented: str | None, expected: str | None) -> bool:
    """Constant-time comparison of an optional proxy shared secret.

    Args:
        presented: The value the request carried, or ``None``.
        expected: The configured secret. Empty/``None`` means no secret is
            configured, and the check is skipped (returns True).

    Returns:
        True when no secret is configured, or when the presented value matches.
    """
    if not expected:
        return True
    if not presented:
        # Still burn a comparison so "no header at all" and "wrong header" are not
        # distinguishable by timing.
        hmac.compare_digest(expected.encode(), expected.encode())
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
