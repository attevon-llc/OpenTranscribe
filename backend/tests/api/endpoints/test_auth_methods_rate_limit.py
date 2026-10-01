"""Rate-limit coverage for ``GET /api/auth/methods`` (issue #1131).

Every page load of the SPA calls this public endpoint at least once (the root layout
reads the login-banner settings from it, and the login page reads it again), so a few
reloads in a minute must not trip the strict credential-endpoint limit
(``RATE_LIMIT_AUTH_PER_MINUTE``, 10/min) that exists for password guessing. It keeps its
own, still bounded, per-IP budget.

Same technique as ``test_search_rate_limit.py``: flip the module-level ``limiter`` on for
one test and reset its storage on the way out.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.auth.rate_limit import limiter
from app.core.config import settings

METHODS_PATH = "/api/auth/methods"


@pytest.fixture
def rate_limiting_enabled():
    was_enabled = limiter.enabled
    limiter.enabled = True
    try:
        limiter.reset()
    except Exception:
        pass
    try:
        yield
    finally:
        limiter.enabled = was_enabled
        try:
            limiter.reset()
        except Exception:
            pass


def test_normal_reloads_are_not_throttled(client, rate_limiting_enabled):
    """Twice the credential-endpoint limit of page loads in a minute still succeeds."""
    attempts = settings.RATE_LIMIT_AUTH_PER_MINUTE * 2
    codes = [client.get(METHODS_PATH).status_code for _ in range(attempts)]
    assert codes == [status.HTTP_200_OK] * attempts


def test_abuse_is_still_bounded(client, rate_limiting_enabled):
    """The endpoint is still rate limited: the request past its own budget gets a 429."""
    budget = settings.RATE_LIMIT_AUTH_METHODS_PER_MINUTE
    codes = [client.get(METHODS_PATH).status_code for _ in range(budget)]
    assert codes == [status.HTTP_200_OK] * budget
    blocked = client.get(METHODS_PATH)
    assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS, blocked.text
