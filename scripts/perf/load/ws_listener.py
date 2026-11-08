"""A browser tab's live-notification socket (``/api/ws``), held for the whole run.

Locust has no built-in WebSocket client, and its socket.io user (``locust.contrib.socketio``)
speaks the socket.io protocol, which ``/api/ws`` does not. So this follows Locust's documented
pattern for other protocols ("Testing other systems/protocols"): a plain client
(``websocket-client``, already pinned as a Locust dependency) wrapped so every connect, echo and
notification is reported through ``events.request``, the way ``locust.contrib.socketio`` itself
does it.

The socket authenticates with the ``access_token`` cookie the sign-in set, and sends the
``Origin`` a browser on the backend's own address would. Every ping is echoed by the server
(``api/websockets.py``), so the round trip measures how quickly the event loop answers under
load; every other frame (a notification) is counted by its ``type``.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import gevent
import scenario
import websocket
from locust import between, task
from personas_base import SeededUser

logger = logging.getLogger('ot-load')


class WebSocketListener(SeededUser):
    """Holds one live-notification socket and pings it every half minute."""

    weight = 25
    pool = 'ws'
    wait_time = between(20, 40)

    def on_start(self) -> None:
        super().on_start()
        self.ws: Any = None
        self.pending: dict[str, float] = {}
        self._stopping = False
        self._connect()

    def on_stop(self) -> None:
        self._stopping = True
        self._close()

    def _connect(self) -> None:
        token = scenario.cookie_value(self.client.cookiejar, 'access_token')
        started = time.perf_counter()
        try:
            self.ws = websocket.create_connection(
                scenario.ws_url(self.host),
                cookie=f'access_token={token}',
                origin=self.host,
                timeout=self.timeout,
            )
            hello = json.loads(self.ws.recv())
            if hello.get('type') != 'connection_established':
                raise RuntimeError(f'unexpected first frame {hello.get("type")!r}')
        except (websocket.WebSocketException, OSError, ValueError, RuntimeError) as exc:
            self._fire('WS', 'connect', started, error=exc)
            self._close()
            return
        self._fire('WS', 'connect', started)
        gevent.spawn(self._receive_loop)

    def _close(self) -> None:
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                ws.close()
            except (websocket.WebSocketException, OSError):
                logger.debug('socket was already closed')

    def _receive_loop(self) -> None:
        while self.ws is not None:
            self._receive_once()

    def _receive_once(self) -> None:
        ws = self.ws
        if ws is None:
            return
        started = time.perf_counter()
        try:
            frame = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            return  # quiet socket; keep listening
        except (websocket.WebSocketException, OSError, ValueError) as exc:
            if not self._stopping:  # Locust closing its own socket at run end is not a drop
                self._fire('WS', 'dropped', started, error=exc)
            self._close()
            return
        kind = str(frame.get('type', 'unknown'))
        if kind == 'echo':
            nonce = _nonce(frame.get('data'))
            sent = self.pending.pop(nonce, None) if nonce else None
            if sent is not None:
                self._fire('WS', 'echo round trip', sent)
            return
        self._fire('WS', f'notification {kind}', started)

    @task
    def ping(self) -> None:
        if self.ws is None:
            self._connect()  # the previous socket dropped: reconnect as the SPA does
            return
        nonce = uuid.uuid4().hex
        self.pending[nonce] = time.perf_counter()
        try:
            self.ws.send(json.dumps({'type': 'ping', 'nonce': nonce}))
        except (websocket.WebSocketException, OSError) as exc:
            self.pending.pop(nonce, None)
            self._fire('WS', 'dropped', time.perf_counter(), error=exc)
            self._close()


def _nonce(data: Any) -> str | None:
    """The nonce of a ping the server echoed back (it echoes the raw text it received)."""
    try:
        sent = json.loads(data) if isinstance(data, str) else None
    except ValueError:
        return None
    return sent.get('nonce') if isinstance(sent, dict) else None
