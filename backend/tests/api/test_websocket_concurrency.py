"""``/ws`` must stay cheap for the event loop that every other user shares.

One API process serves every WebSocket on one event loop. Two defects made a busy
deployment worse for everyone at once:

1. The handshake authenticated on the loop: a synchronous ``session_scope()`` (a pool
   checkout that can wait up to the pool timeout, then the user query) and the token
   verification. A reconnect storm (every tab after a backend restart) stalled every request
   in the process for the sum of those queries. The work now runs in the threadpool.
2. Fan-out iterated the live connection dict and lists while awaiting each send. Any socket
   connecting or disconnecting during a broadcast raised ``RuntimeError: dictionary changed
   size during iteration``; the subscriber treated that as a lost connection, dropped its
   pub/sub subscription and slept, so every notification published meanwhile was lost for
   every user. Fan-out now iterates a snapshot.
3. Fan-out awaited each send in turn with no timeout, so one client that stopped reading
   held up every other user's notifications. Sends now run concurrently, each bounded by
   ``WS_SEND_TIMEOUT_S``, and a socket that times out is dropped and closed.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message
from starlette.websockets import WebSocket
from starlette.websockets import WebSocketState

from app.api import websockets as ws_module
from app.models.user import User
from tests.fixtures.ws_handshake import access_token
from tests.fixtures.ws_handshake import authenticate_frame
from tests.fixtures.ws_handshake import cookie_header
from tests.fixtures.ws_handshake import isolated_revocation
from tests.fixtures.ws_handshake import make_user
from tests.fixtures.ws_handshake import ws_test_client


@pytest.fixture
def client(db_session, monkeypatch) -> Iterator[TestClient]:
    isolated_revocation(monkeypatch)
    with ws_test_client(db_session, monkeypatch) as test_client:
        yield test_client


@pytest.fixture
def owner(db_session) -> User:
    return make_user(db_session)


@pytest.fixture
def threads(monkeypatch) -> dict[str, int]:
    """Record which thread authenticated the socket and which one registered it."""
    seen: dict[str, int] = {}
    authenticate = ws_module._try_authenticate_token

    def _recording_authenticate(*args: Any, **kwargs: Any) -> Any:
        seen["authenticate"] = threading.get_ident()
        return authenticate(*args, **kwargs)

    connect = ws_module.manager.connect

    def _recording_connect(*args: Any, **kwargs: Any) -> None:
        seen["event_loop"] = threading.get_ident()
        connect(*args, **kwargs)

    monkeypatch.setattr(ws_module, "_try_authenticate_token", _recording_authenticate)
    monkeypatch.setattr(ws_module.manager, "connect", _recording_connect)
    return seen


def _established(socket: Any) -> bool:
    return bool(json.loads(socket.receive_text())["type"] == "connection_established")


def test_the_mounted_ws_route_is_still_the_endpoint():
    """Moving the auth into a helper must not move the route decorator onto the helper."""
    from starlette.routing import WebSocketRoute

    from app.main import app

    routes = [r for r in app.routes if isinstance(r, WebSocketRoute) and r.path == "/api/ws"]
    assert [r.endpoint for r in routes] == [ws_module.websocket_endpoint]


def test_cookie_handshake_authenticates_off_the_event_loop(client, owner, threads):
    with client.websocket_connect("/ws", headers=cookie_header(access_token(owner))) as socket:
        assert _established(socket)

    assert set(threads) == {"authenticate", "event_loop"}
    assert threads["authenticate"] != threads["event_loop"], "auth ran on the event loop"


def test_first_message_handshake_authenticates_off_the_event_loop(client, owner, threads):
    with client.websocket_connect("/ws") as socket:
        socket.send_text(authenticate_frame(access_token(owner)))
        assert _established(socket)

    assert set(threads) == {"authenticate", "event_loop"}
    assert threads["authenticate"] != threads["event_loop"], "auth ran on the event loop"


class _Peer:
    """The far end of a real ``WebSocket`` whose writes yield to the loop, like a network write."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.on_send: Callable[[], None] | None = None
        self.socket = WebSocket(
            {"type": "websocket", "path": "/ws", "headers": []}, self._receive, self._send
        )
        self.socket.application_state = WebSocketState.CONNECTED

    async def _receive(self) -> Message:
        raise AssertionError("the fan-out never reads")

    async def _send(self, message: Message) -> None:
        if self.on_send is not None:
            self.on_send()
        await asyncio.sleep(0)
        self.received.append(json.loads(message["text"]))


_EVENT = {"type": "cache_invalidate", "data": {}}


def test_a_broadcast_survives_sockets_joining_and_leaving_mid_fan_out():
    manager = ws_module.ConnectionManager()
    first, leaver, newcomer = _Peer(), _Peer(), _Peer()

    def _churn() -> None:
        manager.connect(newcomer.socket, 3)  # a new user connects ...
        manager.disconnect(leaver.socket, 2)  # ... and another one leaves

    first.on_send = _churn
    manager.connect(first.socket, 1)
    manager.connect(leaver.socket, 2)

    asyncio.run(manager.broadcast(_EVENT))

    assert first.received == [_EVENT]
    # Snapshot of the users registered when the fan-out started: a user who joined
    # meanwhile is not visited (the live iterator walked into it), and one who left
    # before their turn is skipped rather than written to.
    assert newcomer.received == []
    assert leaver.received == []
    assert set(manager.active_connections) == {1, 3}


