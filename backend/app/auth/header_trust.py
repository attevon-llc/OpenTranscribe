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
import struct
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class RequestLike(Protocol):
    """The request attributes the header-trust and identity-header code reads.

    A Starlette/FastAPI ``Request`` satisfies it structurally; this layer does not import the
    web framework.
    """

    @property
    def headers(self) -> Mapping[str, str]: ...
    @property
    def client(self) -> object | None: ...


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


#: Linux's IPv4 routing table for this network namespace (proc(5), ``/proc/net/route``).
ROUTE_TABLE = Path("/proc/net/route")

#: ``RTF_GATEWAY`` from ``linux/route.h``: the route goes through a gateway.
_RTF_GATEWAY = 0x0002


def default_gateways(route_table: Path | None = None) -> frozenset[str]:
    """IPv4 default gateways of this process's network namespace.

    In a container on a Docker bridge network the default gateway is the host's own address on
    that bridge (Docker docs, "Port publishing and mapping": an address is assigned to the bridge
    so host processes can reach the network). Every connection the host opens to the container,
    and every connection Docker's userland proxy relays to a published port, arrives from it:
    the proxy opens a new connection whose source is the gateway (docker/docs issue #17312). So
    that address stands for "the host, or anyone the host relayed", never for the reverse proxy
    that terminated authentication, which reaches the backend from its own container address.

    Args:
        route_table: The kernel table to read; :data:`ROUTE_TABLE` when omitted, looked
            up at call time so a test can point it elsewhere. One header line, then
            whitespace-separated ``Iface Destination Gateway Flags ...``, the addresses as
            hex of their in-memory (network-order) bytes, so they unpack in native order.

    Returns:
        The gateway addresses of every default route, or an empty set when the table cannot be
        read (not Linux), which leaves the allowlist exactly as configured.
    """
    try:
        rows = (route_table or ROUTE_TABLE).read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        return frozenset()
    gateways: set[str] = set()
    for row in rows:
        fields = row.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            gateway, flags = int(fields[2], 16), int(fields[3], 16)
        except ValueError:
            continue
        if flags & _RTF_GATEWAY and gateway:
            gateways.add(str(ipaddress.IPv4Address(struct.pack("=I", gateway))))
    return frozenset(gateways)


def immediate_peer_ip(request: RequestLike) -> str:
    """Return the address of the host that opened this connection.

    Deliberately NOT ``utils/client_ip.resolve_client_ip`` — see rule 1 in the
    module docstring. ``request.client`` is that host only because uvicorn runs with
    ``--no-proxy-headers`` (``scripts/api-start.sh``, the dev override): uvicorn's
    proxy-header handling would replace it with the forwarded end user.

    Args:
        request: Starlette/FastAPI request (or any object exposing ``.client``).

    Returns:
        The peer address, or :data:`UNKNOWN_PEER` when the transport has none.
    """
    client = getattr(request, "client", None)
    if client is not None:
        return str(getattr(client, "host", "") or UNKNOWN_PEER)
    return UNKNOWN_PEER


def header_source_is_trusted(
    request: RequestLike, networks: Sequence[TrustedNetwork], *, secret_vouched: bool = False
) -> bool:
    """Whether the peer that delivered *request* may assert identity headers.

    One rule, with no environment-dependent relaxation: the immediate peer must be
    in the configured allowlist, and must not be this container's default gateway
    (:func:`default_gateways`). The gateway sits inside the very container subnet an
    operator allow-lists for the proxy, yet it is the host, and every client Docker's
    userland proxy relays to a published port. Trusting it let anyone who could reach
    the backend's port sign in as any DN. An empty allowlist trusts nobody.

    Args:
        request: The incoming request.
        networks: Parsed trusted-proxy allowlist.
        secret_vouched: The request carried a configured shared secret that matched
            (:func:`shared_secret_matches`, constant time). The secret then authenticates
            the hop itself, so a proxy run on the host may reach the backend from the
            gateway. Only trusted-header auth has a secret; PKI never passes this.
    """
    if not networks:
        return False
    peer = immediate_peer_ip(request)
    if not ip_in_networks(peer, networks):
        return False
    return secret_vouched or peer not in default_gateways()


def header_assertion_permitted(
    request: RequestLike,
    networks: Sequence[TrustedNetwork],
    *,
    asserted: bool,
    method: str,
    setting_name: str,
    secret_vouched: bool = False,
) -> bool:
    """Decide whether to accept identity headers on *request*.

    Args:
        request: The incoming request.
        networks: Parsed trusted-proxy allowlist for this auth method.
        asserted: Whether the request actually carries any of the method's identity
            headers. When it does not there is nothing to refuse.
        method: Auth-method name, for the log line (``pki`` / ``proxy``).
        setting_name: The allowlist setting an operator would fix.
        secret_vouched: See :func:`header_source_is_trusted`.

    Returns:
        True to continue (trusted peer, or nothing asserted), False to refuse.
    """
    if header_source_is_trusted(request, networks, secret_vouched=secret_vouched):
        return True

    if not asserted:
        # Nothing was claimed, so nothing is being refused — let the request fall
        # through to the other authentication methods.
        return True

    peer = immediate_peer_ip(request)
    if peer in default_gateways():
        logger.warning(
            "%s identity headers received from %s, this container's default gateway: the "
            "address the host and Docker's userland proxy connect from, so it vouches for "
            "nobody even where %s covers it. Send them through the reverse-proxy container "
            "on the compose network, or, for trusted-header auth, set a shared secret.",
            method,
            peer,
            setting_name,
        )
    elif networks:
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
