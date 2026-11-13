"""Every recorded client address goes through ``app.utils.client_ip``.

Audit rows for admin settings, chat projects, takedowns, quarantines, auth-config changes and
verification resends read ``request.client.host`` directly. Behind the bundled nginx that is the
proxy, so each of those records named nginx instead of the person, while the rate limiter, the
login audit and sessions named the person: two answers to "who sent this?". The only module
allowed to read the raw peer besides the resolver is ``auth/header_trust.py``, whose rule 1 is
that the immediate peer, not the forwarded chain, decides whether an identity header counts.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from starlette.requests import Request

from app.utils import client_ip

APP = Path(__file__).resolve().parents[2] / "app"

#: Modules that may read the socket peer, with the reason.
RAW_PEER_READERS = {
    "utils/client_ip.py": "the resolver itself",
    "auth/header_trust.py": "identity headers are judged by the immediate peer, by design",
}

NGINX = "172.18.0.5"
VISITOR = "203.0.113.9"


def _raw_peer_reads(source: str) -> list[int]:
    tree = ast.parse(source)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "host"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "client"
    )


def test_only_the_resolver_and_header_trust_read_the_raw_peer():
    offenders = [
        f"{path.relative_to(APP)}:{line}"
        for path in sorted(APP.rglob("*.py"))
        if path.relative_to(APP).as_posix() not in RAW_PEER_READERS
        for line in _raw_peer_reads(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_the_scan_sees_a_raw_peer_read():
    source = "ip = request.client.host if request.client else None\npeer = websocket.client.host\n"

    assert _raw_peer_reads(source) == [1, 2]
    assert _raw_peer_reads("ip = resolve_client_ip(request)\n") == []


def _behind_nginx(forwarded: str | None = VISITOR) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "client": (NGINX, 40000), "headers": headers})


@pytest.fixture
def nginx_trusted(monkeypatch):
    monkeypatch.setattr(client_ip, "_TRUSTED_NETWORKS", client_ip._parse_networks("172.16.0.0/12"))


def test_known_client_ip_is_the_resolved_client(nginx_trusted):
    assert client_ip.known_client_ip(_behind_nginx()) == VISITOR


def test_known_client_ip_is_none_when_nothing_is_known():
    assert client_ip.known_client_ip(None) is None
    assert client_ip.known_client_ip(Request({"type": "http", "headers": []})) is None


@pytest.mark.parametrize(
    ("module", "helper"),
    [
        ("app.api.endpoints.admin_quarantine", "_request_meta"),
        ("app.api.endpoints.org_admin_takedown", "_request_meta"),
        ("app.api.endpoints.admin_takedown_notices", "_meta"),
    ],
)
def test_takedown_audit_records_the_visitor_not_nginx(nginx_trusted, module, helper):
    import importlib

    meta = getattr(importlib.import_module(module), helper)

    assert meta(_behind_nginx())[0] == VISITOR
