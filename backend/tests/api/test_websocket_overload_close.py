"""A database overload at the handshake closes 1013 (Try Again Later), not 4003 (scale plan 6).

RFC 6455 section 7.4.1 / IANA close code 1013: the client should back off and retry. 4003 is
this API's "invalid token" refusal, which the SPA treats as a credential problem.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.websockets import WebSocketDisconnect

from app.services import ws_admission
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
def exhausted_pool(monkeypatch):
    """Every session checkout times out, as with a saturated pool."""
    from app.api import websockets as ws_module

    def _raise(*_a, **_k):
        raise PoolTimeoutError("QueuePool limit of size 5 overflow 10 reached", None, None)

    monkeypatch.setattr(ws_module, "session_scope", _raise)


def _close_code(exc: pytest.ExceptionInfo[WebSocketDisconnect]) -> int:
    return exc.value.code


def test_cookie_handshake_with_exhausted_pool_closes_1013(client, db_session, exhausted_pool):
    owner = make_user(db_session)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws", headers=cookie_header(access_token(owner))) as ws:
            ws.receive_text()
    assert _close_code(exc) == ws_admission.TRY_AGAIN_LATER_CLOSE == 1013


def test_first_message_handshake_with_exhausted_pool_closes_1013(
    client, db_session, exhausted_pool
):
    owner = make_user(db_session)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(authenticate_frame(access_token(owner)))
            ws.receive_text()
    assert _close_code(exc) == 1013
