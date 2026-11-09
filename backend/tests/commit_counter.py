"""Count ``Session.commit()`` calls made while a block runs.

Moving queries out of a router must not move its transaction boundary. A characterization
test wraps the request in ``count_commits(db_session)`` and pins the number of commits.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def count_commits(session: Any) -> Iterator[list[int]]:
    """Yield a one-element list holding the number of ``session.commit()`` calls so far."""
    calls = [0]
    original = session.commit

    def counting_commit(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        return original(*args, **kwargs)

    session.commit = counting_commit
    try:
        yield calls
    finally:
        session.commit = original


def endpoint_commits(client: Any, session: Any, method: str, path: str, **kwargs: Any) -> int:
    """Commits an endpoint adds beyond what authenticating the same request costs.

    Every authenticated request commits a fixed amount in the auth layer. That amount is
    measured on ``GET /api/users/me`` (a handler that writes nothing) with the same headers,
    and subtracted, so the number returned is the handler's own.
    """
    headers = kwargs.get("headers")
    with count_commits(session) as overhead:
        assert client.get("/api/users/me", headers=headers).status_code == 200
    with count_commits(session) as total:
        response = client.request(method, path, **kwargs)
    assert response.status_code == 200, (path, response.status_code, response.text)
    return total[0] - overhead[0]
