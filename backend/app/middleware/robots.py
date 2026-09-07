"""Tell crawlers not to index API responses (issue #668, finding 3).

`frontend/static/robots.txt` covers the pages a crawler discovers by following links, but a
crawler that hits an API URL directly (a shared link, a stray reference, a misconfigured
sitemap) never reads that file — it just requests the resource. `X-Robots-Tag` is the
per-response equivalent: it works even when nothing served an HTML page to read `<meta
robots>` from, and search engines honour it identically on any content type.

Scoped to `API_PREFIX` only. The frontend SPA and `/docs/` are legitimate to index on a
deliberately public instance (issue #628's demo); nothing here should stop that.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from app.core.config import settings


class RobotsHeaderMiddleware(BaseHTTPMiddleware):
    """Adds `X-Robots-Tag: noindex, nofollow` to every response under `API_PREFIX`."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._prefix = settings.API_PREFIX

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(self._prefix):
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response
