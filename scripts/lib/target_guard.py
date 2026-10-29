#!/usr/bin/env python3
"""Refuse scan targets that are this machine's live OpenTranscribe stack.

Shared by ``scripts/public-smoke.sh`` and ``scripts/zap-baseline.sh`` through
``scripts/lib/refuse_live_stack.sh``. A target is refused when its port is one the
stack publishes AND any address its host resolves to is loopback, unspecified,
link-local or one of this machine's own interface addresses. Looking at the resolved
addresses rather than the spelling closes ``0.0.0.0``, ``localhost.``, ``LOCALHOST``,
``user@localhost``, ``[::ffff:127.0.0.1]``, ``2130706433``, ``0x7f000001``, ``127.1``,
a LAN address of this host, and any DNS name that points at one of those.

The port set comes from the defaults in ``docker-compose*.yml`` and the
``FRESH_PORT_VARS`` table in ``opentr.sh``: frontend 5173, backend 5174, flower 5175,
postgres 5176, redis 5177, MinIO 5178/5179, OpenSearch 5180/5181, PKI 5182, docs 5183,
and the 5184-5190 monitoring/overlay band, plus 80 and 443 for the nginx overlay.
A ``--fresh`` stack should use ``--port-offset`` of 100 or more so it lands outside it.

Usage: target_guard.py <url> [--allow-live-dev]
Exit codes: 0 allowed, 3 refused, 2 unparseable or unresolvable (never allowed).
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
from collections.abc import Callable
from urllib.parse import urlsplit

LIVE_PORTS = frozenset(range(5173, 5191)) | {80, 443}

EXIT_ALLOWED = 0
EXIT_UNUSABLE = 2
EXIT_REFUSED = 3

Resolver = Callable[[str, int], list[str]]
LocalAddresses = Callable[[], set[str]]


class Refused(Exception):
    """The target is the live stack."""


class Unusable(Exception):
    """The target cannot be parsed or resolved; it must not be treated as safe."""


def _split_authority(url: str) -> tuple[str, int]:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ('http', 'https'):
        raise Unusable(f'not an http(s) URL: {url!r}')
    try:
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise Unusable(f'bad port in {url!r}: {exc}') from exc
    if not host:
        raise Unusable(f'no host in {url!r}')
    return host.rstrip('.').lower(), port or (443 if parts.scheme.lower() == 'https' else 80)


def _numeric_ipv4(host: str) -> str | None:
    """Decimal, hex, octal and short IPv4 spellings that libc accepts."""
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except (OSError, UnicodeError):
        return None


def _ip_literal(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host.split('%', 1)[0]))
    except ValueError:
        return None


def default_resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise Unusable(f'cannot resolve {host!r}: {exc}') from exc
    return [str(info[4][0]) for info in infos]


def default_local_addresses() -> set[str]:
    found: set[str] = set()
    try:
        out = subprocess.run(
            ['ip', '-o', 'addr', 'show'], capture_output=True, text=True, timeout=10, check=False
        ).stdout
        for line in out.splitlines():
            fields = line.split()
            if 'inet' in fields or 'inet6' in fields:
                found.add(
                    fields[fields.index('inet' if 'inet' in fields else 'inet6') + 1].split('/')[0]
                )
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(str(info[4][0]))
    except OSError:
        pass
    return found


def _normalise(addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    ip = ipaddress.ip_address(addr.split('%', 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def assert_not_live(
    url: str,
    *,
    allow_live: bool = False,
    resolve: Resolver | None = None,
    local_addresses: LocalAddresses | None = None,
) -> None:
    """Raise Refused if *url* is the live stack, Unusable if it cannot be judged."""
    host, port = _split_authority(url)
    numeric = _numeric_ipv4(host)
    literal = _ip_literal(host)
    if numeric is not None:
        raw = [numeric]
    elif literal is not None:
        raw = [literal]
    elif host == 'localhost' or host.endswith('.localhost'):
        raw = ['127.0.0.1']
    else:
        try:
            raw = (resolve or default_resolve)(host, port)
        except OSError as exc:
            raise Unusable(f'cannot resolve {host!r}: {exc}') from exc
    if not raw:
        raise Unusable(f'{host!r} resolved to no addresses')
    try:
        addrs = [_normalise(a) for a in raw]
    except ValueError as exc:
        raise Unusable(f'{host!r} resolved to an unparseable address: {exc}') from exc
    if allow_live or port not in LIVE_PORTS:
        return
    own = {_normalise(a) for a in (local_addresses or default_local_addresses)()}
    for ip in addrs:
        if ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip in own:
            raise Refused(
                f'{url} points at this machine ({ip}) on port {port}, which the live stack publishes'
            )


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != '--allow-live-dev']
    if len(args) != 1:
        print('usage: target_guard.py <url> [--allow-live-dev]', file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        assert_not_live(args[0], allow_live='--allow-live-dev' in argv)
    except Refused as exc:
        print(f'refusing {exc}.', file=sys.stderr)
        print(
            'start a --fresh stack with --port-offset 100 or more and scan that, '
            'or pass --allow-live-dev.',
            file=sys.stderr,
        )
        return EXIT_REFUSED
    except Unusable as exc:
        print(f'cannot judge target: {exc}', file=sys.stderr)
        return EXIT_UNUSABLE
    return EXIT_ALLOWED


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
