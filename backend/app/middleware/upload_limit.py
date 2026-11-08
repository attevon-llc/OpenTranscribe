"""Enforce upload size limits while the request body streams (issue #999).

FastAPI parses a multipart body — spooling each file part to a temp file — *before*
the route handler runs, so a size check in the handler comes after the whole body is
already on disk. One oversized (or endless chunked) request could fill ``/tmp`` and
get the backend evicted. This middleware sits in front of the body parser on the
routes that accept an ``UploadFile`` through the API:

* ``POST /api/files`` — the API-mediated upload. Its ceiling is the global
  ``MAX_UPLOAD_BYTES`` that ``validate_file_size_for_tenant`` already enforces, plus
  :data:`MULTIPART_OVERHEAD_BYTES` for form framing. Deliberately never a lower fixed
  number: the browser falls back to this route for files the presigned flow could not
  take. A per-tenant ceiling needs the authenticated org, which is not known before
  the body is parsed; it is still enforced by the handler, and it can only ever be
  tighter than the global one, so disk use stays bounded by the global ceiling.
* ``POST /api/speaker-profiles/profiles/{uuid}/avatar`` — capped at the avatar limit.

A ``Content-Length`` over the ceiling is refused before a byte is read; otherwise the
bytes are counted as they arrive and the request is cut off with 413 as soon as the
count passes the ceiling. The presigned single-PUT and multipart paths never carry
file bytes through the API and are not governed here.

Every other route is held to the much smaller ``MAX_REQUEST_BODY_BYTES`` by Starlette's
``RequestBodyLimitMiddleware``, installed app-wide in ``app/main.py``. The counting here
is that same middleware, wrapped around the governed route: a nested
``RequestBodyLimitMiddleware`` replaces the active request's limit (Starlette 1.10,
``starlette.max_body_size`` scope key), which is how a route raises the app-wide
ceiling to its own. This module only adds the route table, the route-specific 413
message and the early refusal of a declared ``Content-Length``.

With ``API_MEDIATED_UPLOAD_ENABLED`` off, ``POST /api/files`` is refused outright —
404 before a byte is read (issue #1008).
"""

from __future__ import annotations

import re

from starlette.exceptions import HTTPException
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

from app.core.config import settings
from app.core.constants import MAX_AVATAR_SIZE
from app.core.exceptions import DomainError

#: Allowance on top of a file ceiling for the multipart envelope: boundaries, part
#: headers and the small form fields sent alongside the file.
MULTIPART_OVERHEAD_BYTES = 1024 * 1024

#: The raised limit for ``POST /api/files`` when ``MAX_UPLOAD_BYTES`` is disabled.
_NO_PRACTICAL_LIMIT = 2**63 - 1


_API_UPLOAD_DISABLED_DETAIL = (
    "Uploading through the API is disabled on this server; use the direct upload flow."
)


def _is_api_upload_route(path: str) -> bool:
    prefix = settings.API_PREFIX
    return path in (f"{prefix}/files", f"{prefix}/files/")


def _limit_for(path: str) -> tuple[int, str] | None:
    """``(max_body_bytes, detail)`` for a governed POST route, else None."""
    prefix = settings.API_PREFIX
    if _is_api_upload_route(path):
        ceiling = settings.MAX_UPLOAD_BYTES
        if not ceiling:
            # No upload ceiling configured: the route must still be exempted from the
            # app-wide MAX_REQUEST_BODY_BYTES, so it is raised to no practical limit.
            return (_NO_PRACTICAL_LIMIT, "")
        return (
            ceiling + MULTIPART_OVERHEAD_BYTES,
            f"File exceeds the maximum upload size of {ceiling / (1024**3):.1f} GB.",
        )
    if re.fullmatch(rf"{re.escape(prefix)}/speaker-profiles/profiles/[^/]+/avatar/?", path):
        return (
            MAX_AVATAR_SIZE + MULTIPART_OVERHEAD_BYTES,
            f"File too large. Maximum size is {MAX_AVATAR_SIZE // (1024 * 1024)}MB.",
        )
    return None


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class _BodyTooLarge(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(status_code=413, detail=detail)


class _RouteSpecificTooLarge:
    """Re-raise Starlette's generic body-limit 413 with the governed route's own message.

    FastAPI re-raises an ``HTTPException`` from body parsing as-is, so the route's
    message reaches the client as the 413's ``detail``; the parser stops reading there.
    """

    def __init__(self, app: ASGIApp, detail: str) -> None:
        self.app = app
        self.detail = detail

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def translated_receive() -> Message:
            try:
                return await receive()
            except (HTTPException, DomainError) as exc:
                if exc.status_code == 413:
                    raise _BodyTooLarge(self.detail) from exc
                raise

        await self.app(scope, translated_receive, send)


class UploadBodyLimitMiddleware:
    """Pure ASGI middleware: it has to wrap ``receive``, which BaseHTTPMiddleware hides."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        if _is_api_upload_route(scope["path"]) and not settings.API_MEDIATED_UPLOAD_ENABLED:
            await JSONResponse({"detail": _API_UPLOAD_DISABLED_DETAIL}, status_code=404)(
                scope, receive, send
            )
            return
        limit = _limit_for(scope["path"])
        if limit is None:
            await self.app(scope, receive, send)
            return
        max_bytes, detail = limit

        declared = _declared_length(scope)
        if declared is not None and declared > max_bytes:
            await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        limited = RequestBodyLimitMiddleware(
            _RouteSpecificTooLarge(self.app, detail), max_body_size=max_bytes
        )
        try:
            await limited(scope, receive, tracking_send)
        except _BodyTooLarge:
            # Reached only if nothing inside rendered it (e.g. a non-FastAPI app).
            if response_started:
                raise
            await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
