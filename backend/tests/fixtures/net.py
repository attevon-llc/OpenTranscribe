"""Loopback socket helpers shared by the test fixtures and by tests that stand up a real server."""

from __future__ import annotations

import socket


def free_port() -> int:
    """A real TCP port on localhost, bound then released (so nothing is listening on it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def tcp_reachable(host: str, port: int, timeout: float = 0.3) -> bool:
    """True when something accepts a TCP connection on ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0
