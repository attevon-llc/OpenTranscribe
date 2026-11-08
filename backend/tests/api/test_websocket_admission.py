"""WebSocket admission limits and the non-blocking fan-out (concurrency audit M7).

* the subscriber task only enqueues: a stalled socket cannot delay the next notification;
* each socket's queue is bounded, and a socket that cannot keep up is dropped;
* the subscriber task is referenced, so it cannot be garbage collected mid-run;
* handshakes per IP per minute are counted in Redis (real Redis, real MULTI);
* the per-process and per-user socket caps refuse with 1013 (try again later).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
import redis.asyncio as aioredis
from fastapi.testclient import TestClient
from starlette.types import Message
from starlette.websockets import WebSocket
from starlette.websockets import WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.api import websockets as ws_module
from app.core.config import settings
from tests.fixtures.ws_handshake import access_token
from tests.fixtures.ws_handshake import cookie_header
from tests.fixtures.ws_handshake import isolated_revocation
from tests.fixtures.ws_handshake import make_user
from tests.fixtures.ws_handshake import ws_test_client

_EVENT = {"type": "cache_invalidate", "data": {}}
_SEND_TIMEOUT_S = 0.5
#: RFC 6455 "Try Again Later", the admission refusal (TRY_AGAIN_LATER).
TRY_AGAIN_LATER = 1013


class _Peer:
    def __init__(self, stall: bool = False) -> None:
        self.stall = stall
        self.received: list[dict[str, Any]] = []
        self.socket = WebSocket(
            {"type": "websocket", "path": "/ws", "headers": []}, self._receive, self._send
        )
        self.socket.application_state = WebSocketState.CONNECTED

    async def _receive(self) -> Message:
        raise AssertionError("the fan-out never reads")

    async def _send(self, message: Message) -> None:
        if self.stall and message["type"] == "websocket.send":
            await asyncio.Event().wait()  # stops reading frames; a close still completes
        await asyncio.sleep(0)
        self.received.append(json.loads(message["text"]))


@pytest.fixture
def short_send_timeout(monkeypatch) -> None:
    monkeypatch.setattr(ws_module, "WS_SEND_TIMEOUT_S", _SEND_TIMEOUT_S)


def _message(event: dict[str, Any]) -> dict[str, Any]:
    return {"type": "message", "data": json.dumps({**event, "broadcast": True})}


def test_a_stalled_socket_does_not_hold_the_subscriber(short_send_timeout, monkeypatch):
    manager = ws_module.ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", manager)
    stalled, fast = _Peer(stall=True), _Peer()
    manager.connect(stalled.socket, 1)
    manager.connect(fast.socket, 2)

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        for _ in range(3):
            await ws_module._dispatch_notification(_message(_EVENT))
        dispatched = loop.time() - start
        # Three notifications went through the subscriber's dispatch while one socket's
        # first send was still pending; none of them waited on it.
        assert dispatched < _SEND_TIMEOUT_S / 5, dispatched
        await asyncio.wait_for(manager.flush(), timeout=_SEND_TIMEOUT_S * 6)

    asyncio.run(_main())

    assert fast.received == [_EVENT, _EVENT, _EVENT]
    assert stalled.socket not in manager.socket_owners


def test_a_socket_that_cannot_keep_up_is_dropped_when_its_queue_fills(monkeypatch):
    monkeypatch.setattr(settings, "WS_SEND_QUEUE_SIZE", 2)
    monkeypatch.setattr(ws_module, "WS_SEND_TIMEOUT_S", 30.0)  # the queue, not the timeout
    manager = ws_module.ConnectionManager()
    stalled, fast = _Peer(stall=True), _Peer()
    manager.connect(stalled.socket, 1)
    manager.connect(fast.socket, 2)

    async def _main() -> None:
        for _ in range(4):  # 1 in flight + 2 queued, the 4th overflows
            await manager.broadcast(_EVENT)
            await asyncio.sleep(0)
        await asyncio.wait_for(manager.flush(), timeout=5)

    asyncio.run(_main())

    assert stalled.socket not in manager.socket_owners
    assert stalled.socket not in manager.outboxes
    assert fast.received == [_EVENT] * 4


def test_the_subscriber_task_is_referenced(monkeypatch):
    monkeypatch.setattr(ws_module, "redis_client", None)
    monkeypatch.setattr(ws_module, "_subscriber_task", None, raising=False)

    async def _forever() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(ws_module, "redis_subscriber", _forever)

    async def _main() -> bool:
        ws_module.setup_redis()
        task = ws_module._subscriber_task
        client = ws_module.redis_client
        assert task is not None and client is not None
        alive = not task.done()
        task.cancel()
        await client.aclose()
        return alive

    assert asyncio.run(_main()) is True


def test_handshakes_per_ip_are_counted_across_processes_in_redis(local_model_redis, monkeypatch):
    from app.services import ws_admission

    monkeypatch.setattr(settings, "WS_HANDSHAKES_PER_IP_PER_MINUTE", 3)
    kwargs = local_model_redis.connection_pool.connection_kwargs
    ip = f"203.0.113.{uuid.uuid4().int % 250 + 1}"

    async def _main() -> tuple[list[bool], bool, int]:
        # Two clients stand in for two API processes sharing one Redis.
        a = aioredis.Redis(host=kwargs["host"], port=kwargs["port"])
        b = aioredis.Redis(host=kwargs["host"], port=kwargs["port"])
        try:
            verdicts = [await ws_admission.handshake_allowed(client, ip) for client in (a, b, a, b)]
            other_ip = await ws_admission.handshake_allowed(a, "198.51.100.7")
            keys = [k async for k in a.scan_iter(match=f"ws_handshake:{ip}:*")]
            ttl = await a.ttl(keys[0])
            return verdicts, other_ip, ttl
        finally:
            await a.aclose()
            await b.aclose()

    verdicts, other_ip, ttl = asyncio.run(_main())

    assert verdicts == [True, True, True, False]
    assert other_ip is True
    assert 0 < ttl <= 120, "the counter must expire"


def test_the_rate_limit_fails_open_when_redis_is_unreachable(monkeypatch):
    from app.services import ws_admission

    monkeypatch.setattr(settings, "WS_HANDSHAKES_PER_IP_PER_MINUTE", 1)

    async def _main() -> bool:
        dead = aioredis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.5)
        try:
            return await ws_admission.handshake_allowed(dead, "192.0.2.1")
        finally:
            await dead.aclose()

    assert asyncio.run(_main()) is True


@pytest.fixture
def client(db_session, monkeypatch):
    isolated_revocation(monkeypatch)
    with ws_test_client(db_session, monkeypatch) as test_client:
        yield test_client


def _refused_before_accept(client: TestClient, headers: dict[str, str] | None = None) -> int:
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/ws", headers=headers or {}),
    ):
        pass
    return refused.value.code


def test_a_handshake_over_the_ip_rate_limit_is_refused_before_accept(client, monkeypatch):
    from app.services import ws_admission

    async def _spent(*_args: Any) -> bool:
        return False

    monkeypatch.setattr(ws_admission, "handshake_allowed", _spent)
    assert _refused_before_accept(client) == TRY_AGAIN_LATER


def test_a_full_process_refuses_the_handshake(client, monkeypatch):
    monkeypatch.setattr(settings, "WS_MAX_SOCKETS_PER_PROCESS", 1)
    ws_module.manager.connect(_Peer().socket, 999_999)
    assert _refused_before_accept(client) == TRY_AGAIN_LATER


def test_a_user_at_the_socket_cap_cannot_open_another(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "WS_MAX_SOCKETS_PER_USER", 2)
    owner = make_user(db_session)
    headers = cookie_header(access_token(owner))

    with client.websocket_connect("/ws", headers=headers) as first:
        assert json.loads(first.receive_text())["type"] == "connection_established"
        with client.websocket_connect("/ws", headers=headers) as second:
            assert json.loads(second.receive_text())["type"] == "connection_established"
            with client.websocket_connect("/ws", headers=headers) as third:
                with pytest.raises(WebSocketDisconnect) as refused:
                    third.receive_text()
    assert refused.value.code == TRY_AGAIN_LATER
