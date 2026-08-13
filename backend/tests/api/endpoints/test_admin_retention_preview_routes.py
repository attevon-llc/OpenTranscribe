"""Functional tests for the retention preview + manual run (``admin.py``).

``GET /api/admin/settings/retention-config/preview`` and
``POST .../run`` had **no functional coverage**:
``unit/test_retention_cleanup_task.py`` covers the Celery task and
``unit/test_route_has_a_caller.py`` only asserts the paths exist. The eligibility
query the operator makes their delete decision from was never executed over HTTP.
(The config GET/PUT/status half lives in ``test_admin_retention_routes.py``.)

This is the most destructive surface in the app: enabling retention deletes media
files deployment-wide on a schedule, and ``/preview`` is the only thing an operator
sees before they authorise it. The invariants pinned here:

* eligibility is age **and** status: an ``error`` file is included only when
  ``delete_error_files=true`` — with the excluding case as its control;
* ``/preview`` deletes nothing (checked against a real row either side);
* ``/run`` **dispatches** and does not delete inline;
* the tier is ``admin``, a plain user gets 403 and an anonymous caller 401.

**No real retention sweep is ever run here.** ``/run``'s ``celery_app.send_task``
is patched at the broker seam — it is the one call in these two routes that would
publish to a live queue where a worker would then delete real dev media. The
authz tests never reach the handler, so no dispatch happens there either.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import status

from app.models.media import MediaFile

BASE = "/api/admin/settings/retention-config"

#: Older than the 3650-day maximum retention window, so a preview with the largest
#: legal window matches these rows and nothing in the dev dataset.
_ANCIENT_DAYS = 4000
#: The largest window the schema accepts (~10 years), used so the fabricated rows
#: are the only matches.
_MAX_WINDOW = 3650


def _make_file(db_session, owner, *, status_value: str, age_days: int) -> MediaFile:
    """A completed-or-errored file whose completion is ``age_days`` in the past.

    Rolled back with the test's savepoint; no MinIO object is created, so nothing
    is left behind in the dev bucket either.
    """
    file_uuid = uuid_pkg.uuid4()
    completed = datetime.now(UTC) - timedelta(days=age_days)
    media_file = MediaFile(
        uuid=file_uuid,
        filename=f"retention-{file_uuid.hex[:8]}.wav",
        title=f"retention-{file_uuid.hex[:8]}",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=2048,
        status=status_value,
        is_public=False,
        user_id=owner.id,
        upload_time=completed,
        completed_at=completed,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


# ---------------------------------------------------------------------------
# Privilege tier
# ---------------------------------------------------------------------------
_ROUTES = [
    ("GET", f"{BASE}/preview?retention_days=30"),
    ("POST", f"{BASE}/run"),
]


@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_a_plain_user_is_refused_on_every_route(client, user_token_headers, method, path):
    """A non-admin must not preview or trigger deployment-wide deletion.

    Catches the dependency being relaxed to ``get_current_active_user``, which
    would let any account enumerate every other account's files through
    ``/preview`` (it reports owner emails and filenames) and then trigger a sweep.
    """
    response = client.request(method, path, headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_every_route_requires_authentication(client, method, path):
    response = client.request(method, path)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /preview
# ---------------------------------------------------------------------------
def test_preview_requires_the_retention_window(client, admin_token_headers):
    """``retention_days`` has no default — a preview without a window is a 422.

    Catches a default being added: the operator would be shown the impact of a
    window they never chose and could act on it.
    """
    response = client.get(f"{BASE}/preview", headers=admin_token_headers)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_preview_lists_an_eligible_file_with_its_owner_and_age(
    client, admin_token_headers, admin_user, db_session
):
    """The preview row is what the operator reads before authorising deletion.

    Catches the age/size/owner projection breaking — an operator cannot judge a
    deletion from a UUID alone — and the cutoff comparison being inverted, which
    would list *recent* files as expiring.
    """
    doomed = _make_file(db_session, admin_user, status_value="completed", age_days=_ANCIENT_DAYS)

    response = client.get(
        f"{BASE}/preview", params={"retention_days": _MAX_WINDOW}, headers=admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["file_count"] == 1
    row = body["files"][0]
    assert row["uuid"] == str(doomed.uuid)
    assert row["owner_email"] == admin_user.email
    assert row["size_bytes"] == 2048
    assert row["status"] == "completed"
    assert row["age_days"] >= _MAX_WINDOW
    assert body["total_size_bytes"] == 2048


def test_preview_excludes_error_files_unless_asked_for(
    client, admin_token_headers, admin_user, db_session
):
    """``delete_error_files`` is the only thing that admits an ``error`` file.

    Both halves are asserted in one test on purpose: the pair is the point. A
    handler that always included error files, and one that never did, each pass one
    half alone. Sweeping error files silently would destroy exactly the evidence
    needed to diagnose the failure that produced them.
    """
    error_file = _make_file(db_session, admin_user, status_value="error", age_days=_ANCIENT_DAYS)

    excluded = client.get(
        f"{BASE}/preview",
        params={"retention_days": _MAX_WINDOW, "delete_error_files": False},
        headers=admin_token_headers,
    )
    assert excluded.status_code == status.HTTP_200_OK
    assert excluded.json()["file_count"] == 0

    included = client.get(
        f"{BASE}/preview",
        params={"retention_days": _MAX_WINDOW, "delete_error_files": True},
        headers=admin_token_headers,
    )
    assert included.status_code == status.HTTP_200_OK
    listed = included.json()
    assert listed["file_count"] == 1
    assert listed["files"][0]["uuid"] == str(error_file.uuid)


def test_preview_deletes_nothing(client, admin_token_headers, admin_user, db_session):
    """ "Dry run" is the whole contract of this route.

    Catches ``_get_retention_eligible_files`` being followed by the real delete
    helper (they live in the same module and take the same arguments) — an operator
    checking the impact of a window would destroy the files they were still
    deciding about.
    """
    doomed = _make_file(db_session, admin_user, status_value="completed", age_days=_ANCIENT_DAYS)

    response = client.get(
        f"{BASE}/preview", params={"retention_days": _MAX_WINDOW}, headers=admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["file_count"] == 1
    survivor = db_session.query(MediaFile).filter(MediaFile.uuid == doomed.uuid).one()
    assert survivor.status == "completed"


def test_preview_ignores_files_inside_the_window(
    client, admin_token_headers, admin_user, db_session
):
    """The control for the eligibility tests: a young file is never listed.

    Without it, an implementation returning every completed file regardless of age
    would pass every assertion above.
    """
    _make_file(db_session, admin_user, status_value="completed", age_days=1)

    response = client.get(
        f"{BASE}/preview", params={"retention_days": _MAX_WINDOW}, headers=admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["file_count"] == 0


# ---------------------------------------------------------------------------
# POST /run
# ---------------------------------------------------------------------------
def test_run_dispatches_a_task_and_deletes_nothing_in_request(
    client, admin_token_headers, admin_user, db_session
):
    """The manual trigger queues work; it must not delete inside the request.

    ``celery_app.send_task`` is patched because it is the one call here that reaches
    a **live broker** — an unpatched dispatch would hand a real worker a forced
    sweep over the dev dataset. What is asserted is the response contract the admin
    panel polls on, plus the fact that the eligible file is still present when the
    response comes back: an inline delete would have removed it.
    """
    doomed = _make_file(db_session, admin_user, status_value="completed", age_days=_ANCIENT_DAYS)
    dispatched = MagicMock(name="AsyncResult")
    dispatched.id = "retention-task-id"

    with patch("app.core.celery.celery_app.send_task", return_value=dispatched) as send_task:
        response = client.post(f"{BASE}/run", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["task_id"] == "retention-task-id"
    assert body["status"] == "queued"
    assert db_session.query(MediaFile).filter(MediaFile.uuid == doomed.uuid).count() == 1
    # force=True is what makes the manual run bypass the schedule window and the
    # retention_enabled flag; without it the button silently does nothing on a
    # deployment that has not enabled the schedule — which is every deployment that
    # would reach for a manual run in the first place.
    assert send_task.call_args.kwargs["kwargs"] == {"force": True}
