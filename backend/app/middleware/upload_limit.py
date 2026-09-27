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

With ``API_MEDIATED_UPLOAD_ENABLED`` off, ``POST /api/files`` is refused outright —
404 before a byte is read (issue #1008).
"""

from __future__ import annotations

import re

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

from app.core.config import settings
from app.core.constants import MAX_AVATAR_SIZE

#: Allowance on top of a file ceiling for the multipart envelope: boundaries, part
#: headers and the small form fields sent alongside the file.
MULTIPART_OVERHEAD_BYTES = 1024 * 1024


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
            return None
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

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    # FastAPI re-raises an HTTPException from body parsing as-is, so
                    # this becomes the 413 response; the parser stops reading here.
                    raise _BodyTooLarge(detail)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            # Reached only if nothing inside rendered it (e.g. a non-FastAPI app).
            if response_started:
                raise
            await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
