"""Functional tests for the directory-sync admin API (``directory_sync_settings.py``, issue #484).

Before this router existed there was **no way to reach ``directory_sync.*`` SystemSettings
from the API at all** — the periodic LDAP deprovisioning sweep
(``app/tasks/directory_sync_task.py``) could only ever be turned on by an operator writing
directly to the database, so `directory_sync.enabled` stayed at its coded ``False`` default
in every real deployment. See the issue for the full gap analysis.

Mirrors ``test_backup_settings.py``'s shape: privilege tier, GET defaults, PUT validation
(only-provided-fields, empty-body 400, bad-cron 400), GET /status due-gating, POST /run
dispatch. This router is thinner than backup's (no secrets, no S3, no file listing), so this
file is proportionally smaller.

``xdist_group("directory_sync_task_system_settings")``: joins ``test_directory_sync_task.py``'s
group — both write ``directory_sync.*`` ``SystemSettings`` keys, and two xdist workers
inserting overlapping keys in different orders deadlock on ``system_settings_key_key``
(issue #389). ``test_directory_sync_task.py``'s docstring claiming no other file writes this
namespace is now stale; this file is the second one.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from fastapi import status

from app.core import constants as C  # noqa: N812
from app.models.system_settings import SystemSettings
from app.services import directory_sync_service as dss
from app.services import system_settings_service as sss

pytestmark = pytest.mark.xdist_group("directory_sync_task_system_settings")

BASE = "/api/admin/directory-sync"


@pytest.fixture
def clean_directory_sync_settings(db_session):
    """Remove any ambient ``directory_sync.*`` rows so coded defaults are observable."""
    db_session.query(SystemSettings).filter(SystemSettings.key.like("directory_sync.%")).delete(
        synchronize_session=False
    )
    db_session.commit()
    return db_session


# ---------------------------------------------------------------------------
# Privilege tier
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", BASE, None),
        ("PUT", BASE, {"enabled": True}),
        ("GET", f"{BASE}/status", None),
        ("POST", f"{BASE}/run", None),
    ],
)
def test_plain_admin_is_refused_on_every_route(client, admin_token_headers, method, path, body):
    """This sweep disables accounts and reconciles group membership (privilege).

    Catches any route here being re-gated to ``get_current_admin_user`` — a plain admin
    could then flip ``enabled``/``dry_run`` and trigger a live deprovisioning pass,
    disabling arbitrary accounts (including other admins' sessions).
    """
    response = client.request(method, path, headers=admin_token_headers, json=body)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_get_requires_authentication(client):
    response = client.get(BASE)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET / PUT
# ---------------------------------------------------------------------------
def test_get_returns_the_coded_defaults(
    client, super_admin_token_headers, clean_directory_sync_settings
):
    """With nothing stored, the wire shape is the timid coded defaults.

    Catches a wrong default ever going out: this sweep disables accounts, so an
    ``enabled=True`` or ``dry_run=False`` default reaching a first-boot deployment
    would let a directory misconfiguration lock people out before anyone saw a log line.
    """
    response = client.get(BASE, headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["enabled"] == C.DEFAULT_DIRECTORY_SYNC_ENABLED
    assert body["enabled"] is False
    assert body["dry_run"] == C.DEFAULT_DIRECTORY_SYNC_DRY_RUN
    assert body["dry_run"] is True
    assert body["schedule"] == C.DEFAULT_DIRECTORY_SYNC_SCHEDULE
    assert body["max_disables_per_run"] == C.DEFAULT_DIRECTORY_SYNC_MAX_DISABLES_PER_RUN
    assert body["last_run_at"] is None
    assert body["last_result"] is None


def test_put_persists_only_the_provided_fields(
    client, super_admin_token_headers, clean_directory_sync_settings, db_session
):
    """This is THE fix for #484: an admin can now flip ``enabled`` at all.

    Also proves the only-provided-fields contract: setting ``enabled`` must not
    reset ``dry_run`` or ``schedule`` to their coded defaults.
    """
    response = client.put(BASE, headers=super_admin_token_headers, json={"enabled": True})
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["enabled"] is True
    assert body["dry_run"] == C.DEFAULT_DIRECTORY_SYNC_DRY_RUN
    assert body["schedule"] == C.DEFAULT_DIRECTORY_SYNC_SCHEDULE

    stored = sss.get_setting(db_session, dss.KEY_ENABLED)
    assert stored is not None
    assert stored.lower() == "true"


def test_put_with_no_fields_is_a_400(
    client, super_admin_token_headers, clean_directory_sync_settings
):
    """An empty patch is refused rather than answered with an unchanged 200."""
    response = client.put(BASE, headers=super_admin_token_headers, json={})
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_put_invalid_cron_is_a_400_not_a_500(
    client, super_admin_token_headers, clean_directory_sync_settings, db_session
):
    """A bad cron must not become an unhandled 500 (the ``ValueError`` -> 400 mapping)."""
    response = client.put(BASE, headers=super_admin_token_headers, json={"schedule": "not a cron"})
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert sss.get_setting(db_session, dss.KEY_SCHEDULE) is None


def test_put_zero_max_disables_is_a_422(
    client, super_admin_token_headers, clean_directory_sync_settings
):
    """The floor of 1 lives on the request model (``ge=1``), so this never reaches the service."""
    response = client.put(BASE, headers=super_admin_token_headers, json={"max_disables_per_run": 0})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------
def test_status_reports_due_when_enabled_and_not_due_when_disabled(
    client, super_admin_token_headers, clean_directory_sync_settings, db_session
):
    """``next_due`` must be gated on ``enabled`` — same schedule, opposite answers.

    Catches the ``if cfg["enabled"]`` guard being dropped: the admin panel would
    announce an overdue reconciliation pass on a deployment where the sweep is
    switched off. The disabled half is the control.
    """
    dss.update_settings(db_session, enabled=True, schedule="* * * * *")
    dss.update_settings_last_run(db_session, (datetime.now(UTC) - timedelta(minutes=5)).isoformat())
    db_session.commit()
    enabled = client.get(f"{BASE}/status", headers=super_admin_token_headers)
    assert enabled.status_code == status.HTTP_200_OK
    assert enabled.json()["next_due"] is True

    dss.update_settings(db_session, enabled=False)
    db_session.commit()
    disabled = client.get(f"{BASE}/status", headers=super_admin_token_headers)
    assert disabled.json()["next_due"] is False


def test_status_treats_an_unparseable_stored_cron_as_not_due(
    client, super_admin_token_headers, clean_directory_sync_settings, db_session
):
    """A schedule written before validation existed must not break the status panel."""
    sss.set_setting(db_session, dss.KEY_ENABLED, True, "test")
    sss.set_setting(db_session, dss.KEY_SCHEDULE, "not a cron", "test")
    db_session.commit()
    response = client.get(f"{BASE}/status", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["schedule"] == "not a cron"
    assert body["next_due"] is False


def test_status_reflects_the_last_recorded_result(
    client, super_admin_token_headers, clean_directory_sync_settings, db_session
):
    """The operator-visibility half of the fix: last run outcome is readable via the API."""
    dss.update_settings_last_run(db_session, "2026-08-01T04:00:00+00:00")
    dss.record_result(
        db_session,
        {"status": "ok", "dry_run": True, "candidates": 12, "disabled": 0, "reconciled": 12},
    )
    db_session.commit()
    response = client.get(f"{BASE}/status", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["last_run_at"] == "2026-08-01T04:00:00+00:00"
    assert body["last_result"]["status"] == "ok"
    assert body["last_result"]["candidates"] == 12


# ---------------------------------------------------------------------------
# POST /run
# ---------------------------------------------------------------------------
def test_run_returns_the_dispatched_task_id(
    client, super_admin_token_headers, clean_directory_sync_settings
):
    """A manual run answers with the task id the caller polls.

    Celery dispatch is no-oped by the autouse ``_skip_celery_dispatch`` fixture, so this
    exercises the handler body, not a broker.
    """
    response = client.post(f"{BASE}/run", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "queued"
    assert body["task_id"] == "test-task-id"
