"""Rate-limit coverage for ``GET /users/search`` (issue #904).

The sharing/group-member-add autocomplete carried no rate limit at all: an
authenticated user could page the whole tenant directory as fast as the connection
allowed. `RATE_LIMIT_DIRECTORY_PER_MINUTE` closes the volume gap; the query minimum
length and tenant gate (see `test_users.py`'s SEARCH_PATH suite) are what limit what
any single request can see.

Same technique as `test_llm_settings_rate_limit.py`: flip the module-level `limiter`
on for one test, reset its storage on the way out.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.auth.rate_limit import limiter
from app.core.config import settings

SEARCH_PATH = "/api/users/search"


@pytest.fixture
def rate_limiting_enabled():
    """Turn the real slowapi limiter on for one test, then clean up after it.

    See `test_llm_settings_rate_limit.py`'s fixture of the same name for why the
    `.reset()` call is wrapped: host runs routinely have no Redis reachable.
    """
    was_enabled = limiter.enabled
    limiter.enabled = True
    try:
        yield
    finally:
        limiter.enabled = was_enabled
        try:
            limiter.reset()
        except Exception:
            pass


def _search(client, headers, q="ab"):
    return client.get(SEARCH_PATH, params={"q": q}, headers=headers)


def test_user_search_is_rate_limited_per_user(
    client, user_token_headers, other_user, rate_limiting_enabled
):
    """The Nth+1 request within the window is refused with 429.

    Reads `settings.RATE_LIMIT_DIRECTORY_PER_MINUTE` rather than hardcoding a number
    so the test passes under both the coded default and any dev-overlay override.
    """
    limit = settings.RATE_LIMIT_DIRECTORY_PER_MINUTE
    for _ in range(limit):
        resp = _search(client, user_token_headers, q=other_user.email[:6])
        assert resp.status_code == status.HTTP_200_OK, resp.text

    blocked = _search(client, user_token_headers, q=other_user.email[:6])
    assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS, blocked.text


def test_the_directory_bucket_is_keyed_per_user_not_by_ip(
    client, user_token_headers, admin_token_headers, other_user, rate_limiting_enabled
):
    """A second authenticated user is not punished by the first user's usage.

    Both requests originate from the same TestClient (same source IP), so this only
    passes if the bucket key is the resolved user id (`user_or_ip_key`) rather than
    the shared IP. Dropping `key_func=user_or_ip_key` from the decorator is the one
    change that fails this test and no other in this file.
    """
    limit = settings.RATE_LIMIT_DIRECTORY_PER_MINUTE
    for _ in range(limit):
        resp = _search(client, user_token_headers, q=other_user.email[:6])
        assert resp.status_code == status.HTTP_200_OK, resp.text

    # user_token_headers' bucket is now exhausted...
    assert (
        _search(client, user_token_headers, q=other_user.email[:6]).status_code
        == status.HTTP_429_TOO_MANY_REQUESTS
    )

    # ...but a different authenticated user still gets served.
    other_resp = _search(client, admin_token_headers, q=other_user.email[:6])
    assert other_resp.status_code == status.HTTP_200_OK, other_resp.text
