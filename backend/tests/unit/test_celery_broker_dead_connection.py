"""Workers must survive a SILENT broker connection drop (issue #1144).

When a worker<->Redis TCP flow vanishes without a FIN/RST reaching the worker (NAT or
conntrack entry expiring, firewall/network-policy reload, idle reset), the worker used to
stop consuming forever while the process stayed alive:

* kombu passes ``socket_keepalive=None`` to redis-py, which turns OFF redis-py 8's
  keepalive default — a BRPOP parked in epoll on a dead socket is never woken;
* when anything finally raised a connection error, ``Channel.close()`` drained the
  outstanding BRPOP with a blocking ``recv()`` and ``socket_timeout=None`` — the main
  event loop wedged inside it, permanently.

The unit tests pin the settings; ``test_close_does_not_wedge_on_a_silent_peer`` reproduces
the wedge itself against an in-process peer that accepts the connection and then never
answers a BRPOP.
"""

from __future__ import annotations

import socket
import threading

import pytest


def _opts() -> dict:
    from app.core.celery import celery_app

    return dict(celery_app.conf.broker_transport_options)


# ── broker transport options ─────────────────────────────────────────────────────


def test_broker_enables_tcp_keepalive():
    opts = _opts()
    assert opts.get("socket_keepalive") is True, (
        "kombu passes socket_keepalive=None unless set, which DISABLES redis-py's default "
        "keepalive — a BRPOP on a silently dropped socket then waits forever"
    )


@pytest.mark.skipif(not hasattr(socket, "TCP_KEEPIDLE"), reason="Linux keepalive tunables")
def test_keepalive_detects_a_dead_socket_within_two_minutes():
    """Kernel defaults (7200 s idle) would leave a deaf worker for over two hours."""
    ka = _opts().get("socket_keepalive_options") or {}
    idle = ka.get(socket.TCP_KEEPIDLE)
    intvl = ka.get(socket.TCP_KEEPINTVL)
    cnt = ka.get(socket.TCP_KEEPCNT)
    assert idle and intvl and cnt, f"keepalive tunables missing: {ka}"
    assert idle + intvl * cnt <= 120


@pytest.mark.skipif(not hasattr(socket, "TCP_USER_TIMEOUT"), reason="Linux only")
def test_user_timeout_bounds_unacknowledged_writes():
    """Keepalive is suspended while data is in flight; TCP_USER_TIMEOUT covers that case."""
    ka = _opts().get("socket_keepalive_options") or {}
    user_timeout_ms = ka.get(socket.TCP_USER_TIMEOUT)
    assert user_timeout_ms and 0 < user_timeout_ms <= 120_000


def test_broker_socket_timeout_is_finite():
    timeout = _opts().get("socket_timeout")
    assert timeout is not None and timeout > 0, (
        "Channel.close() drains an outstanding BRPOP with a blocking read; without a "
        "socket timeout that read never returns on a half-open socket"
    )


def test_broker_socket_timeout_clears_the_brpop_poll_timeout():
    """A socket_timeout at or under kombu's BRPOP wait would reconnect-loop an idle worker.

    kombu's redis transport issues ``BRPOP ... <brpop_timeout>`` (1 s, or
    ``polling_interval`` when that option is set) and the drain in ``Channel.close()``
    may legitimately wait that long for the server's reply.
    """
    from kombu.transport.redis import Transport

    opts = _opts()
    brpop_wait = opts.get("polling_interval") or Transport.brpop_timeout
    assert opts["socket_timeout"] >= 5 * brpop_wait


def test_broker_connect_timeout_is_finite():
    timeout = _opts().get("socket_connect_timeout")
    assert timeout is not None and 0 < timeout <= 60


def test_broker_health_check_interval_is_set():
    opts = _opts()
    assert opts.get("health_check_interval") and opts["health_check_interval"] > 0


def test_broker_does_not_retry_inside_redis_py():
    """retry_on_timeout=True re-wedges the consumer on its pubsub socket.

    It gives redis-py connections Retry(NoBackoff(), 1). When the fanout socket dies,
    that retry reconnects and re-runs PubSub.parse_response(block=True), which reads with
    timeout=None — the event loop blocks again on a healthy socket instead of letting
    kombu see the ConnectionError and rebuild the consumer.
    """
    assert _opts().get("retry_on_timeout") is False


