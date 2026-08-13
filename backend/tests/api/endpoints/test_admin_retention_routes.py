"""Functional tests for the file-retention config API (``admin.py``).

``GET``/``PUT /api/admin/settings/retention-config`` and
``GET .../status`` had **no functional coverage**:
``unit/test_retention_cleanup_task.py`` covers the Celery task and
``unit/test_route_has_a_caller.py`` only asserts the paths exist. The HTTP layer —
the schema bounds and the last-run bookkeeping the panel polls — was unexecuted.
(The ``/preview`` and ``/run`` half lives in
``test_admin_retention_preview_routes.py``.)

This is the most destructive settings surface in the app: enabling it deletes
media files deployment-wide on a schedule. The invariants pinned here:

* the tier is ``admin`` (not super_admin), a plain user gets 403 and an anonymous
  caller 401;
* the coded defaults leave retention **off**;
* a partial patch writes only the fields it names, and is read back;
* out-of-range or malformed config is refused *and* not persisted.

Nothing destructive runs: these three routes only read and write
``SystemSettings`` rows, inside the test's savepoint.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.models.system_settings import SystemSettings
from app.services import system_settings_service as sss

BASE = "/api/admin/settings/retention-config"


@pytest.fixture
def clean_retention_settings(db_session):
    """Remove ambient ``files.retention*`` rows so coded defaults are observable.

    The dev stack can carry admin overrides from manual testing; the deletion
    happens inside the test's savepoint and rolls back at teardown.
    """
    db_session.query(SystemSettings).filter(
        SystemSettings.key.in_(
            [
                "files.retention_enabled",
                "files.retention_days",
                "files.delete_error_files",
                "files.retention_run_time",
                "files.retention_timezone",
                "files.retention_last_run",
                "files.retention_last_run_deleted",
            ]
        )
    ).delete(synchronize_session=False)
    db_session.commit()
    return db_session


# ---------------------------------------------------------------------------
# Privilege tier
# ---------------------------------------------------------------------------
_ROUTES = [
    ("GET", BASE, None),
    ("PUT", BASE, {"retention_days": 30}),
    ("GET", f"{BASE}/status", None),
]


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_a_plain_user_is_refused_on_every_route(client, user_token_headers, method, path, body):
    """A non-admin must not read or change the deployment's deletion policy.

    Catches the dependency being relaxed to ``get_current_active_user``: any
    account could enable automatic deletion, or shorten the window to its 1-day
    minimum, for every other account's media.
    """
    response = client.request(method, path, headers=user_token_headers, json=body)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_every_route_requires_authentication(client, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET / PUT
# ---------------------------------------------------------------------------
def test_get_returns_the_coded_defaults_with_retention_off(
    client, admin_token_headers, clean_retention_settings
):
    """A deployment that never configured retention deletes nothing.

    Catches ``retention_enabled``'s default flipping to True — automatic media
    deletion would become opt-out, and an upgrade would start deleting files on a
    deployment whose operator never asked for it.
    """
    response = client.get(BASE, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["retention_enabled"] is False
    assert body["retention_days"] == 90
    assert body["delete_error_files"] is False
    assert body["run_time"] == "02:00"
    assert body["timezone"] == "UTC"
    assert body["last_run"] is None
    assert body["last_run_deleted"] == 0


def test_put_persists_provided_fields_and_leaves_the_rest_alone(
    client, admin_token_headers, clean_retention_settings
):
    """A partial patch writes only what it names.

    Catches ``None`` values being written through, which would reset the schedule
    and the error-file flag every time the operator changed the window alone.
    """
    response = client.put(
        BASE,
        headers=admin_token_headers,
        json={"retention_enabled": True, "retention_days": 45, "run_time": "23:30"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["retention_enabled"] is True
    assert body["retention_days"] == 45
    assert body["run_time"] == "23:30"
    assert body["delete_error_files"] is False
    assert body["timezone"] == "UTC"

    reread = client.get(BASE, headers=admin_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["retention_days"] == 45


@pytest.mark.parametrize(
    "payload",
    [
        {"retention_days": 0},
        {"retention_days": 3651},
        {"run_time": "24:00"},
        {"run_time": "2:00"},
        {"timezone": "Mars/Olympus_Mons"},
    ],
)
def test_out_of_range_config_is_a_422_and_is_not_persisted(
    client, admin_token_headers, clean_retention_settings, db_session, payload
):
    """Bounds live on ``RetentionConfigUpdate``, so nothing reaches the service.

    ``retention_days=0`` is the dangerous one: a zero window makes every completed
    file immediately eligible, so it must be rejected at the wire rather than
    clamped. The non-persistence assertion catches validation moving after the
    write.
    """
    response = client.put(BASE, headers=admin_token_headers, json=payload)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    stored = sss.get_retention_config(db_session)
    assert stored["retention_days"] == 90
    assert stored["run_time"] == "02:00"
    assert stored["timezone"] == "UTC"


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------
def test_status_reports_the_last_run_bookkeeping(
    client, admin_token_headers, clean_retention_settings, db_session
):
    """``/status`` is what the panel polls after a manual run.

    Catches it being wired to a different settings namespace: the panel would
    report "never run" forever while sweeps kept happening, which is the state
    that makes an operator trigger a second one.
    """
    before = client.get(f"{BASE}/status", headers=admin_token_headers)
    assert before.status_code == status.HTTP_200_OK
    assert before.json()["last_run"] is None

    sss.set_setting(db_session, "files.retention_last_run", "2026-08-01T02:00:00+00:00", "test")
    sss.set_setting(db_session, "files.retention_last_run_deleted", 7, "test")
    db_session.commit()

    after = client.get(f"{BASE}/status", headers=admin_token_headers)
    assert after.status_code == status.HTTP_200_OK
    body = after.json()
    assert body["last_run"] == "2026-08-01T02:00:00+00:00"
    assert body["last_run_deleted"] == 7
