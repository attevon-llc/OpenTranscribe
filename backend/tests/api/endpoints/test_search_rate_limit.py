"""Rate-limit coverage for ``GET /search`` (issue #904).

The hybrid transcript+summary search carried no rate limit at all, despite
being able to burn ~2s of Presidio snippet masking per call (`search/CLAUDE.md`'s
per-page masking cost table). `RATE_LIMIT_SEARCH_PER_MINUTE` closes the volume
gap; `/count` and `/suggestions` are deliberately excluded — see
`test_the_find_bar_polls_are_not_limited_with_the_search_page` below.

Same technique as `test_user_search_rate_limit.py`: flip the module-level
`limiter` on for one test, reset its storage on the way out.
"""

from __future__ import annotations

import uuid as uuid_pkg
from unittest.mock import patch

import pytest
from fastapi import status

from app.auth.rate_limit import limiter
from app.core.config import settings

SEARCH_PATH = "/api/search"
COUNT_PATH = "/api/search/count"


@pytest.fixture
def rate_limiting_enabled():
    """Turn the real slowapi limiter on for one test, then clean up after it.

    See `test_llm_settings_rate_limit.py`'s fixture of the same name for why
    the `.reset()` call is wrapped: host runs routinely have no Redis reachable.
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


def _search_summaries_only(client, headers):
    """`result_type=summaries` against an empty corpus needs no OpenSearch
    client at all — the transcript leg is skipped entirely when
    `want_transcripts` is false (`search.py`'s `search_transcripts`)."""
    return client.get(
        SEARCH_PATH,
        params={"q": uuid_pkg.uuid4().hex, "result_type": "summaries"},
        headers=headers,
    )


def test_search_is_rate_limited_per_user(client, user_token_headers, rate_limiting_enabled):
    """The Nth+1 request within the window is refused with 429."""
    limit = settings.RATE_LIMIT_SEARCH_PER_MINUTE
    for _ in range(limit):
        resp = _search_summaries_only(client, user_token_headers)
        assert resp.status_code == status.HTTP_200_OK, resp.text

    blocked = _search_summaries_only(client, user_token_headers)
    assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS, blocked.text


def test_the_search_bucket_is_keyed_per_user_not_by_ip(
    client, user_token_headers, admin_token_headers, rate_limiting_enabled
):
    """A second authenticated user is not punished by the first user's usage,
    even though both requests originate from the same TestClient/IP."""
    limit = settings.RATE_LIMIT_SEARCH_PER_MINUTE
    for _ in range(limit):
        resp = _search_summaries_only(client, user_token_headers)
        assert resp.status_code == status.HTTP_200_OK, resp.text

    assert (
        _search_summaries_only(client, user_token_headers).status_code
        == status.HTTP_429_TOO_MANY_REQUESTS
    )

    other_resp = _search_summaries_only(client, admin_token_headers)
    assert other_resp.status_code == status.HTTP_200_OK, other_resp.text


def test_the_find_bar_polls_are_not_limited_with_the_search_page(
    client, user_token_headers, rate_limiting_enabled
):
    """Pins the deliberate scope decision: `/count` is a per-keystroke poll and
    must stay reachable even after `/search` itself is exhausted.

    ⚠️ Does NOT re-assert the 429 precondition (that is
    `test_search_is_rate_limited_per_user`'s job) — exhausting `/search`'s
    bucket here is scaffolding, not this test's claim. This is deliberate:
    with no rate limit on `/search` at all, the scaffolding would just run
    without ever tripping 429 and this test would still pass, because its own
    assertion is only about `/count`. That is a SCOPE guard, not evidence
    that `/search` itself is limited — read this test's pass/fail together
    with `test_search_is_rate_limited_per_user`, never alone.
    """
    limit = settings.RATE_LIMIT_SEARCH_PER_MINUTE
    for _ in range(limit + 1):
        _search_summaries_only(client, user_token_headers)

    with (
        patch(
            "app.services.search.hybrid_search_service.get_opensearch_client",
            return_value=None,
        ),
        patch(
            "app.services.search.hybrid_search_service._ensure_infrastructure",
            lambda: None,
        ),
    ):
        count_resp = client.get(
            COUNT_PATH, params={"q": uuid_pkg.uuid4().hex}, headers=user_token_headers
        )
    assert count_resp.status_code == status.HTTP_200_OK, count_resp.text
