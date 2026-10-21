"""Functional test for ``POST /api/admin/media-duration-backfill`` (issue #969).

The route had no coverage at all beyond ``test_route_has_a_caller.py`` asserting it
exists. Pinned here: the privilege tier is **super_admin**, not plain admin (a
data-repair backfill outranks the ordinary admin ceiling the same way GDPR erasure
does); the default is a dry run; and the response carries the Celery task id so an
operator can poll it. Celery dispatch is no-oped by the autouse
``_skip_celery_dispatch`` fixture, so no real task runs.
"""

from __future__ import annotations

from fastapi import status

ROUTE = "/api/admin/media-duration-backfill"


def test_plain_admin_is_refused(client, admin_token_headers):
    """A data-repair backfill outranks the ordinary admin ceiling."""
    response = client.post(ROUTE, headers=admin_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_anonymous_is_refused(client):
    response = client.post(ROUTE)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_super_admin_can_dispatch_and_defaults_to_dry_run(client, super_admin_token_headers):
    response = client.post(ROUTE, headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "started"
    assert body["dry_run"] is True
    assert body["task_id"]


def test_dry_run_can_be_disabled_explicitly(client, super_admin_token_headers):
    response = client.post(f"{ROUTE}?dry_run=false", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["dry_run"] is False
