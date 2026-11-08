"""Rate limits on authenticated routes are per user, not per client IP.

``@limiter.limit(...)`` with no ``key_func`` keys the bucket on the client IP. Behind a NAT,
a corporate proxy, or a reverse proxy not listed in ``RATE_LIMIT_TRUSTED_PROXIES``, every
user shares one IP, so 20 chat messages a minute was 20 for the whole office (concurrency
audit F12). An authenticated route keys on the user (``user_or_ip_key``, which falls back to
the IP only when no user was resolved); login and the other pre-authentication routes stay
per IP, because there is no user to key on and the IP is what a credential-stuffing limit
must follow.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from fastapi import status

_APP = Path(__file__).resolve().parents[2] / "app"

#: Dependencies that resolve an authenticated user (and set ``request.state.user_id``).
_AUTHENTICATED_DEPENDENCIES = frozenset(
    {
        "get_current_user",
        "get_current_active_user",
        "get_current_active_superuser",
        "get_current_admin_user",
        "get_current_super_admin_user",
        "get_current_context",
        "get_base_context",
    }
)


#: One fixed conversation for the two-user test, so it holds whichever way the chat
#: limit is scoped.
_CONVERSATION = uuid.uuid4()


def _send(client, headers):
    return client.post(
        f"/api/chat/conversations/{_CONVERSATION}/messages",
        json={"content": "hello"},
        headers=headers,
    )


def test_two_users_behind_one_ip_get_separate_chat_buckets(
    client, user_token_headers, admin_token_headers, rate_limiting_enabled
):
    """One user exhausting the chat limit leaves another user on the same IP untouched."""
    for _ in range(20):
        response = _send(client, user_token_headers)
        # The conversation does not exist, so each request is a 404 that still spends
        # the bucket: the limit is checked before the handler runs.
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text

    assert _send(client, user_token_headers).status_code == status.HTTP_429_TOO_MANY_REQUESTS
    other = _send(client, admin_token_headers)
    assert other.status_code == status.HTTP_404_NOT_FOUND, other.text


def test_the_chat_limit_counts_one_user_across_conversations(
    client, user_token_headers, rate_limiting_enabled
):
    """Opening another conversation does not buy another 20 messages a minute.

    The limiter's default ``key_style`` is "url", so a plain ``@limiter.limit`` keeps
    a bucket per request path, i.e. per conversation. The chat turn routes share one
    named scope instead.
    """
    first, second = uuid.uuid4(), uuid.uuid4()
    for index in range(20):
        conversation = first if index % 2 == 0 else second
        response = client.post(
            f"/api/chat/conversations/{conversation}/messages",
            json={"content": "hello"},
            headers=user_token_headers,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text

    third = client.post(
        f"/api/chat/conversations/{uuid.uuid4()}/messages",
        json={"content": "hello"},
        headers=user_token_headers,
    )
    assert third.status_code == status.HTTP_429_TOO_MANY_REQUESTS, third.text


def _limited_routes() -> list[tuple[str, str, bool, bool]]:
    """``(location, handler, keyed_per_user, authenticated)`` for every limited route."""
    found = []
    for path in sorted(_APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            limits = [
                d
                for d in node.decorator_list
                if isinstance(d, ast.Call)
                and ast.unparse(d.func) in ("limiter.limit", "limiter.shared_limit")
            ]
            if not limits:
                continue
            per_user = all(
                any(
                    k.arg == "key_func" and ast.unparse(k.value) == "user_or_ip_key"
                    for k in d.keywords
                )
                for d in limits
            )
            defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]
            authenticated = any(
                isinstance(d, ast.Call)
                and ast.unparse(d.func) == "Depends"
                and d.args
                and ast.unparse(d.args[0]) in _AUTHENTICATED_DEPENDENCIES
                for d in defaults
            )
            found.append(
                (f"{path.relative_to(_APP)}:{node.lineno}", node.name, per_user, authenticated)
            )
    return found


def test_every_authenticated_limited_route_is_keyed_per_user() -> None:
    routes = _limited_routes()
    assert len(routes) > 30, f"the scan found only {len(routes)} limited routes"
    per_ip = [f"{where} {name}" for where, name, per_user, auth in routes if auth and not per_user]
    assert not per_ip, (
        "these authenticated routes rate-limit by client IP, so users behind one NAT or "
        f"proxy share a bucket; pass key_func=user_or_ip_key: {per_ip}"
    )


@pytest.mark.parametrize(
    ("handler", "expected_authenticated"),
    [("send_message", True), ("login_for_access_token", False), ("refresh_access_token", False)],
)
def test_the_scan_tells_authenticated_routes_from_pre_auth_ones(
    handler: str, expected_authenticated: bool
) -> None:
    """The guard is only as good as its classification: pin one of each kind."""
    by_name = {name: auth for _, name, _, auth in _limited_routes()}
    assert by_name[handler] is expected_authenticated