def test_priority_and_visibility_settings_are_unchanged():
    opts = _opts()
    assert opts["priority_steps"] == list(range(10))
    assert opts["queue_order_strategy"] == "priority"
    assert "visibility_timeout" in opts


# ── result backend ───────────────────────────────────────────────────────────────


def test_result_backend_detects_dead_connections():
    from app.core.celery import celery_app

    conf = celery_app.conf
    assert conf.redis_socket_keepalive is True
    assert conf.redis_socket_timeout and conf.redis_socket_timeout > 0
    assert conf.redis_socket_connect_timeout and conf.redis_socket_connect_timeout > 0
    assert conf.redis_retry_on_timeout is True
    assert conf.redis_backend_health_check_interval and conf.redis_backend_health_check_interval > 0


# ── reconnect policy ─────────────────────────────────────────────────────────────


def test_broker_reconnects_forever():
    """The default of 100 retries makes a worker give up during a long broker outage."""
    from app.core.celery import celery_app

    conf = celery_app.conf
    assert conf.broker_connection_retry is True
    assert conf.broker_connection_retry_on_startup is True
    assert conf.broker_connection_max_retries is None


# ── the wedge itself ─────────────────────────────────────────────────────────────


class _SilentRedis:
    """Speaks just enough RESP to complete redis-py's handshake, then never answers BRPOP.

    From the client's side this is indistinguishable from a peer whose connection was
    dropped silently: the socket stays ESTABLISHED and no byte, FIN or RST ever arrives.
    """

    def __init__(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _read_command(f) -> list[bytes] | None:
        header = f.readline()
        if not header:
            return None
        args = []
        for _ in range(int(header[1:])):
            length = int(f.readline()[1:])
            args.append(f.read(length + 2)[:-2])
        return args

    def _serve(self, conn: socket.socket) -> None:
        f = conn.makefile("rb")
        while True:
            try:
                cmd = self._read_command(f)
            except (OSError, ValueError):
                return
            if cmd is None:
                return
            name = cmd[0].upper()
            if name == b"BRPOP":
                continue  # the dropped flow: never answer
            if name == b"HELLO":
                reply = b"%1\r\n$5\r\nproto\r\n:3\r\n"
            elif name == b"PING":
                reply = b"+PONG\r\n"
            else:
                reply = b"+OK\r\n"
            try:
                conn.sendall(reply)
            except OSError:
                return

    def close(self) -> None:
        self._srv.close()
        for conn in self._conns:
            conn.close()


def test_close_does_not_wedge_on_a_silent_peer():
    """Reproduces the consumer wedge: closing a channel with a BRPOP in flight.

    This is exactly the path the worker takes on a connection error
    (``on_connection_error_after_connected`` -> ``collect`` -> ``Channel.close`` ->
    ``_brpop_read``). With no socket timeout it blocks forever.
    """
    from kombu import Connection

    opts = _opts()
    # Shrink a CONFIGURED timeout so the test is fast; an absent one stays absent, which
    # is precisely the hang being tested for.
    if opts.get("socket_timeout"):
        opts["socket_timeout"] = 1.0

    peer = _SilentRedis()
    conn = Connection(f"redis://127.0.0.1:{peer.port}/0", transport_options=opts)
    try:
        channel = conn.default_channel
        client = channel.client
        client.connection = client.connection_pool.get_connection()
        if opts.get("socket_keepalive"):
            sock = client.connection._sock
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1, (
                "kombu did not forward socket_keepalive to redis-py"
            )
        client.connection.send_command("BRPOP", "utility", 1)
        channel._in_poll = client.connection

        finished = threading.Event()

        def _close() -> None:
            try:
                channel.close()
            finally:
                finished.set()

        threading.Thread(target=_close, daemon=True).start()
        assert finished.wait(15), (
            "Channel.close() is blocked draining a BRPOP the peer will never answer — "
            "the worker's event loop would be wedged here forever"
        )
    finally:
        peer.close()
