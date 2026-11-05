"""Host identity and same-origin resolution for protected-media (MediaCMS) sources.

Every "is this the configured host" decision the MediaCMS plugin makes goes through
``host_matches``, and the server-supplied download path goes through
``resolve_same_origin_url`` so a user's token is only ever sent to the configured origin.
The frontend applies the same host rule; ``backend/tests/fixtures/mediacms_host_vectors.json``
pins both sides.
"""

from urllib.parse import urljoin
from urllib.parse import urlparse

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _host_identity(host: str, scheme: str | None = None) -> tuple[str, int | None] | None:
    """Return (lower-cased hostname, port) for ``host`` or ``host:port``, or None if unusable.

    The one rule for "is this the same MediaCMS host": hostname compared case-insensitively
    and the port compared with an absent port meaning the scheme's default (when ``scheme``
    is known). Mirrored in the frontend's ``lib/utils/mediaHost.ts::hostIdentity``.
    """
    try:
        parsed = urlparse(f"//{host.strip()}")
        name = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    # userinfo ("user@host") would make a different authority look like the same host.
    if not name or "@" in host:
        return None
    return name.lower(), port or _DEFAULT_PORTS.get(scheme or "")


def host_matches(configured: str, candidate: str, scheme: str | None = None) -> bool:
    """True when a configured source host and a URL's host are the same host."""
    left = _host_identity(configured, scheme)
    return left is not None and left == _host_identity(candidate, scheme)


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """Return (scheme, host, effective port) of *url*, or None when it has no usable origin."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or _DEFAULT_PORTS.get(parsed.scheme)
    except ValueError:
        return None
    if parsed.scheme not in _DEFAULT_PORTS or not host or parsed.username or parsed.password:
        return None
    return parsed.scheme, host.lower(), port


def resolve_same_origin_url(base_url: str, server_value: str) -> str | None:
    """Resolve a server-supplied URL or path against *base_url*; None if it leaves its origin.

    The value comes from the remote server's response, so it is parsed as a reference
    (never concatenated) and the result must keep the configured scheme, host and port.
    Anything else fails closed so the user's token is only ever sent to that origin.
    """
    if not server_value or any(ord(c) < 0x20 or c == "\\" for c in server_value):
        return None
    resolved = urljoin(base_url if base_url.endswith("/") else base_url + "/", server_value)
    expected = _origin(base_url)
    if expected is None or _origin(resolved) != expected:
        return None
    return resolved
