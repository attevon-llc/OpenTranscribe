"""Functional tests for ``GET /api/admin/data-integrity/status`` (``admin.py``).

The status read behind the admin Data Integrity panel had **no functional
coverage** — the only mention of the path anywhere in ``tests/`` was
``unit/test_route_has_a_caller.py``, which asserts a path exists and never issues
a request. It is the operator's only view of whether an OpenSearch orphan sweep is
under way, and the panel enables or disables its Start button off that one flag.

The destructive sibling ``POST /admin/data-integrity`` is not exercised **here** — it
dispatches a sweep that deletes OpenSearch documents across the whole deployment. It is
covered in ``test_misc_uncovered_routes.py`` with the task and the Redis lock replaced by
stand-ins, so the authz tiers and the ``already_running`` guard (asserted to dispatch
nothing) are checked without a real sweep. Do not "complete" the coverage here by calling
it for real.

**Redis is faked; nothing else is.** These handlers read a lock key, and priming
the real Redis with ``data_integrity_running`` would make the live admin panel
believe a sweep was in progress. The index overview is read for real.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi import status

STATUS = "/api/admin/data-integrity/status"

#: Keys ``get_integrity_status`` reads (``app/tasks/opensearch_integrity_task.py``).
LOCK = "data_integrity_running"
LAST_RUN = "data_integrity_last_run"


class _InMemoryRedis:
    """The three Redis operations ``get_integrity_status`` uses, backed by a dict.

    Deliberately tiny: a broader double would start asserting its own behaviour.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self.store.pop(key, None) is not None)


@pytest.fixture
def fake_redis():
    """Patch the task module's ``get_redis`` with an in-memory double.

    The module binds ``get_redis`` at import (``from app.core.redis import
    get_redis``), so the patch target is the *module attribute*.
    """
    fake = _InMemoryRedis()
    with patch("app.tasks.opensearch_integrity_task.get_redis", return_value=fake):
        yield fake


# ---------------------------------------------------------------------------
# Privilege tier
# ---------------------------------------------------------------------------
def test_a_plain_user_is_refused(client, user_token_headers):
    """The overview reports deployment-wide index document counts.

    Catches the dependency being relaxed to ``get_current_active_user``: any
    account would learn how many documents every index holds, and — paired with the
    POST sibling — could tell whether an admin's sweep was running.
    """
    response = client.get(STATUS, headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_it_requires_authentication(client):
    response = client.get(STATUS)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_an_admin_does_not_need_super_admin(client, admin_token_headers, fake_redis):
    """The control for the refusal above: this is the ``admin`` tier.

    Without it, a handler gated to ``get_current_active_superuser`` would pass the
    negative tests and 403 the admins the panel is built for.
    """
    response = client.get(STATUS, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# Response contract
# ---------------------------------------------------------------------------
def test_reports_not_running_and_carries_an_index_overview(client, admin_token_headers, fake_redis):
    """The idle shape: no lock, no last run, plus the index overview panel.

    Catches ``index_overview`` being dropped from the merge — the admin page renders
    the per-index document counts from that key and would show an empty panel with
    no error.
    """
    response = client.get(STATUS, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) == {"running", "last_run", "index_overview"}
    assert body["running"] is False
    assert body["last_run"] is None
    assert isinstance(body["index_overview"], dict)


def test_reports_running_while_the_lock_is_held(client, admin_token_headers, fake_redis):
    """The other half of the pair: ``running`` really is derived from the lock.

    Without this, a handler hardcoding ``False`` would pass the idle test above and
    the panel would offer "Start" during a live sweep — two concurrent orphan
    cleanups deleting each other's candidates.
    """
    fake_redis.store[LOCK] = "1"

    response = client.get(STATUS, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["running"] is True


def test_decodes_the_stored_last_run(client, admin_token_headers, fake_redis):
    """``last_run`` is stored as JSON and must be served as an object, not a string.

    Catches the ``json.loads`` being dropped: the panel would render the raw JSON
    text, and any consumer indexing into the result would fail on a string.
    """
    fake_redis.store[LAST_RUN] = json.dumps({"deleted": 4, "dry_run": False})

    response = client.get(STATUS, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["last_run"] == {"deleted": 4, "dry_run": False}
