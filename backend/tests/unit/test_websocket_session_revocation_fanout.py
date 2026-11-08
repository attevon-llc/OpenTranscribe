"""The session-revocation fan-out, at its two boundaries.

Publish side: stamping a user's revocation epoch publishes one ``session_revoked``
message on the channel every API process subscribes to, carrying the same epoch it
recorded. Subscribe side: the dispatcher closes that user's sockets authenticated by the
epoch, and nothing else. Neither side needs Redis or a database.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from typing import cast

import pytest
from fastapi import WebSocket

from app.api import websockets as ws_module
from app.auth.session import InMemoryStore
from app.auth.token_service import USER_REVOCATION_EPOCH_PREFIX
from app.auth.token_service import TokenService
from app.utils import websocket_notify

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000f1"
OTHER_UUID = "019ec90a-1b2c-7def-8000-0000000000f2"


class _RecordingRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, dict]] = []
        self._fail = fail

    def publish(self, channel: str, payload: str) -> int:
        if self._fail:
            raise ConnectionError("redis down")
        self.published.append((channel, json.loads(payload)))
        return 1


class _RaisingStore:
    def set(self, *_a: Any, **_k: Any) -> None:
        raise ConnectionError("redis down")


def _service(store: Any) -> TokenService:
    svc = TokenService()
    svc._store = store
    return svc


class TestThePublishSide:
    def test_stamping_the_epoch_publishes_it_for_every_process(self, monkeypatch):
        redis = _RecordingRedis()
        monkeypatch.setattr(websocket_notify, "get_request_redis", lambda: redis)
        store = InMemoryStore()

        assert _service(store).stamp_user_revocation_epoch(USER_UUID) is True

        stored_epoch = int(store.get(f"{USER_REVOCATION_EPOCH_PREFIX}{USER_UUID}") or "0")
        assert redis.published == [
            (
                "websocket_notifications",
                {
                    "type": "session_revoked",
                    "user_uuid": USER_UUID,
                    "data": {"epoch": stored_epoch},
                },
            )
        ]

    def test_sockets_are_still_closed_when_the_epoch_write_fails(self, monkeypatch):
        redis = _RecordingRedis()
        monkeypatch.setattr(websocket_notify, "get_request_redis", lambda: redis)

        assert _service(_RaisingStore()).stamp_user_revocation_epoch(USER_UUID) is False

        assert [message["type"] for _, message in redis.published] == ["session_revoked"]

    def test_a_failed_publish_leaves_the_recorded_epoch_standing(self, monkeypatch):
        """Fail-secure: the fan-out is best-effort; the epoch is what refuses the token."""
        monkeypatch.setattr(
            websocket_notify, "get_request_redis", lambda: _RecordingRedis(fail=True)
        )
        store = InMemoryStore()

        assert _service(store).stamp_user_revocation_epoch(USER_UUID) is True

        assert store.get(f"{USER_REVOCATION_EPOCH_PREFIX}{USER_UUID}") is not None

    def test_no_user_means_no_message(self, monkeypatch):
        redis = _RecordingRedis()
        monkeypatch.setattr(websocket_notify, "get_request_redis", lambda: redis)

        assert _service(InMemoryStore()).stamp_user_revocation_epoch(None) is False

        assert redis.published == []


class _Socket:
    """A socket double that records what was done to it (closes, frames sent)."""

    def __init__(self) -> None:
        self.closed_with: tuple[int, str] | None = None
        self.sent: list[str] = []

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


@pytest.fixture
def mgr(monkeypatch) -> ws_module.ConnectionManager:
    manager = ws_module.ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", manager)
    return manager


def _connect(
    manager: ws_module.ConnectionManager,
    user_id: int,
    uuid: str,
    at: int,
    *,
    issued_at: int | None = None,
    jti: str | None = None,
) -> _Socket:
    sock = _Socket()
    manager.connect(cast(WebSocket, sock), user_id, user_uuid=uuid)
    manager.socket_owners[cast(WebSocket, sock)] = ws_module.SocketAuth(
        user_uuid=uuid, authenticated_at=at, jti=jti, issued_at=issued_at
    )
    return sock


async def _dispatch(user_uuid: Any, epoch: Any) -> None:
    payload = {"type": "session_revoked", "user_uuid": user_uuid, "data": {"epoch": epoch}}
    await ws_module._dispatch_notification({"type": "message", "data": json.dumps(payload)})


REVOKED = (ws_module.SESSION_REVOKED_CLOSE, "Session revoked")


class TestTheSubscribeSide:
    @pytest.mark.asyncio
    async def test_the_users_older_sockets_are_closed_as_revoked(self, mgr):
        first, second = _connect(mgr, 1, USER_UUID, 100), _connect(mgr, 1, USER_UUID, 200)

        await _dispatch(USER_UUID, 200)

        assert (first.closed_with, second.closed_with) == (REVOKED, REVOKED)
        assert first.sent == second.sent == []

    @pytest.mark.asyncio
    async def test_another_users_socket_is_untouched(self, mgr):
        other = _connect(mgr, 2, OTHER_UUID, 100)

        await _dispatch(USER_UUID, 200)

        assert other.closed_with is None
        assert other.sent == []

    @pytest.mark.asyncio
    async def test_a_socket_authenticated_after_the_epoch_stays_open(self, mgr):
        """Its handshake already compared its token against this epoch."""
        newer = _connect(mgr, 1, USER_UUID, 201)

        closed = await mgr.close_revoked(USER_UUID, 200)

        assert closed == 0
        assert newer.closed_with is None

    @pytest.mark.asyncio
    async def test_a_malformed_message_closes_nothing(self, mgr):
        sock = _connect(mgr, 1, USER_UUID, 100)

        await _dispatch(USER_UUID, "not-an-int")

        assert sock.closed_with is None
        assert sock.sent == []

    def test_disconnect_drops_the_owner_record(self, mgr):
        sock = _connect(mgr, 1, USER_UUID, 100)

        mgr.disconnect(cast(WebSocket, sock), 1)

        assert cast(WebSocket, sock) not in mgr.socket_owners


class TestTheTokenIsWhatCounts:
    """The close follows the socket's token, not this host's clock (section: skew)."""

    @pytest.mark.asyncio
    async def test_a_token_older_than_the_epoch_is_closed_whatever_this_hosts_clock_said(self, mgr):
        """This host accepted the socket at 300 by its own clock, ahead of the stamping
        host's epoch of 200; the token was minted at 95, so HTTP already refuses it."""
        sock = _connect(mgr, 1, USER_UUID, 300, issued_at=95)

        assert await mgr.close_revoked(USER_UUID, 200) == 1
        assert sock.closed_with == REVOKED

    @pytest.mark.asyncio
    async def test_a_token_newer_than_the_epoch_stays_open_whatever_this_hosts_clock_said(
        self, mgr
    ):
        sock = _connect(mgr, 1, USER_UUID, 100, issued_at=250)

        assert await mgr.close_revoked(USER_UUID, 200) == 0
        assert sock.closed_with is None

    def test_connect_records_the_tokens_claims(self, mgr):
        sock = _Socket()
        claims = {"jti": "j-1", "iat": 1000, "exp": 4600}

        mgr.connect(cast(WebSocket, sock), 1, user_uuid=USER_UUID, claims=claims)

        auth = mgr.socket_owners[cast(WebSocket, sock)]
        assert (auth.jti, auth.issued_at, auth.expires_at) == ("j-1", 1000, 4600)


