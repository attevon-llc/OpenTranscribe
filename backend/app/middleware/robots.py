"""Tell crawlers not to index API responses (issue #668).

`frontend/static/robots.txt` covers the pages a crawler discovers by following links, but a
crawler that hits an API URL directly (a shared link, a stray reference, a misconfigured
sitemap) never reads that file — it just requests the resource. `X-Robots-Tag` is the
per-response equivalent: it works even when nothing served an HTML page to read `<meta
robots>` from, and search engines honour it identically on any content type.

Scoped to `API_PREFIX` only. The frontend SPA and `/docs/` are legitimate to index on a
deliberately public instance (issue #628's demo); nothing here should stop that.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

from app.core.config import settings


class RobotsHeaderMiddleware:
    """Pure ASGI: adds `X-Robots-Tag: noindex, nofollow` to every response under `API_PREFIX`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._prefix = settings.API_PREFIX

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self._prefix):
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Robots-Tag"] = "noindex, nofollow"
            await send(message)

        await self.app(scope, receive, send_with_header)
