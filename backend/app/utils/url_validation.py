"""URL validation utilities to prevent SSRF attacks.

Every server-side fetch of a **user-supplied** URL must go through this module first
(issue #284 A0.1/A0.2/A0.10). Before that work, `is_safe_url` had exactly one caller —
the yt-dlp ingest path — while the LLM/ASR "test connection" and "model discovery"
endpoints, and the watch-source S3/SMB connectors, took an arbitrary host from any
authenticated user and fetched it with no validation at all. Combined with open
self-registration that is effectively anonymous reach into the deployment's private
network, including cloud instance-metadata endpoints.

Two layers are offered:

* :func:`is_safe_url` — boolean check, resolves DNS and rejects non-public targets.
* :func:`assert_safe_outbound_url` — the same check raising a **generic** ``HTTPException``,
  for endpoints. SSRF via these endpoints is semi-blind (the connection error is echoed
  back to the caller), so the rejection reason is logged but never returned.

Self-hosted Ollama/vLLM on a private LAN is a legitimate configuration, so callers may
pass ``allow_private=True`` — wired to ``LLM_ALLOW_PRIVATE_ENDPOINTS``, which must stay
off on any multi-tenant deployment.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Private/reserved hostnames that should be blocked
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",  # GCP metadata
    "metadata.goog",
    "instance-data",  # AWS/OpenStack metadata alias
}

#: Cloud instance-metadata addresses. IPv4 link-local and IPv6 ULA already cover these,
#: but they are listed explicitly so the intent survives a refactor of the range checks.
METADATA_ADDRESSES = {
    "169.254.169.254",  # AWS / Azure / GCP / DigitalOcean IMDS
    "169.254.170.2",  # AWS ECS task metadata
    "fd00:ec2::254",  # AWS IMDS over IPv6
}


def _reject_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return why *ip* is not a safe outbound target, or None if it is fine."""
    # IPv6-mapped IPv4 (e.g. ::ffff:169.254.169.254) must be judged as its IPv4 form.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if str(ip) in METADATA_ADDRESSES:
        return "Cloud metadata endpoint blocked"
    if ip.is_unspecified:
        # 0.0.0.0 / :: — routes to "this host" on most stacks and passed every
        # is_private/is_loopback/is_reserved check before this was added.
        return f"Unspecified address: {ip}"
    if ip.is_loopback:
        return f"Loopback address: {ip}"
    if ip.is_link_local:
        return f"Link-local address: {ip}"
    if ip.is_private:
        # Covers RFC1918, IPv6 ULA (fc00::/7), and the shared-address space.
        return f"Private IP address: {ip}"
    if ip.is_reserved:
        return f"Reserved address: {ip}"
    if ip.is_multicast:
        return f"Multicast address: {ip}"
    return None


def resolve_public_addresses(url: str, *, allow_private: bool = False) -> tuple[list[str], str]:
    """Resolve *url*'s host and validate **every** address it resolves to.

    All records are checked, not just the first: a hostname with one public and one
    private A record would otherwise pass validation and then connect to the private
    address on a subsequent resolution.

    Args:
        url: The URL whose host should be resolved.
        allow_private: Permit private/loopback targets (self-hosted LLM endpoints).

    Returns:
        ``(addresses, "")`` on success, or ``([], reason)`` when the URL must be refused.
        The returned addresses can be pinned by the caller to narrow the DNS-rebinding
        window between validation and connection.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return [], "Invalid URL format"

    if parsed.scheme not in ("http", "https"):
        return [], "Only HTTP/HTTPS URLs are allowed"

    hostname = parsed.hostname
    if not hostname:
        return [], "No hostname in URL"

    if not allow_private and hostname.lower() in BLOCKED_HOSTNAMES:
        return [], f"Blocked hostname: {hostname}"

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        # urlparse defers port parsing, so a malformed port raises on access.
        return [], "Invalid port in URL"

    try:
        addr_infos = socket.getaddrinfo(hostname, port)
    except (socket.gaierror, UnicodeError):
        return [], f"Cannot resolve hostname: {hostname}"

    addresses: list[str] = []
    for _family, _, _, _, sockaddr in addr_infos:
        # sockaddr is (host, port) for IPv4 and (host, port, flowinfo, scope_id) for
        # IPv6; the first element is always the address string.
        ip_str = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return [], f"Unparseable address for {hostname}"

        if not allow_private:
            reason = _reject_reason(ip)
            if reason:
                return [], reason
        addresses.append(ip_str)

    if not addresses:
        return [], f"Cannot resolve hostname: {hostname}"
    return addresses, ""


def is_safe_url(url: str, *, allow_private: bool = False) -> tuple[bool, str]:
    """Validate URL is not targeting internal/private resources.

    Args:
        url: The URL to validate.
        allow_private: Permit private/loopback targets (self-hosted LLM endpoints).

    Returns:
        Tuple of (is_safe, reason_if_blocked).
    """
    addresses, reason = resolve_public_addresses(url, allow_private=allow_private)
    return (bool(addresses), reason)


def assert_safe_outbound_url(url: str, *, purpose: str, allow_private: bool = False) -> None:
    """Refuse a user-supplied outbound URL, without telling the caller why.

    The rejection reason distinguishes "private IP" from "cannot resolve", which turns
    the endpoint into a network scanner. It is logged server-side and replaced with a
    generic message in the response.

    Args:
        url: The user-supplied URL about to be fetched server-side.
        purpose: Short label for the log line (e.g. "LLM test-connection").
        allow_private: Permit private/loopback targets.

    Raises:
        fastapi.HTTPException: 400 if the URL must not be fetched.
    """
    from fastapi import HTTPException

    safe, reason = is_safe_url(url, allow_private=allow_private)
    if not safe:
        logger.warning("Blocked %s to %r: %s", purpose, url, reason)
        raise HTTPException(
            status_code=400,
            detail=(
                "The provided URL could not be used. It must be a publicly reachable "
                "http(s) address."
            ),
        )