async def _dispatch_jti(user_uuid: Any, jti: Any) -> None:
    payload = {"type": "session_revoked", "user_uuid": user_uuid, "data": {"jti": jti}}
    await ws_module._dispatch_notification({"type": "message", "data": json.dumps(payload)})


class TestSingleSessionLogout:
    @pytest.mark.asyncio
    async def test_only_the_logged_out_tokens_socket_is_closed(self, mgr):
        logged_out = _connect(mgr, 1, USER_UUID, 100, issued_at=100, jti="token-a")
        other_tab = _connect(mgr, 1, USER_UUID, 100, issued_at=100, jti="token-b")
        other_user = _connect(mgr, 2, OTHER_UUID, 100, issued_at=100, jti="token-a")

        await _dispatch_jti(USER_UUID, "token-a")

        assert logged_out.closed_with == REVOKED
        assert other_tab.closed_with is None
        assert other_user.closed_with is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("jti", ["", None, 5])
    async def test_a_malformed_jti_closes_nothing(self, mgr, jti):
        sock = _connect(mgr, 1, USER_UUID, 100, jti="token-a")

        await _dispatch_jti(USER_UUID, jti)

        assert sock.closed_with is None

    def test_the_publish_names_the_user_and_the_token(self, monkeypatch):
        redis = _RecordingRedis()
        monkeypatch.setattr(websocket_notify, "get_request_redis", lambda: redis)

        assert websocket_notify.publish_token_revocation(USER_UUID, "token-a") is True

        assert redis.published == [
            (
                "websocket_notifications",
                {"type": "session_revoked", "user_uuid": USER_UUID, "data": {"jti": "token-a"}},
            )
        ]


class _SilentSocket(_Socket):
    """A peer that never sends a frame."""

    async def receive_text(self) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _ChattySocket(_Socket):
    async def receive_text(self) -> str:
        return "ping"


class TestTheTokenBoundsTheSocket:
    @pytest.mark.asyncio
    async def test_a_socket_is_closed_when_its_token_expires(self):
        sock = _SilentSocket()

        frame = await ws_module._receive_until(cast(WebSocket, sock), int(time.time()) + 1)

        assert frame is None
        assert sock.closed_with == (ws_module.SESSION_REVOKED_CLOSE, "Session expired")

    @pytest.mark.asyncio
    async def test_an_already_expired_token_closes_at_once(self):
        sock = _ChattySocket()

        frame = await ws_module._receive_until(cast(WebSocket, sock), int(time.time()) - 1)

        assert frame is None
        assert sock.closed_with == (ws_module.SESSION_REVOKED_CLOSE, "Session expired")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("offset", [None, 600])
    async def test_frames_flow_while_the_token_is_valid(self, offset):
        sock = _ChattySocket()
        expires_at = None if offset is None else int(time.time()) + offset

        assert await ws_module._receive_until(cast(WebSocket, sock), expires_at) == "ping"
        assert sock.closed_with is None
