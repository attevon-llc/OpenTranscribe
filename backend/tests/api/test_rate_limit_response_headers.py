"""A rate-limited handler that returns a Pydantic model answers 200 with the limit headers.

The limiter runs with ``headers_enabled=True``, so slowapi writes ``X-RateLimit-*`` into the
handler's ``response`` parameter. The handler must declare it: without it, slowapi falls back
to the return value and raises for any non-Response, so every call is a 500. The static half
of this rule is ``tests/unit/test_rate_limited_endpoints_declare_response.py``; this test
drives the real middleware stack so the header injection itself is pinned.
"""

from __future__ import annotations

from fastapi import status


def test_rate_limited_pydantic_return_gets_rate_limit_headers(
    client, user_token_headers, rate_limiting_enabled
):
    response = client.get("/api/users/search", params={"q": "ab"}, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert "X-RateLimit-Limit" in response.headers
    assert "X-RateLimit-Remaining" in response.headers
