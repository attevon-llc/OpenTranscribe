"""Functional tests for three super_admin operator routes with no coverage.

``GET /api/admin/first-run-wizard/status`` · ``POST .../complete``
(``first_run_wizard.py``) · ``POST /api/admin/redaction-policy/reindex``
(``redaction_settings.py``) · ``DELETE /api/admin/scim-tokens/{token_uuid}``
(``admin_scim_tokens.py``).

None of the four had a test issue a request. ``api/test_scim.py`` covers issuing
and listing SCIM tokens but stops before revocation — the only control that takes
a leaked provisioning credential (one that can create and disable accounts across
the whole deployment) out of service.

Nothing destructive runs here. ``/reindex`` dispatches a Celery task and the
autouse ``_skip_celery_dispatch`` fixture no-ops ``apply_async``, so no worker ever
re-detects redaction spans over the dev corpus; the wizard and token routes only
touch rows inside the test's savepoint.
"""

from __future__ import annotations

import uuid as uuid_pkg
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import status

from app.models.system_settings import SystemSettings

WIZARD = "/api/admin/first-run-wizard"
REINDEX = "/api/admin/redaction-policy/reindex"
TOKENS = "/api/admin/scim-tokens"

WIZARD_KEY = "first_run_wizard.completed_at"


@pytest.fixture
def wizard_not_completed(db_session):
    """Clear the completion stamp so the "never run" shape is observable.

    Rolled back with the savepoint, so a deployment that really has completed the
    wizard is not made to offer it again.
    """
    db_session.query(SystemSettings).filter(SystemSettings.key == WIZARD_KEY).delete(
        synchronize_session=False
    )
    db_session.commit()
    return db_session


