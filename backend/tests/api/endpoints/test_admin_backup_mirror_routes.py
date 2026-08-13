"""Functional tests for the media-mirror admin API (``media_mirror_settings.py``).

``GET``/``PUT /api/admin/backup/mirror`` had **no functional coverage** (nor did
their ``/test-s3`` and ``/run`` siblings, now in
``test_admin_backup_mirror_ops_routes.py``):
``unit/test_media_mirror_service.py`` covers the service, and
``unit/test_route_privilege_tiers.py`` asserts a *prefix* dependency without ever
issuing a request — for ``/api/admin/media-mirror``, which is not where this
router is mounted (``router.py`` mounts it at ``/admin/backup/mirror``). Every
handler body here was therefore unexecuted: the response assembly, the
ValueError→400 mapping, the write-only secret rule, and the "no fields" guard.

Why it is worth testing: this router stores an S3 access key and secret for a
destination that receives a **copy of every media object in the deployment**. The
invariants pinned here:

* both routes are super_admin — a plain ``admin`` gets 403, not 200;
* the secret is accepted on PUT, encrypted at rest, and never on the wire;
* a bad cron / destination type is a readable 400, not a 500, and is not persisted;
* mirror settings are a **separate namespace** from the DB-dump settings.

No outbound S3 call is made: the reachability probe short-circuits on an unset
bucket.

``xdist_group("backup_system_settings")``: these tests upsert ``backup.mirror_*``
``SystemSettings`` rows, which share the ``backup.%`` key namespace with
``unit/test_backup_{service,metrics,alerts}.py`` and
``api/endpoints/test_backup_settings.py``. Two xdist workers inserting
overlapping keys in different orders deadlock on ``system_settings_key_key``
(issue #389), so this file joins their group.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.core import constants as C  # noqa: N812
from app.models.system_settings import SystemSettings
from app.services import backup_service as bs
from app.services import media_mirror_service as mm
from app.services import system_settings_service as sss
from app.utils.encryption import decrypt_api_key

pytestmark = pytest.mark.xdist_group("backup_system_settings")

BASE = "/api/admin/backup/mirror"


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
_ROUTES = [
    ("GET", BASE, None),
    ("PUT", BASE, {"enabled": True}),
]


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_plain_admin_is_refused_on_every_route(client, admin_token_headers, method, path, body):
    """Mirror config is deployment config: it holds an off-host S3 credential.

    Catches any route here being re-gated to ``get_current_admin_user`` — a plain
    admin could then repoint the mirror at a bucket they control and trigger a
    run, exfiltrating every media file in the deployment.
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
# GET
# ---------------------------------------------------------------------------
def test_get_returns_coded_defaults_and_never_the_secret(
    client, super_admin_token_headers, clean_mirror_settings
):
    """With nothing stored the wire shape is the coded defaults plus mount status.

    Catches the response ever carrying ``s3_secret_key``: the panel binds every
    returned field into its form, so a returned secret is re-submitted and
    encrypted over itself. Also catches ``_s3_status`` probing S3 on a ``local``
    destination, which would make an ordinary settings read do network I/O.
    """
    response = client.get(BASE, headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "s3_secret_key" not in body
    assert body["s3_secret_key_set"] is False
    assert body["enabled"] is C.DEFAULT_BACKUP_MIRROR_ENABLED
    assert body["schedule"] == C.DEFAULT_BACKUP_MIRROR_SCHEDULE
    assert body["destination_type"] == C.DEFAULT_BACKUP_MIRROR_DESTINATION_TYPE
    assert body["destination"] == C.DEFAULT_BACKUP_MIRROR_DESTINATION
    assert body["s3_prefix"] == C.DEFAULT_BACKUP_MIRROR_S3_PREFIX
    assert body["destination_status"]["destination"] == C.DEFAULT_BACKUP_MIRROR_DESTINATION
    assert body["s3_status"] is None


def test_get_reports_running_false_without_a_held_lock(
    client, super_admin_token_headers, clean_mirror_settings
):
    """``running`` is the overlap-lock probe, and it must degrade to False.

    ``task_lock_manager.is_locked`` swallows an unreachable Redis and answers
    False; catching a regression there matters because the panel's Run button is
    disabled off this flag — a raising probe would 500 the whole settings read.
    """
    response = client.get(BASE, headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["running"] is False


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------
def test_put_persists_provided_fields_and_get_agrees(
    client, super_admin_token_headers, clean_mirror_settings, tmp_path
):
    """A partial patch is written and read back; unmentioned fields keep defaults.

    Catches ``exclude_none`` being dropped (every unset field would be written as
    None and clobber the stored config) and a PUT that shapes a response without
    persisting.
    """
    response = client.put(
        BASE,
        headers=super_admin_token_headers,
        json={"enabled": True, "schedule": "15 4 * * *", "destination": str(tmp_path)},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["enabled"] is True
    assert body["schedule"] == "15 4 * * *"
    assert body["destination"] == str(tmp_path)
    assert body["destination_status"]["exists"] is True
    assert body["destination_status"]["writable"] is True
    assert body["throttle_ms"] == C.DEFAULT_BACKUP_MIRROR_THROTTLE_MS

    reread = client.get(BASE, headers=super_admin_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["schedule"] == "15 4 * * *"


def test_put_stores_the_s3_secret_encrypted_and_never_returns_it(
    client, super_admin_token_headers, clean_mirror_settings, db_session
):
    """The secret is write-only, and what lands in the DB is not the plaintext.

    Catches echoing the submitted secret back (it would appear in devtools and any
    response log) and storing it unencrypted, which would put an S3 secret into
    every ``pg_dump`` of this database in cleartext.
    """
    secret = "mirror-secret-value-do-not-log"  # noqa: S105 - test literal, not a credential
    response = client.put(
        BASE,
        headers=super_admin_token_headers,
        json={"s3_access_key_id": "AKIAMIRROR", "s3_secret_key": secret},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "s3_secret_key" not in body
    assert body["s3_secret_key_set"] is True
    assert body["s3_access_key_id"] == "AKIAMIRROR"

    stored = sss.get_setting(db_session, mm.KEY_S3_SECRET_KEY)
    assert stored is not None
    assert stored != secret
    assert decrypt_api_key(stored) == secret


def test_put_does_not_touch_the_database_dump_settings(
    client, super_admin_token_headers, clean_mirror_settings, db_session, tmp_path
):
    """The mirror and the DB dump are separate namespaces sharing a key prefix.

    Catches ``media_mirror_service`` being pointed at ``backup_service``'s keys —
    a change to the media mirror's destination would silently redirect the
    *database dump*, which is the one artifact a restore depends on.
    """
    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir()
    dump_destination_before = bs.get_settings(db_session)["destination"]

    response = client.put(
        BASE, headers=super_admin_token_headers, json={"destination": str(mirror_dir)}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["destination"] == str(mirror_dir)
    assert bs.get_settings(db_session)["destination"] == dump_destination_before


def test_put_with_no_fields_is_a_400(client, super_admin_token_headers, clean_mirror_settings):
    """An empty patch is refused rather than answered with an unchanged 200.

    Catches the guard being dropped: the UI's save button would report success for
    a payload that changed nothing, hiding a serialization bug in the form.
    """
    response = client.put(BASE, headers=super_admin_token_headers, json={})
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_put_invalid_cron_is_a_400_and_is_not_persisted(
    client, super_admin_token_headers, clean_mirror_settings, db_session
):
    """``update_settings`` raises ValueError for a bad cron; the router maps it to 400.

    Catches removal of the ``except ValueError``: an operator typo would become an
    unhandled 500, indistinguishable from the backend being down. The
    non-persistence half catches validation moving to *after* the write.
    """
    response = client.put(BASE, headers=super_admin_token_headers, json={"schedule": "not a cron"})
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert sss.get_setting(db_session, mm.KEY_SCHEDULE) is None


def test_put_unknown_destination_type_is_a_400(
    client, super_admin_token_headers, clean_mirror_settings
):
    response = client.put(
        BASE, headers=super_admin_token_headers, json={"destination_type": "rsync"}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_put_negative_throttle_is_a_422(client, super_admin_token_headers, clean_mirror_settings):
    """The throttle bound lives on the request model, so it never reaches the service."""
    response = client.put(BASE, headers=super_admin_token_headers, json={"throttle_ms": -1})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_selecting_the_s3_destination_adds_an_s3_status_block(
    client, super_admin_token_headers, clean_mirror_settings
):
    """The reachability block appears only for the S3 destination — this is its control.

    Paired with ``test_get_returns_coded_defaults_and_never_the_secret`` (which
    asserts ``s3_status is None`` on a ``local`` destination): together they pin
    that ``_s3_status`` branches on ``destination_type`` rather than always or
    never probing. No bucket is configured, so the probe short-circuits before any
    network call and ``reachable`` is False.
    """
    response = client.put(
        BASE,
        headers=super_admin_token_headers,
        json={"destination_type": mm.DEST_S3},
    )
    assert response.status_code == status.HTTP_200_OK
    s3_status = response.json()["s3_status"]
    assert s3_status is not None
    assert s3_status["reachable"] is False
