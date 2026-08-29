"""Single source of truth for "who is this request from?" (issue #284 A0.5).

Three places needed the client IP and disagreed:

* ``auth/rate_limit.py`` honoured ``RATE_LIMIT_TRUSTED_PROXIES`` but took the FIRST
  ``X-Forwarded-For`` entry, which the client controls when more than one proxy is in
  front of the app.
* ``endpoints/auth.py::_get_client_info`` used ``request.client.host`` directly, so every
  audited login recorded the reverse proxy's address instead of the user's.
* ``middleware/audit.py`` trusted ``X-Forwarded-For`` (and ``X-Real-IP``)
  **unconditionally**, so any client could forge its own audit-log address by sending
  the header.

The resolution rule here is the standard one: start at the direct peer and walk the
forwarded chain right-to-left, stepping over hops we trust. The first untrusted address
is the earliest one we can actually vouch for. Anything the client prepended is ignored,
because we stop before reaching it.

With no trusted proxies configured we return the direct peer and ignore forwarding
headers entirely — correct for a directly-exposed app, and behind a proxy it degrades to
"everyone shares the proxy's address" rather than to "anyone can claim any address".
"""

from __future__ import annotations

import ipaddress
import logging

from starlette.requests import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"


def _parse_networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse a comma-separated list of proxy IPs/CIDRs into networks."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in (raw or "").split(","):
        proxy = entry.strip()
        if not proxy:
            continue
        try:
            networks.append(ipaddress.ip_network(proxy, strict=False))
        except ValueError as exc:
            logger.warning("Invalid trusted proxy address %r: %s", proxy, exc)
    return networks


_TRUSTED_NETWORKS = _parse_networks(settings.RATE_LIMIT_TRUSTED_PROXIES)


def is_trusted_proxy(ip: str) -> bool:
    """Whether *ip* is one of the configured trusted proxies."""
    if not _TRUSTED_NETWORKS:
        return False
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(parsed in network for network in _TRUSTED_NETWORKS)


def resolve_client_ip(request: Request | None) -> str:
    """Return the most trustworthy client address for *request*.

    Args:
        request: The incoming request, or ``None`` where no request is in scope.

    Returns:
        The client IP, or ``"unknown"`` when it cannot be determined.
    """
    # ``None`` is a real case, not a test artifact: several service-layer and background
    # call sites audit an action with no request in scope, and callers reach this through
    # helpers that pass whatever they were given. It already answered UNKNOWN for a
    # request whose transport exposes no peer; a wholly absent request is the same
    # question and must not be an AttributeError — an audit record failing to resolve an
    # IP must never turn a successful config write into a 500.
    if request is None:
        return UNKNOWN

    direct_ip = str(request.client.host) if request.client else UNKNOWN

    # Without a trusted-proxy allowlist, forwarding headers are attacker-controlled.
    if not _TRUSTED_NETWORKS or not is_trusted_proxy(direct_ip):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded and _TRUSTED_NETWORKS:
            logger.warning("Ignoring X-Forwarded-For from untrusted peer %s", direct_ip)
        return direct_ip

    forwarded = request.headers.get("X-Forwarded-For")
    if not forwarded:
        # A trusted proxy that forwards no chain — fall back to X-Real-IP, which is
        # only honoured here because the direct peer is already known-trusted.
        real_ip = request.headers.get("X-Real-IP")
        return real_ip.strip() if real_ip else direct_ip

    # Walk right-to-left, skipping hops we trust; the first untrusted entry is the
    # earliest address we can vouch for. Entries the client prepended sit further left
    # and are never reached.
    for candidate in (entry.strip() for entry in reversed(forwarded.split(","))):
        if not candidate:
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            logger.warning("Malformed X-Forwarded-For entry %r", candidate)
            return direct_ip
        if not is_trusted_proxy(candidate):
            return str(candidate)

    # Every hop was trusted — the request originated from inside the trusted set.
    return direct_ip
