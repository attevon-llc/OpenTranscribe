"""Selective WebSocket frame loss for browser tests (#1308 item C).

Pure and dependency-free so a unit test can pin the drop decision without Playwright or the
e2e conftest, the same reason ``stack_urls.py`` is its own module. The browser wiring uses
Playwright's documented ``Page.route_web_socket`` / ``WebSocketRoute``
(https://playwright.dev/python/docs/api/class-websocketroute): with ``connect_to_server()`` the
server's messages are forwarded to the page unless a ``server.on_message`` handler is
installed, which is where :meth:`FrameFilter.relay` sits.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


class FrameFilter:
    """Relays server frames to the page, withholding the configured frame types."""

    def __init__(self, drop_types: tuple[str, ...] = ("post_processing_status",)) -> None:
        self.drop_types = frozenset(drop_types)
        self.relayed: list[Any] = []
        self.dropped: list[Any] = []

    @staticmethod
    def frame_type(raw: str | bytes) -> str | None:
        """The frame's ``type``, or None for binary, non-JSON or non-object payloads."""
        if isinstance(raw, bytes):
            return None
        try:
            body = json.loads(raw)
        except ValueError:
            return None
        return body.get("type") if isinstance(body, dict) else None

    def relay(self, raw: str | bytes, deliver: Callable[[str | bytes], None]) -> bool:
        """Deliver ``raw`` to the page unless its type is dropped. True when delivered."""
        if self.frame_type(raw) in self.drop_types:
            self.dropped.append(raw)
            return False
        self.relayed.append(raw)
        deliver(raw)
        return True
