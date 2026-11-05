"""Host identity, same-origin resolution and request pinning for protected-media sources.

Every "is this the configured host" decision the MediaCMS plugin makes goes through
``host_matches``, the server-supplied download path goes through
``resolve_same_origin_url`` so a user's token is only ever sent to the configured origin,
and every outbound request is pinned by ``_pin_media_url``. The frontend applies the same
host rule; ``backend/tests/fixtures/mediacms_host_vectors.json`` pins both sides.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urljoin
from urllib.parse import urlparse

from app.core.exceptions import InvalidInputError
from app.utils.url_validation import resolve_pinned_target

if TYPE_CHECKING:  # pragma: no cover - import cost is paid only by type checkers
    from app.utils.url_validation import PinnedTarget

logger = logging.getLogger(__name__)

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


def _pin_media_url(url: str, *, purpose: str) -> PinnedTarget:
    """Validate *url* and pin it to the exact address that was validated.

    All three outbound calls in the MediaCMS plugin (login, media-info, download) fetch the URL
    in the same call frame that validates it, which is exactly the shape
    ``resolve_pinned_target`` exists for (see ``utils/url_validation``'s module docstring).
    The plain ``assert_safe_outbound_url`` + bare ``requests.post/get`` this used to be
    left two gaps open: ``requests`` follows redirects by default, so a server that passes
    validation on the login leg could 302 the media-info or download leg to
    ``169.254.169.254`` with nothing checking the redirect target; and validate-then-
    discard leaves a second, unchecked DNS resolution for a hostname whose records change
    between the check and the connect. Pinning closes both — the checked IP is what gets
    dialled, not the hostname — and mirrors ``llm_service.py``'s
    ``_endpoint_session``/``llm_endpoint_safety.py``'s ``_pin_llm_endpoint``, the two other places
    in this codebase that fetch a user-configured endpoint inline.

    Callers MUST send the returned target's ``url`` through
    ``pinned_requests_session(target)``, merge ``target.headers`` into their own, and pass
    ``allow_redirects=False``: a pin covers exactly one hop, and MediaCMS's REST API (a
    JSON login, a JSON media-info fetch, and a direct file download) has no legitimate
    reason to redirect any of the three.

    Args:
        url: The URL about to be fetched server-side.
        purpose: Short label for the server-side log line (e.g. "MediaCMS login").

    Returns:
        The pinned target to dial.

    Raises:
        InvalidInputError: 400, with a generic detail — the rejection reason
            distinguishes "private IP" from "cannot resolve" and would turn this endpoint
            into a network scanner if returned to the caller.
    """
    target, reason = resolve_pinned_target(url)
    if target is None:
        logger.warning("Blocked %s to %r: %s", purpose, url, reason)
        raise InvalidInputError(
            "The provided URL could not be used. It must be a publicly reachable http(s) address."
        )
    return target