@pytest.fixture
def issued_token(client, super_admin_token_headers) -> dict:
    """A freshly issued SCIM token row (the DB row rolls back with the savepoint)."""
    created = client.post(
        TOKENS,
        headers=super_admin_token_headers,
        json={"name": f"revoke-probe-{uuid_pkg.uuid4().hex[:8]}"},
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text
    row: dict = created.json()
    return row


# ---------------------------------------------------------------------------
# Privilege tier — all four are super_admin
# ---------------------------------------------------------------------------
#: A FIXED uuid, never `uuid4()`. A parametrize argument is evaluated at import time and
#: becomes part of the test ID, and under `-n auto` every xdist worker imports this module
#: separately — so a random value gives each worker a different ID, xdist reports
#: "Different tests were collected between gw1 and gw0", and **the entire suite fails
#: collection**, not just this file. It passes when the file is run alone, which is exactly
#: why it reached the shared suite. Any UUID works here: the assertion is that a plain admin
#: is refused before the handler ever looks the token up.
_ABSENT_TOKEN_UUID = "00000000-0000-4000-8000-000000000000"

_ROUTES = [
    ("GET", f"{WIZARD}/status"),
    ("POST", f"{WIZARD}/complete"),
    ("POST", REINDEX),
    ("DELETE", f"{TOKENS}/{_ABSENT_TOKEN_UUID}"),
]


@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_a_plain_admin_is_refused_on_every_route(client, admin_token_headers, method, path):
    """These are deployment-infrastructure verbs, not user administration.

    The SCIM revocation is the sharpest of the four: the tier that may **issue** a
    provisioning credential and the tier that may retire one must be the same, or a
    plain admin could revoke the connector's token and stop all account
    provisioning deployment-wide.
    """
    response = client.request(method, path, headers=admin_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_a_plain_user_is_refused_on_every_route(client, user_token_headers, method, path):
    response = client.request(method, path, headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_every_route_requires_authentication(client, method, path):
    response = client.request(method, path)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# First-run wizard
# ---------------------------------------------------------------------------
def test_wizard_status_is_null_before_completion(
    client, super_admin_token_headers, wizard_not_completed
):
    """``completed_at`` absent means "offer the wizard".

    Presence, not a bool, is the signal — so the failure mode this catches is a
    handler returning an empty string or the literal ``"None"``, both of which the
    SPA would read as truthy and never show the guided flow to a new operator.
    """
    response = client.get(f"{WIZARD}/status", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["completed_at"] is None


def test_completing_the_wizard_persists_and_is_read_back(
    client, super_admin_token_headers, wizard_not_completed
):
    """POST stamps a timestamp and GET returns the same one.

    Catches a POST that returns a timestamp without writing it: the wizard would
    reappear on every login, which is precisely the behaviour the endpoint exists
    to prevent.
    """
    completed = client.post(f"{WIZARD}/complete", headers=super_admin_token_headers)
    assert completed.status_code == status.HTTP_200_OK
    stamp = completed.json()["completed_at"]
    assert stamp is not None

    reread = client.get(f"{WIZARD}/status", headers=super_admin_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["completed_at"] == stamp


def _status(client, headers) -> dict:
    response = client.get(f"{WIZARD}/status", headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    body: dict = response.json()
    return body


def test_wizard_status_reports_operator_set_for_a_chosen_password(
    client, super_admin_token_headers, super_admin_user
):
    """The fixture's password is not the dev credential and is not forced to change."""
    body = _status(client, super_admin_token_headers)
    assert body["credential_kind"] == "operator_set"


def test_wizard_status_flags_the_well_known_dev_password(
    client, super_admin_token_headers, super_admin_user, db_session
):
    """A super_admin still on ``password`` is the case the warning card exists for.

    Catches a classifier that keys on the account's email rather than on the stored
    hash: renaming the account away from admin@example.com must not hide it.
    """
    from app.core.security import get_password_hash
    from app.initial_data import _DEV_ADMIN_PASSWORD

    super_admin_user.hashed_password = get_password_hash(_DEV_ADMIN_PASSWORD)
    db_session.commit()
    assert _status(client, super_admin_token_headers)["credential_kind"] == "default_dev"


def test_wizard_status_is_unreachable_while_a_password_change_is_forced(
    client, super_admin_token_headers, super_admin_user, db_session
):
    """The lifecycle gate 403s a must-change account before the handler runs.

    So ``generated`` is never observable over HTTP today; the SPA shows the
    forced-change screen first. Pinned so that a gate change which lets this route
    through is noticed and the ``generated`` branch gets an HTTP-level test.
    """
    super_admin_user.must_change_password = True
    db_session.commit()
    response = client.get(f"{WIZARD}/status", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_credential_kind_is_generated_for_a_forced_change_and_dev_wins_over_it(
    super_admin_user,
):
    """Classifier branches, called directly because the HTTP gate hides one of them."""
    from app.api.endpoints.first_run_wizard import _credential_kind
    from app.core.security import get_password_hash
    from app.initial_data import _DEV_ADMIN_PASSWORD

    super_admin_user.must_change_password = True
    assert _credential_kind(super_admin_user) == "generated"

    super_admin_user.hashed_password = get_password_hash(_DEV_ADMIN_PASSWORD)
    assert _credential_kind(super_admin_user) == "default_dev"


def test_wizard_status_password_changed_by_you_follows_the_self_change_stamp(
    client, super_admin_token_headers, super_admin_user, db_session
):
    """False until the holder has chosen their own password; true once the stamp is set."""
    from datetime import UTC
    from datetime import datetime

    super_admin_user.password_last_self_changed_at = None
    db_session.commit()
    assert _status(client, super_admin_token_headers)["password_changed_by_you"] is False

    super_admin_user.password_last_self_changed_at = datetime.now(UTC)
    db_session.commit()
    assert _status(client, super_admin_token_headers)["password_changed_by_you"] is True


def test_wizard_status_never_returns_a_password_or_hash(
    client, super_admin_token_headers, super_admin_user
):
    """The payload is an allowlist of four keys; no credential material rides along."""
    body = _status(client, super_admin_token_headers)
    assert set(body) == {
        "completed_at",
        "credential_kind",
        "password_min_age_hours",
        "password_changed_by_you",
    }
    assert super_admin_user.hashed_password not in str(body)
    assert isinstance(body["password_min_age_hours"], int)


def test_completing_twice_re_stamps_rather_than_erroring(
    client, super_admin_token_headers, wizard_not_completed
):
    """Idempotent by design — re-runnable from Settings by someone who skipped.

    Catches a uniqueness guard being added on the ``SystemSettings`` key: the
    second completion would be a 409 or a 500, and the operator who re-ran the
    wizard deliberately would see an error for a flow that succeeded.
    """
    first = client.post(f"{WIZARD}/complete", headers=super_admin_token_headers)
    assert first.status_code == status.HTTP_200_OK
    second = client.post(f"{WIZARD}/complete", headers=super_admin_token_headers)
    assert second.status_code == status.HTTP_200_OK
    assert second.json()["completed_at"] >= first.json()["completed_at"]


# ---------------------------------------------------------------------------
# Redaction reindex
# ---------------------------------------------------------------------------
def test_reindex_defaults_to_the_stale_only_sweep(client, super_admin_token_headers):
    """A bare POST dispatches the cheap sweep and reports that scope.

    The default matters: ``only_stale=False`` re-detects every completed file in
    the deployment, a full pass over the corpus on the redaction worker. A default
    flipped to ``False`` turns a routine click into a whole-corpus rebuild.
    """
    from app.tasks import redaction_task

    with patch.object(redaction_task.redaction_reindex_all_task, "delay") as delay:
        delay.return_value = MagicMock(id="test-task-id")
        response = client.post(REINDEX, headers=super_admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "dispatched", "only_stale": True}
    assert delay.call_args.kwargs["only_stale"] is True


def test_reindex_full_scope_is_requestable_and_reaches_the_task(client, super_admin_token_headers):
    """``only_stale=false`` is honoured in the response **and** in the dispatch.

    Catches the query parameter being read for the response but not passed on —
    the operator would be told a full reindex started while only stale files were
    queued, so a detector-model upgrade would never reach the already-detected
    corpus it was meant to refresh.
    """
    from app.tasks import redaction_task

    with patch.object(redaction_task.redaction_reindex_all_task, "delay") as delay:
        delay.return_value = MagicMock(id="test-task-id")
        response = client.post(
            REINDEX, params={"only_stale": False}, headers=super_admin_token_headers
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "dispatched", "only_stale": False}
    assert delay.call_args.kwargs["only_stale"] is False


# ---------------------------------------------------------------------------
# SCIM token revocation
# ---------------------------------------------------------------------------
def test_revoking_a_token_stamps_it_and_the_listing_agrees(
    client, super_admin_token_headers, issued_token
):
    """Revocation is visible in the row that authenticates the connector.

    Catches a DELETE that answers 200 without writing ``revoked_at``: the operator
    would believe a leaked provisioning credential was retired while it kept
    creating and disabling accounts. The listing re-read is the half that proves
    persistence — the handler returns its own in-memory view.
    """
    response = client.delete(f"{TOKENS}/{issued_token['uuid']}", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["uuid"] == issued_token["uuid"]
    assert body["revoked_at"] is not None
    assert "token" not in body

    listed = client.get(TOKENS, headers=super_admin_token_headers)
    assert listed.status_code == status.HTTP_200_OK
    row = next(r for r in listed.json() if r["uuid"] == issued_token["uuid"])
    assert row["revoked_at"] == body["revoked_at"]


def test_revoking_twice_keeps_the_original_timestamp(
    client, super_admin_token_headers, issued_token
):
    """Idempotent, and never un-revokes.

    Catches ``revoke_token``'s ``if row.revoked_at is None`` guard being dropped: a
    second call would re-stamp the row, which rewrites the forensic record of when
    the credential was actually taken out of service.
    """
    first = client.delete(f"{TOKENS}/{issued_token['uuid']}", headers=super_admin_token_headers)
    assert first.status_code == status.HTTP_200_OK
    second = client.delete(f"{TOKENS}/{issued_token['uuid']}", headers=super_admin_token_headers)
    assert second.status_code == status.HTTP_200_OK
    assert second.json()["revoked_at"] == first.json()["revoked_at"]


def test_revoking_an_unknown_token_is_a_404(client, super_admin_token_headers):
    """A UUID that names no token is 404, not a silent success.

    A 200 here would let a typo'd revocation read as done, leaving the real token
    live — the single worst outcome this endpoint can produce.
    """
    response = client.delete(f"{TOKENS}/{uuid_pkg.uuid4()}", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "SCIM token not found"


def test_revoking_a_malformed_uuid_is_a_400_not_a_500(client, super_admin_token_headers):
    """An unparseable path segment is an operator typo, not a server fault.

    This test found a real defect: the value went straight into a Postgres UUID
    comparison, so the request died as an unhandled ``DataError``. That is a 500
    whose aborted transaction also poisons the rest of the request's session — and
    it is indistinguishable, from the operator's side, from the backend being down
    while a credential they believe they revoked is still live.
    """
    response = client.delete(f"{TOKENS}/not-a-uuid", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Invalid UUID format: not-a-uuid"