def test_a_broadcast_survives_a_user_connecting_mid_fan_out():
    """The dict growing under the iterator raised and killed the whole fan-out."""
    manager = ws_module.ConnectionManager()
    first, second, newcomer = _Peer(), _Peer(), _Peer()
    first.on_send = lambda: manager.connect(newcomer.socket, 3)
    manager.connect(first.socket, 1)
    manager.connect(second.socket, 2)

    asyncio.run(manager.broadcast(_EVENT))

    assert first.received == [_EVENT]
    assert second.received == [_EVENT]


def test_a_personal_message_reaches_every_tab_when_one_closes_mid_send():
    manager = ws_module.ConnectionManager()
    closing, second, third = _Peer(), _Peer(), _Peer()
    closing.on_send = lambda: manager.disconnect(closing.socket, 7)  # the tab goes away
    for peer in (closing, second, third):
        manager.connect(peer.socket, 7)

    asyncio.run(manager.send_personal_message(7, _EVENT))

    # Removing a tab from the user's live list while iterating it skipped the NEXT tab.
    assert second.received == [_EVENT]
    assert third.received == [_EVENT]
    assert manager.active_connections[7] == [second.socket, third.socket]


# --------------------------------------------------------------------------- #
# One stalled client must not delay anyone else (concurrency audit F11)
#
# One subscriber task per process delivers every notification, and the fan-out awaited
# each send in turn with no timeout: a client whose TCP window was full held up delivery
# to every other user until its write drained, which for a dead peer is never.
# --------------------------------------------------------------------------- #
_SEND_TIMEOUT_S = 0.5


class _StalledPeer(_Peer):
    """A peer whose writes never complete, like a client that stopped reading."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def _send(self, message: Message) -> None:
        self.attempts += 1
        await asyncio.Event().wait()


class _TimedPeer(_Peer):
    """A healthy peer that records when (on the loop clock) each frame arrived."""

    def __init__(self) -> None:
        super().__init__()
        self.arrived_at: list[float] = []

    async def _send(self, message: Message) -> None:
        await super()._send(message)
        self.arrived_at.append(asyncio.get_running_loop().time())


def _timed(coro_factory: Callable[[], Any]) -> tuple[float, float]:
    """Run one fan-out; return ``(start, elapsed)`` on the loop clock.

    Bounded by ``wait_for`` so the old sequential, timeout-free fan-out fails the test
    instead of hanging the run.
    """

    async def _main() -> tuple[float, float]:
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.wait_for(coro_factory(), timeout=_SEND_TIMEOUT_S * 6)
        return start, loop.time() - start

    return asyncio.run(_main())


@pytest.fixture
def short_send_timeout(monkeypatch) -> None:
    monkeypatch.setattr(ws_module, "WS_SEND_TIMEOUT_S", _SEND_TIMEOUT_S, raising=False)


def test_a_stalled_client_does_not_delay_a_broadcast_to_the_others(short_send_timeout):
    manager = ws_module.ConnectionManager()
    stalled, fast = _StalledPeer(), _TimedPeer()
    # The stalled socket comes FIRST, so a sequential fan-out reaches it before the other.
    manager.connect(stalled.socket, 1)
    manager.connect(fast.socket, 2)

    start, elapsed = _timed(lambda: manager.broadcast(_EVENT))

    assert fast.received == [_EVENT]
    # Delivered while the stalled send was still pending, not after it gave up.
    assert fast.arrived_at[0] - start < _SEND_TIMEOUT_S / 2, fast.arrived_at
    # The fan-out itself is bounded by the per-send timeout (plus the bounded close).
    assert elapsed < _SEND_TIMEOUT_S * 3, elapsed
    # The stalled socket was dropped, so the next event does not wait on it again.
    assert stalled.socket not in manager.socket_owners
    assert manager.active_connections == {2: [fast.socket]}


def test_a_stalled_tab_does_not_delay_the_same_users_other_tab(short_send_timeout):
    manager = ws_module.ConnectionManager()
    stalled, fast = _StalledPeer(), _TimedPeer()
    manager.connect(stalled.socket, 7)
    manager.connect(fast.socket, 7)

    start, _elapsed = _timed(lambda: manager.send_personal_message(7, _EVENT))

    assert fast.received == [_EVENT]
    assert fast.arrived_at[0] - start < _SEND_TIMEOUT_S / 2, fast.arrived_at
    assert manager.active_connections == {7: [fast.socket]}


def test_a_dropped_socket_is_not_written_to_again(short_send_timeout):
    """After the drop, the next event goes only to the live socket and does not wait."""
    manager = ws_module.ConnectionManager()
    stalled, fast = _StalledPeer(), _TimedPeer()
    manager.connect(stalled.socket, 1)
    manager.connect(fast.socket, 2)
    _timed(lambda: manager.broadcast(_EVENT))
    attempts = stalled.attempts

    _start, elapsed = _timed(lambda: manager.broadcast(_EVENT))

    assert fast.received == [_EVENT, _EVENT]
    assert stalled.attempts == attempts
    assert elapsed < _SEND_TIMEOUT_S / 2, elapsed


def test_the_socket_can_still_disconnect_itself_after_a_drop():
    """The socket's own coroutine runs disconnect() after the fan-out already dropped it."""
    manager = ws_module.ConnectionManager()
    peer = _Peer()
    manager.connect(peer.socket, 4)
    manager.disconnect(peer.socket, 4)

    manager.disconnect(peer.socket, 4)

    assert manager.active_connections == {}
    assert manager.socket_owners == {}
