"""Functional tests for the scheduled-backup admin API (``backup_settings.py``).

The module had **no functional coverage**: the only mention of
``/api/admin/backup`` in ``tests/`` was ``test_route_privilege_tiers.py``, which
asserts the prefix's dependency and never issues a request.
``unit/test_backup_service.py`` covers the *service* (cron parsing, GFS pruning,
settings round-trip) but nothing in the HTTP layer — so the response assembly,
the ValueError→400 mapping, and the "the S3 secret is write-only" rule were all
unexecuted.

The security shape of this router is why it is worth testing at all: it stores an
S3 access key and secret for a destination that receives a full database dump.
Pinned here:

* every route is super_admin, not admin;
* the secret is accepted on PUT, encrypted at rest, and **never** on the wire;
* a bad cron / destination type is a 400, not a 500;
* ``next_due`` is gated on ``enabled`` and survives an unparseable stored cron;
* ``/list`` and ``/test-s3`` degrade to data instead of raising.

``xdist_group("backup_system_settings")``: these tests upsert the same
``backup.*`` ``SystemSettings`` keys as ``unit/test_backup_service.py``,
``unit/test_backup_metrics.py`` and ``unit/test_backup_alerts.py``. Two xdist
workers inserting overlapping keys in different orders deadlock on
``system_settings_key_key`` (issue #389), so this file joins their group.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from fastapi import status

from app.core import constants as C  # noqa: N812
from app.models.system_settings import SystemSettings
from app.services import backup_service as bs
from app.services import system_settings_service as sss
from app.utils.encryption import decrypt_api_key

pytestmark = pytest.mark.xdist_group("backup_system_settings")

BASE = "/api/admin/backup"


@pytest.fixture
def clean_backup_settings(db_session):
    """Remove any ambient ``backup.*`` rows so coded defaults are observable.

    The dev stack can carry admin overrides from manual testing; the deletion
    happens inside the test's savepoint and rolls back at teardown.
    """
    db_session.query(SystemSettings).filter(SystemSettings.key.like("backup.%")).delete(
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
        ("POST", f"{BASE}/test-s3", {}),
        ("POST", f"{BASE}/run", None),
        ("GET", f"{BASE}/list", None),
    ],
)
def test_plain_admin_is_refused_on_every_route(client, admin_token_headers, method, path, body):
    """Backup config is deployment config: it holds the off-host S3 credential.

    Catches any route here being re-gated to ``get_current_admin_user`` — a plain
    admin could then read the destination, repoint it at a bucket they control and
    trigger a run, exfiltrating the entire database.
    """
    response = client.request(method, path, headers=admin_token_headers, json=body)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_get_requires_authentication(client):
    response = client.get(BASE)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET — defaults and the secret-exposure rule
# ---------------------------------------------------------------------------
def test_get_returns_coded_defaults_and_no_secret_field(
    client, super_admin_token_headers, clean_backup_settings
):
    """With nothing stored, the wire shape is the coded defaults plus mount status.

    Catches the response ever carrying ``s3_secret_key``: the panel binds every
    returned field into its form, and a returned secret is then re-submitted and
    encrypted over itself (the mistake ``AuthConfigResponse.is_set`` exists to
    prevent). Also catches ``_s3_status`` probing S3 on a ``local`` destination,
    which would make an unrelated settings read do network I/O.
    """
    response = client.get(BASE, headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "s3_secret_key" not in body
    assert body["s3_secret_key_set"] is False
    assert body["schedule"] == C.DEFAULT_BACKUP_SCHEDULE
    assert body["destination"] == C.DEFAULT_BACKUP_DESTINATION
    assert body["destination_type"] == C.DEFAULT_BACKUP_DESTINATION_TYPE
    assert body["destination_status"]["destination"] == C.DEFAULT_BACKUP_DESTINATION
    assert body["s3_status"] is None


def test_put_stores_the_s3_secret_encrypted_and_never_returns_it(
    client, super_admin_token_headers, clean_backup_settings, db_session
):
    """The secret is write-only, and what lands in the DB is not the plaintext.

    Catches two independent regressions: echoing the submitted secret back (it
    would then appear in browser devtools and any response log), and storing it
    unencrypted, which would put an S3 secret in every ``pg_dump`` of this
    database in cleartext.
    """
    secret = "s3-secret-value-do-not-log"  # noqa: S105 - test literal, not a credential
    response = client.put(
        BASE,
        headers=super_admin_token_headers,
        json={"s3_access_key_id": "AKIAEXAMPLE", "s3_secret_key": secret},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "s3_secret_key" not in body
    assert body["s3_secret_key_set"] is True
    assert body["s3_access_key_id"] == "AKIAEXAMPLE"

    stored = sss.get_setting(db_session, bs.KEY_S3_SECRET_KEY)
    assert stored is not None
    assert stored != secret
    assert decrypt_api_key(stored) == secret


def test_put_with_no_fields_is_a_400(client, super_admin_token_headers, clean_backup_settings):
    """An empty patch is refused rather than answered with an unchanged 200.

    Catches the guard being dropped: the UI's save button would report success for
    a payload that changed nothing, hiding a serialization bug in the form.
    """
    response = client.put(BASE, headers=super_admin_token_headers, json={})
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_put_invalid_cron_is_a_400_not_a_500(
    client, super_admin_token_headers, clean_backup_settings, db_session
):
    """``update_settings`` raises ValueError for a bad cron; the router maps it to 400.

    Catches removal of the ``except ValueError`` — an operator typo would become an
    unhandled 500, and (worse) the failure would be indistinguishable from the
    backend being down.
    """
    response = client.put(BASE, headers=super_admin_token_headers, json={"schedule": "not a cron"})
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert sss.get_setting(db_session, bs.KEY_SCHEDULE) is None


def test_put_unknown_destination_type_is_a_400(
    client, super_admin_token_headers, clean_backup_settings
):
    response = client.put(BASE, headers=super_admin_token_headers, json={"destination_type": "ftp"})
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_put_out_of_range_retention_is_a_422(
    client, super_admin_token_headers, clean_backup_settings
):
    """Retention bounds live on the request model, so this never reaches the service."""
    response = client.put(BASE, headers=super_admin_token_headers, json={"retention_daily": -1})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------
def test_status_reports_due_when_enabled_and_not_due_when_disabled(
    client, super_admin_token_headers, clean_backup_settings, db_session
):
    """``next_due`` must be gated on ``enabled`` — same schedule, opposite answers.

    Catches the ``if cfg["enabled"]`` guard being dropped: the admin panel would
    announce an overdue backup on a deployment where scheduling is switched off,
    and the operator's only fix would be to change a schedule that never runs.
    The disabled half is the control — without it an implementation that always
    returns True would pass.
    """
    bs.update_settings(db_session, enabled=True, schedule="* * * * *")
    bs.update_settings_last_run(db_session, (datetime.now(UTC) - timedelta(minutes=5)).isoformat())
    db_session.commit()
    enabled = client.get(f"{BASE}/status", headers=super_admin_token_headers)
    assert enabled.status_code == status.HTTP_200_OK
    assert enabled.json()["next_due"] is True

    bs.update_settings(db_session, enabled=False)
    db_session.commit()
    disabled = client.get(f"{BASE}/status", headers=super_admin_token_headers)
    assert disabled.json()["next_due"] is False


def test_status_treats_an_unparseable_stored_cron_as_not_due(
    client, super_admin_token_headers, clean_backup_settings, db_session
):
    """A schedule written before validation existed must not break the status panel.

    Catches removal of the ``except ValueError`` around ``is_due``: the status
    endpoint is what an operator opens to *diagnose* backups, so it failing with a
    500 removes the only view of the problem.
    """
    sss.set_setting(db_session, bs.KEY_ENABLED, True, "test")
    sss.set_setting(db_session, bs.KEY_SCHEDULE, "not a cron", "test")
    db_session.commit()
    response = client.get(f"{BASE}/status", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["schedule"] == "not a cron"
    assert body["next_due"] is False


def test_status_omits_the_opensearch_probe_when_snapshots_are_off(
    client, super_admin_token_headers, clean_backup_settings, db_session
):
    """The snapshot probe is a live network call, so it is only made when opted in.

    Catches the ``include_opensearch`` guard being dropped, which would make the
    status endpoint reach out to OpenSearch on every poll of a pg-only deployment.

    The setting is written EXPLICITLY rather than leaned on as the coded default: since
    issue #658 the default is ``True`` (the speaker indices hold voiceprints, which exist
    nowhere else and are not "derived data"), so a test that inferred "off" from the
    default would silently stop exercising the off path.
    """
    from app.services import backup_service as bs

    bs.update_settings(db_session, include_opensearch=False)

    response = client.get(f"{BASE}/status", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["include_opensearch"] is False
    assert body["opensearch_snapshot_status"] is None


# ---------------------------------------------------------------------------
# GET /list
# ---------------------------------------------------------------------------
def test_list_returns_backup_files_newest_first_with_the_encrypted_flag(
    client, super_admin_token_headers, clean_backup_settings, db_session, tmp_path
):
    """Ordering and the ``encrypted`` flag are what the restore UI reads.

    Catches an inverted sort (the operator would restore the oldest dump when
    recovering) and a mis-derived ``encrypted`` flag (a ``.gpg`` restored without
    the passphrase fails at the worst possible moment). Files live in ``tmp_path``,
    which pytest removes.
    """
    (tmp_path / "opentranscribe-20260101-030000.dump").write_bytes(b"older")
    (tmp_path / "opentranscribe-20260102-030000.dump.gpg").write_bytes(b"newer-encrypted")
    (tmp_path / "unrelated.txt").write_bytes(b"ignored")
    bs.update_settings(db_session, destination=str(tmp_path))
    db_session.commit()

    response = client.get(f"{BASE}/list", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert [b["filename"] for b in body["backups"]] == [
        "opentranscribe-20260102-030000.dump.gpg",
        "opentranscribe-20260101-030000.dump",
    ]
    assert body["backups"][0]["encrypted"] is True
    assert body["backups"][1]["encrypted"] is False
    assert body["destination_status"]["exists"] is True
    assert body["destination_status"]["writable"] is True


def test_list_on_a_missing_destination_is_an_empty_200(
    client, super_admin_token_headers, clean_backup_settings, db_session, tmp_path
):
    """An unmounted destination reports "not mounted", not an error.

    Catches ``list_backups`` losing its ``is_dir()`` guard: the most common real
    misconfiguration (the host directory was never bind-mounted) would surface as
    a 500 instead of the mount-status banner built for exactly this case.
    """
    missing = tmp_path / "never-mounted"
    bs.update_settings(db_session, destination=str(missing))
    db_session.commit()

    response = client.get(f"{BASE}/list", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["backups"] == []
    assert body["destination_status"]["exists"] is False
    assert body["destination_status"]["mounted"] is False


# ---------------------------------------------------------------------------
# POST /test-s3 and POST /run
# ---------------------------------------------------------------------------
def test_test_s3_without_a_bucket_returns_a_failed_envelope(
    client, super_admin_token_headers, clean_backup_settings
):
    """The connection test reports failure as data and never raises.

    Catches the envelope being replaced by a raised exception: the admin panel
    shows ``error`` inline, so a 500 here leaves the operator with no diagnosis at
    all. No bucket is configured, so this asserts the short-circuit *before* any
    network call — the test cannot depend on outbound connectivity.
    """
    response = client.post(f"{BASE}/test-s3", headers=super_admin_token_headers, json={})
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["ok"] is False
    assert "bucket" in body["error"]


def test_run_returns_the_dispatched_task_id(
    client, super_admin_token_headers, clean_backup_settings
):
    """A manual run answers with the task id the caller polls.

    Catches the handler returning the ``AsyncResult`` object (unserializable) or a
    placeholder id — a scripted backup has nothing else to correlate against.
    Celery dispatch is no-oped by the autouse ``_skip_celery_dispatch`` fixture, so
    this exercises the handler body, not a broker.
    """
    response = client.post(f"{BASE}/run", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "queued"
    assert body["task_id"] == "test-task-id"
