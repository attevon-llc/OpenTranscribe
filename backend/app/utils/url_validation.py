"""URL validation utilities to prevent SSRF attacks.

Every server-side fetch of a **user-supplied** URL must go through this module first
(issue #284 A0.1/A0.2/A0.10). Before that work, `is_safe_url` had exactly one caller —
the yt-dlp ingest path — while the LLM/ASR "test connection" and "model discovery"
endpoints, and the watch-source S3/SMB connectors, took an arbitrary host from any
authenticated user and fetched it with no validation at all. Combined with open
self-registration that is effectively anonymous reach into the deployment's private
network, including cloud instance-metadata endpoints.

Three layers are offered:

* :func:`is_safe_url` — boolean check, resolves DNS and rejects non-public targets.
* :func:`assert_safe_outbound_url` — the same check raising a **generic** ``HTTPException``,
  for endpoints. SSRF via these endpoints is semi-blind (the connection error is echoed
  back to the caller), so the rejection reason is logged but never returned.
* :func:`resolve_pinned_target` — the check **plus** everything the caller needs to connect
  to the address that was checked. Prefer this whenever the same code both validates and
  fetches; the two above are validate-only and therefore re-resolve at connect time.

DNS rebinding — why the first two are not enough on their own
-------------------------------------------------------------
``is_safe_url``/``assert_safe_outbound_url`` resolve the hostname, judge the answers, and
throw the answers away. The caller then hands the **hostname** to httpx/requests, which
resolves it a *second* time. A hostname whose DNS alternates between a public address and
``127.0.0.1``/``169.254.169.254`` passes the check on answer #1 and is connected to on
answer #2, so the guard is defeated without ever having to beat it.

:func:`resolve_pinned_target` closes that window: it returns the URL rewritten to the
**validated IP literal**, the original authority for the ``Host`` header, and the original
hostname for SNI + certificate verification. There is exactly one resolution, and the
address that was judged is the address that is dialled.

**TLS is not weakened by this.** Connecting to an IP while verifying the certificate
against the hostname is the whole difficulty, and both clients support it natively:

* httpx/httpcore reads ``request.extensions["sni_hostname"]`` and passes it as
  ``server_hostname`` to the TLS handshake (``httpcore/_async/connection.py``), which is
  both the SNI value and the name ``ssl`` matches the peer certificate against.
* urllib3 has the same knob as ``HTTPSConnection.server_hostname``, reachable from
  ``requests`` through an adapter's ``init_poolmanager`` — see
  :func:`pinned_requests_session`.

Neither path touches ``verify``, ``check_hostname`` or ``CERT_REQUIRED``. A certificate
that does not cover the original hostname still fails, which
``tests/unit/test_ssrf_connection_pinning.py`` proves against a real TLS server rather
than asserting it.

**Redirects are not followed on a pinned request.** A pin covers one hop; a 302 to
``http://169.254.169.254/`` would be resolved and dialled by the client with no check at
all, and would additionally reuse the pinned adapter's SNI for a different host.

Self-hosted Ollama/vLLM on a private LAN is a legitimate configuration, so callers may
pass ``allow_private=True`` — wired to ``LLM_ALLOW_PRIVATE_ENDPOINTS``, which must stay
off on any multi-tenant deployment.

``allow_private=True`` loosens the **address range**, never the metadata carve-out:
cloud instance metadata is not a private service anyone deploys, and the OIDC
discovery/test-connection paths that pass the flag document blocking it as the whole
point of calling this module. It used to skip ``_reject_reason`` wholesale, so every
``allow_private=True`` caller would happily fetch ``169.254.169.254`` — see
``tests/api/endpoints/test_admin_auth_config_audit_routes.py``.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import asynccontextmanager
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from urllib.parse import urlunparse

if TYPE_CHECKING:  # pragma: no cover - import cost is paid only by type checkers
    import aiohttp
    import requests

logger = logging.getLogger(__name__)

#: Hostnames that name an instance-metadata service. Refused even under
#: ``allow_private=True`` — unlike ``localhost``, no deployment runs its IdP or its LLM
#: behind one of these names.
METADATA_HOSTNAMES = {
    "metadata.google.internal",  # GCP metadata
    "metadata.goog",
    "instance-data",  # AWS/OpenStack metadata alias
}

# Private/reserved hostnames that should be blocked
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
} | METADATA_HOSTNAMES

#: Cloud instance-metadata addresses. Most are covered by IPv4 link-local / IPv6 ULA anyway,
#: but they are listed explicitly so the intent survives a refactor of the range checks —
#: and because this set is the ONLY thing applied under ``allow_private=True``.
METADATA_ADDRESSES = {
    "169.254.169.254",  # AWS / Azure / GCP / DigitalOcean / Oracle / Hetzner IMDS
    "169.254.170.2",  # AWS ECS task metadata
    "fd00:ec2::254",  # AWS IMDS over IPv6
    # Alibaba Cloud IMDS. Unlike the others this is NOT link-local: it sits in the RFC 6598
    # shared address space, which `is_private` does not cover (see SHARED_ADDRESS_SPACE), so
    # before it was listed here it was reachable in BOTH modes, not just under the flag.
    "100.100.100.200",
}

#: RFC 6598 carrier-grade NAT / shared address space.
#:
#: ``ipaddress`` reports this range as neither ``is_private`` **nor** ``is_global`` — measured
#: identical on CPython 3.9/3.11/3.12 — so every range check in ``_reject_reason`` returned
#: None for it and the docstring claim that ``is_private`` "covers the shared-address space"
#: was simply false. Cloud providers put internal services here (Alibaba's IMDS at
#: 100.100.100.200, EKS/GKE pod and service CIDRs), so it is not routable-public in any sense
#: this module cares about.
SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


def _metadata_reject_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a reason when *ip* is an instance-metadata address, else None.

    Applied unconditionally, including under ``allow_private=True``: the flag exists
    for a LAN IdP or a self-hosted model server, and neither of those is IMDS.
    """
    # IPv6-mapped IPv4 (e.g. ::ffff:169.254.169.254) must be judged as its IPv4 form.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if str(ip) in METADATA_ADDRESSES:
        return "Cloud metadata endpoint blocked"
    return None


