"""The ``/api/ws`` handshake must refuse a cross-origin ``Origin`` (issue #903).

The WebSocket handshake is **not** subject to the same-origin policy and browsers
attach cookies to it, while this app's CORS and CSRF middleware are both
``BaseHTTPMiddleware``-based and do not run on the WebSocket path. So before this
gate, any page on any origin could open a socket in a visiting authenticated
user's browser, the ``access_token`` cookie went along, ``_try_authenticate_token``
succeeded, and the attacker's page received that user's live event stream —
transcription progress, file events, notifications — for as long as it held the
socket open. Zero prior access required.

These drive the REAL route through Starlette's ASGI ``TestClient``. Two notes
that cost time when this was written:

* **The path is ``/api/ws``, not ``/ws``.** ``websockets.router`` declares
  ``/ws`` and ``main.py`` mounts ``api_router`` under ``settings.API_PREFIX``.
  A websocket to an unmatched path is closed by Starlette's router with a clean
  **1000**, which looks exactly like the app deciding to hang up — that is the
  "unexplained clean disconnect" ``test_handler_and_ws_hardening.py`` records.
* **Refusal happens BEFORE ``accept()``**, so ``websocket_connect(...)`` raises on
  ENTRY. A test that got as far as the ``with`` body would be proving the
  opposite of what it claims: an accepted socket is a server-side connection the
  attacker's page already holds.

⚠️ ``WebSocketRoute`` has no ``.methods`` attribute, so a route-table test that
filters on it selects nothing and passes vacuously. These are functional.
"""

from __future__ import annotations

import json

import pytest
from starlette.websockets import WebSocketDisconnect

# Reaching this close code means the handshake got past the Origin gate and into
# the authentication step — i.e. the gate allowed the connection.
_PAST_THE_GATE = (4003, "Invalid token")


def _connect_and_read_first_frame(client, **kwargs) -> dict[str, object]:
    """Open ``/api/ws``, offer a bogus token, and return the first frame back.

    Only reachable when the handshake was accepted; a refused handshake raises out
    of ``websocket_connect`` before this can run.
    """
    with client.websocket_connect("/api/ws", **kwargs) as websocket:
        websocket.send_text(json.dumps({"type": "authenticate", "token": "bogus"}))
        # TestClient.receive() is untyped; naming the ASGI message shape it returns
        # keeps every use below checked instead of widening to Any.
        frame: dict[str, object] = websocket.receive()
        return frame


def test_a_cross_origin_handshake_is_refused_before_accept(client):
    """The control this issue is about: a foreign origin never gets a socket.

    Watched red first — the pre-fix handler accepted the handshake from
    ``https://evil.example`` and went straight on to authenticate it.
    """
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/ws", headers={"Origin": "https://evil.example"}):
            pytest.fail("the handshake was accepted from a foreign origin")

    assert excinfo.value.code == 4403, excinfo.value.reason


def test_an_origin_that_merely_prefixes_an_allowed_one_is_refused(client):
    """``https://localhost:5173.evil.example`` is not ``http://localhost:5173``.

    Guards against a substring/``startswith`` comparison — the classic way an
    origin allowlist ends up allowing an attacker-controlled subdomain.
    """
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/ws", headers={"Origin": "http://localhost:5173.evil.example"}
        ):
            pytest.fail("a look-alike origin was accepted")

    assert excinfo.value.code == 4403, excinfo.value.reason


def test_an_allowlisted_origin_still_connects(client):
    """Control: an origin in ``settings.CORS_ORIGINS`` reaches authentication.

    Without this, the two tests above would also pass if the gate refused
    everything.
    """
    frame = _connect_and_read_first_frame(client, headers={"Origin": "http://localhost:5173"})

    assert (frame["code"], frame["reason"]) == _PAST_THE_GATE, frame


def test_a_same_origin_handshake_still_connects(client):
    """Control, and the reason the gate is not keyed on ``CORS_ORIGINS`` alone.

    ``CORS_ORIGINS`` is set by no shipped compose file — it defaults to the two
    Vite dev URLs — because a same-origin SPA behind nginx never needs CORS. A
    gate that consulted only the allowlist would therefore refuse the WebSocket in
    every real deployment. ``testserver`` is the TestClient's own host, and is
    deliberately NOT in ``CORS_ORIGINS``.
    """
    from app.core.config import settings

    assert "http://testserver" not in settings.CORS_ORIGINS, (
        "this test proves the same-origin branch; it is meaningless if the host is also allowlisted"
    )

    frame = _connect_and_read_first_frame(client, headers={"Origin": "http://testserver"})

    assert (frame["code"], frame["reason"]) == _PAST_THE_GATE, frame


def test_a_handshake_with_no_origin_header_still_connects(client):
    """Control: non-browser clients are unaffected.

    curl, a cron script and an agent send no ``Origin`` — and carry no ambient
    cookie for another site to abuse, which is the whole threat here. This app's
    non-UI API is a deliberate feature, so refusing them would be a regression,
    not extra safety.
    """
    frame = _connect_and_read_first_frame(client)

    assert (frame["code"], frame["reason"]) == _PAST_THE_GATE, frame
