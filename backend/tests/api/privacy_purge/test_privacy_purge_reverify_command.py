"""``POST /admin/privacy-policy/purge-reverify``: the administrator's way to re-verify now (issue #1334 item 2).

The durable record of a purge's pending centroid ids (``privacy_purge_pending``, v461) is finished
by the ``privacy.purge_reverify`` beat task once a row is an hour old. This command runs that same
task on demand, so an administrator who sees a purge reported ``interrupted`` does not wait for
the next tick.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import status

from app.services.privacy import purge_pending
from app.tasks.privacy_purge_task import privacy_purge_reverify_task

REVERIFY = "/api/admin/privacy-policy/purge-reverify"


@pytest.fixture
def queued(monkeypatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    def record(args=None, kwargs=None, **options):
        sent.append(dict(kwargs or {}))
        return SimpleNamespace(id="reverify-task-1")

    monkeypatch.setattr(privacy_purge_reverify_task, "apply_async", record)
    return sent


def test_a_super_admin_queues_the_reverify_with_the_default_cutoff(
    client, super_admin_token_headers, queued
):
    response = client.post(REVERIFY, headers=super_admin_token_headers)

    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    assert response.json() == {"task_id": "reverify-task-1"}
    assert queued == [{"grace_seconds": purge_pending.REVERIFY_GRACE_SECONDS}]


@pytest.mark.xdist_group("local_model_redis")
def test_the_cutoff_is_passed_to_the_task_that_accepts_it(
    client, super_admin_token_headers, queued, reverify_lease
):
    response = client.post(
        REVERIFY, headers=super_admin_token_headers, json={"older_than_seconds": 600}
    )

    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    assert queued == [{"grace_seconds": 600}]
    # The kwarg the endpoint sends is one the real task takes: nothing pending, nothing due.
    summary = privacy_purge_reverify_task.apply(kwargs=queued[0]).get(propagate=True)
    assert set(summary) == {"rows", "finished", "remaining", "interrupted", "skipped"}
    assert summary["skipped"] == 0, "the run took its lease"


@pytest.mark.parametrize("seconds", [0, 299, 7 * 24 * 3600 + 1])
def test_a_cutoff_that_would_fail_a_purge_still_running_is_refused(
    client, super_admin_token_headers, queued, seconds
):
    response = client.post(
        REVERIFY, headers=super_admin_token_headers, json={"older_than_seconds": seconds}
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert queued == []


def test_only_a_super_admin_may_run_it(client, user_token_headers, admin_token_headers, queued):
    for headers in (user_token_headers, admin_token_headers):
        response = client.post(REVERIFY, headers=headers)
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert client.post(REVERIFY).status_code == status.HTTP_403_FORBIDDEN
    assert queued == []


def test_a_broker_failure_is_a_503_and_not_a_silent_success(
    client, super_admin_token_headers, monkeypatch
):
    def down(*_args, **_kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(privacy_purge_reverify_task, "apply_async", down)

    response = client.post(REVERIFY, headers=super_admin_token_headers)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