def _reject_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return why *ip* is not a safe outbound target, or None if it is fine."""
    metadata_reason = _metadata_reject_reason(ip)
    if metadata_reason:
        return metadata_reason

    # IPv6-mapped IPv4 (e.g. ::ffff:169.254.169.254) must be judged as its IPv4 form.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if ip.is_unspecified:
        # 0.0.0.0 / :: — routes to "this host" on most stacks and passed every
        # is_private/is_loopback/is_reserved check before this was added.
        return f"Unspecified address: {ip}"
    if ip.is_loopback:
        return f"Loopback address: {ip}"
    if ip.is_link_local:
        return f"Link-local address: {ip}"
    if ip.is_private:
        # Covers RFC1918 and IPv6 ULA (fc00::/7). It does NOT cover RFC 6598 — that is the
        # `is_global` catch-all below, not this line.
        return f"Private IP address: {ip}"
    if ip.is_reserved:
        return f"Reserved address: {ip}"
    if ip.is_multicast:
        return f"Multicast address: {ip}"
    if not ip.is_global:
        # Catch-all for ranges that are neither private nor globally routable — RFC 6598
        # shared address space above all, which every check above misses. Written as a
        # catch-all rather than one more named range so the next such range is closed on
        # arrival instead of after the next audit. It runs AFTER the ipv4-mapped unwrap
        # above, which matters: `IPv6Address('::ffff:100.100.100.200').is_global` is True.
        return f"Non-globally-routable address: {ip}"
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

    The addresses are returned so they can be **pinned**, but this function does not pin
    anything and a caller that discards them has a validate-then-re-resolve check with a
    DNS-rebinding window in it. Use :func:`resolve_pinned_target` unless you genuinely only
    need the yes/no answer.
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

    lowered = hostname.lower()
    if lowered in METADATA_HOSTNAMES or (not allow_private and lowered in BLOCKED_HOSTNAMES):
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

        reason = _reject_reason(ip) if not allow_private else _metadata_reject_reason(ip)
        if reason:
            return [], reason
        addresses.append(ip_str)

    if not addresses:
        return [], f"Cannot resolve hostname: {hostname}"
    return addresses, ""


@dataclass(frozen=True)
class PinnedTarget:
    """A validated URL, rewritten to dial the exact address that was validated.

    Produced by :func:`resolve_pinned_target`. Send ``url`` with ``headers`` merged into
    your own and — for httpx — ``httpx_extensions`` passed as ``extensions=``.

    Two shapes are offered because the three HTTP clients in this codebase pin
    differently, and neither shape weakens TLS:

    * **URL rewriting** (httpx, requests) — send ``url`` (host replaced by the address)
      with ``headers`` and, for httpx, ``httpx_extensions``.
    * **Resolver override** (aiohttp) — send ``original_url`` through
      :func:`pinned_aiohttp_session`, whose resolver only ever answers ``address``. The
      request keeps its hostname throughout, so aiohttp derives SNI and the certificate
      name from it with nothing to override.

    Attributes:
        original_url: The URL exactly as given. Use with resolver-level pinning.
        url: The request URL with the host replaced by ``address`` (IPv6 bracketed).
            Identical to ``original_url`` when the input already named an IP literal.
        address: The validated IP the connection goes to.
        hostname: The original hostname. Used for SNI **and** certificate verification —
            never dropped, never replaced by the address.
        host_header: The original authority (``host`` or ``host:port``), so the origin
            server still sees the name it was asked for and virtual hosting works.
        scheme: ``http`` or ``https``.
        pinned: False when the URL already named an IP literal, i.e. there was no DNS
            step to rebind and nothing to rewrite.
    """

    original_url: str
    url: str
    address: str
    hostname: str
    host_header: str
    scheme: str
    pinned: bool

    @property
    def headers(self) -> dict[str, str]:
        """Headers that must accompany the pinned request."""
        return {"Host": self.host_header} if self.pinned else {}

    @property
    def httpx_extensions(self) -> dict[str, str]:
        """``extensions=`` for httpx, carrying SNI/verification hostname over TLS.

        Empty for plaintext and for un-pinned literals: httpcore only reads
        ``sni_hostname`` on the TLS path, and for a literal the URL host is already right.
        """
        if not self.pinned or self.scheme != "https":
            return {}
        return {"sni_hostname": self.hostname}


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def resolve_pinned_target(
    url: str, *, allow_private: bool = False
) -> tuple[PinnedTarget | None, str]:
    """Validate *url* and return the material needed to connect to the checked address.

    This is :func:`resolve_public_addresses` with its promise actually kept. Use it in place
    of ``is_safe_url``/``assert_safe_outbound_url`` wherever the validating code is also the
    fetching code — those two resolve, judge, discard, and let the HTTP client resolve
    again, which is a DNS-rebinding window an attacker-controlled hostname wins by design.

    The first validated address is pinned. All of them passed, so any is safe; the client's
    own multi-address failover is given up in exchange for there being exactly one
    resolution. A host whose only working address is not the first will fail to connect
    rather than silently reach an unchecked one.

    Args:
        url: The URL about to be fetched server-side.
        allow_private: Permit private/loopback targets (self-hosted LLM/IdP endpoints).

    Returns:
        ``(target, "")`` on success, or ``(None, reason)`` when the URL must be refused.
    """
    addresses, reason = resolve_public_addresses(url, allow_private=allow_private)
    if not addresses:
        return None, reason

    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    userinfo, _, authority = parsed.netloc.rpartition("@")
    address = addresses[0]

    if _is_ip_literal(hostname):
        # Nothing resolved, so nothing can rebind. Leave the request untouched rather than
        # rewriting it into an equivalent-but-different form.
        return (
            PinnedTarget(
                original_url=url,
                url=url,
                address=hostname,
                hostname=hostname,
                host_header=authority,
                scheme=parsed.scheme,
                pinned=False,
            ),
            "",
        )

    host_part = f"[{address}]" if ":" in address else address
    port_part = f":{parsed.port}" if parsed.port is not None else ""
    netloc = f"{userinfo}@{host_part}{port_part}" if userinfo else f"{host_part}{port_part}"
    return (
        PinnedTarget(
            original_url=url,
            url=urlunparse(parsed._replace(netloc=netloc)),
            address=address,
            hostname=hostname,
            host_header=authority,
            scheme=parsed.scheme,
            pinned=True,
        ),
        "",
    )


@contextmanager
def pinned_requests_session(target: PinnedTarget) -> Iterator[requests.Session]:
    """A ``requests.Session`` that verifies TLS against ``target.hostname``.

    ``requests`` has no per-request SNI override, so the hostname is bound to a dedicated
    session via an adapter that forwards ``server_hostname`` into urllib3's connection
    pool. urllib3 hands that value to the TLS handshake, which is what ``ssl`` matches the
    peer certificate against — so verification stays on and stays bound to the *name*,
    while the socket goes to the *address* named in ``target.url``.

    Because the SNI hostname is bound to the session, the session must not be reused for
    another host, and requests made through it must pass ``allow_redirects=False``.

    Args:
        target: The pinned target from :func:`resolve_pinned_target`.

    Yields:
        A session to issue exactly the pinned request through. Closed on exit.
    """
    import requests
    from requests.adapters import HTTPAdapter

    class _PinnedAdapter(HTTPAdapter):
        def init_poolmanager(self, *args: object, **kwargs: object) -> None:
            kwargs["server_hostname"] = target.hostname
            super().init_poolmanager(*args, **kwargs)  # type: ignore[arg-type]

    session = requests.Session()
    if target.pinned and target.scheme == "https":
        session.mount("https://", _PinnedAdapter())
    try:
        yield session
    finally:
        session.close()


@asynccontextmanager
async def pinned_aiohttp_session(
    target: PinnedTarget, *, timeout_seconds: float = 10.0
) -> AsyncIterator[aiohttp.ClientSession]:
    """An ``aiohttp.ClientSession`` whose resolver only ever answers ``target.address``.

    aiohttp is the one client here that pins **cleanly**: ``TCPConnector(resolver=...)``
    replaces DNS outright, so the request keeps its original hostname end to end and
    aiohttp derives SNI and the certificate name from it by itself
    (``connector.py``: ``server_hostname = (req.server_hostname or host)``). Nothing about
    TLS is overridden, which is why this helper takes ``target.original_url`` rather than
    the rewritten ``target.url``.

    The resolver **refuses any other hostname** rather than answering it with the pinned
    address: a redirect to ``evil.example`` must not be silently served the IP that was
    approved for a different name. Requests through this session must still pass
    ``allow_redirects=False`` — refusing at the resolver produces a confusing error where
    the caller wanted a clean "we do not follow redirects".

    Args:
        target: The pinned target from :func:`resolve_pinned_target`.
        timeout_seconds: Total request timeout.

    Yields:
        A session for exactly the pinned request.
    """
    import aiohttp
    from aiohttp.abc import AbstractResolver
    from aiohttp.abc import ResolveResult

    address = target.address
    addr_family = socket.AF_INET6 if ":" in address else socket.AF_INET
    expected = target.hostname.lower()

    class _PinnedResolver(AbstractResolver):
        async def resolve(
            self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
        ) -> list[ResolveResult]:
            if host.lower().rstrip(".") != expected:
                raise OSError(
                    f"Refusing to resolve {host!r}: this connection is pinned to {expected!r}"
                )
            return [
                ResolveResult(
                    hostname=host,
                    host=address,
                    port=port,
                    family=addr_family,
                    proto=socket.IPPROTO_TCP,
                    flags=0,
                )
            ]

        async def close(self) -> None:
            return None

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    connector = aiohttp.TCPConnector(resolver=_PinnedResolver(), family=addr_family)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        yield session


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
