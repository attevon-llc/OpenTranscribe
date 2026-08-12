"""Functional tests for ``POST /api/admin/backup/mirror/{test-s3,run}``.

The two action routes of ``media_mirror_settings.py`` had **no functional
coverage**: nothing in ``tests/`` issued a request to either, and
``unit/test_route_privilege_tiers.py`` asserts a prefix dependency for
``/api/admin/media-mirror``, which is not where the router is mounted. (The
settings GET/PUT half lives in ``test_admin_backup_mirror_routes.py``.)

Both matter because the mirror copies **every media object in the deployment** to
an off-host destination: ``/test-s3`` handles a just-typed credential, and ``/run``
starts the copy.

**No outbound S3 call and no real mirror run happens here.** The ``/test-s3``
probes are short-circuited by an unset bucket or an unroutable endpoint
(``127.0.0.1:1``), and ``/run``'s ``apply_async`` is no-oped by the autouse
``_skip_celery_dispatch`` fixture, so nothing reaches the download queue.

``xdist_group("backup_system_settings")`` for the same reason as its sibling:
these tests touch ``backup.mirror_*`` ``SystemSettings`` rows, which share the
``backup.%`` key namespace with four other suites, and two xdist workers inserting
overlapping keys in different orders deadlock on ``system_settings_key_key``
(issue #389).
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.models.system_settings import SystemSettings
from app.services import media_mirror_service as mm
from app.services import system_settings_service as sss

pytestmark = pytest.mark.xdist_group("backup_system_settings")

BASE = "/api/admin/backup/mirror"
TEST_S3 = f"{BASE}/test-s3"
RUN = f"{BASE}/run"


@pytest.fixture
def clean_mirror_settings(db_session):
    """Remove ambient ``backup.mirror_*`` rows so coded defaults are observable.

    The dev stack can carry admin overrides from manual testing. The deletion
    happens inside the test's savepoint and rolls back at teardown, so the live
    configuration is restored.
    """
    db_session.query(SystemSettings).filter(SystemSettings.key.like("backup.mirror_%")).delete(
        synchronize_session=False
    )
    db_session.commit()
    return db_session


# ---------------------------------------------------------------------------
# Privilege tier
# ---------------------------------------------------------------------------
_ROUTES: list[tuple[str, str, dict | None]] = [
    ("POST", TEST_S3, {}),
    ("POST", RUN, None),
]


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_plain_admin_is_refused_on_every_route(client, admin_token_headers, method, path, body):
    """Starting an off-host copy of every media file is deployment configuration.

    Catches either route being re-gated to ``get_current_admin_user``: paired with
    a PUT that repointed the destination, a plain admin could exfiltrate the whole
    media bucket to a bucket they control.
    """
    response = client.request(method, path, headers=admin_token_headers, json=body)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_a_plain_user_is_refused_on_every_route(client, user_token_headers, method, path, body):
    response = client.request(method, path, headers=user_token_headers, json=body)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_every_route_requires_authentication(client, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# POST /test-s3
# ---------------------------------------------------------------------------
def test_test_s3_without_a_bucket_returns_a_failed_envelope(
    client, super_admin_token_headers, clean_mirror_settings
):
    """The connection test reports failure as data and never raises.

    Catches the envelope being replaced by a raised exception: the admin panel
    renders ``error`` inline, so a 500 leaves the operator with no diagnosis. No
    bucket is configured, so this asserts the short-circuit *before* any network
    call — the test cannot depend on outbound connectivity.
    """
    response = client.post(TEST_S3, headers=super_admin_token_headers, json={})
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["ok"] is False
    assert "bucket" in body["error"]


def test_test_s3_does_not_persist_the_submitted_secret(
    client, super_admin_token_headers, clean_mirror_settings, db_session
):
    """A just-typed credential is probed in place, never written.

    Catches the handler calling ``update_settings`` before probing — clicking "Test
    Connection" would then commit an untested credential, and a typo would break the
    next scheduled mirror run rather than failing in the dialog.
    """
    response = client.post(
        TEST_S3,
        headers=super_admin_token_headers,
        json={
            "s3_bucket": "bucket-that-does-not-exist-ot-test",
            "s3_endpoint_url": "http://127.0.0.1:1",
            "s3_access_key_id": "AKIAPROBE",
            "s3_secret_key": "probe-secret",  # noqa: S106 - test literal
        },
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["ok"] is False
    assert sss.get_setting(db_session, mm.KEY_S3_SECRET_KEY) is None
    assert sss.get_setting(db_session, mm.KEY_S3_BUCKET) is None


# ---------------------------------------------------------------------------
# POST /run
# ---------------------------------------------------------------------------
def test_run_returns_the_dispatched_task_id(
    client, super_admin_token_headers, clean_mirror_settings
):
    """A manual run answers with the task id the caller polls.

    Catches the handler returning the ``AsyncResult`` object (unserializable) or a
    placeholder id — a scripted mirror has nothing else to correlate against.
    Celery dispatch is no-oped by the autouse ``_skip_celery_dispatch`` fixture, so
    this exercises the handler body and not a broker.
    """
    response = client.post(RUN, headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "queued"
    assert body["task_id"] == "test-task-id"
    assert "mirror" in body["message"].lower()
