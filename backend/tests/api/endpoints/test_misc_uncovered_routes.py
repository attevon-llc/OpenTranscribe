"""Three unrelated routes that ``audit-route-coverage.py`` reported as unreferenced.

Grouped by "nothing else covers them" rather than by subsystem — each is the last
uncovered route in its own router, and none is big enough to earn a file:

* ``POST /api/admin/data-integrity`` — starts an OpenSearch orphan sweep (admin);
* ``POST /api/asr-settings/clear-active`` — revert to the local GPU engine (user);
* ``GET /scim/v2/Schemas/{schema_id}`` — one SCIM schema by URN (bearer token).

**The orphan sweep is never actually started here.** It deletes OpenSearch documents
across the whole deployment; the Celery task is replaced with a recorder, so what is
asserted is the guard (``already_running`` → nothing dispatched) and the attribution
fix from #431 (the sweep used to publish progress to a hardcoded ``user_id=1``, so an
admin who was not account 1 waited forever while whoever held id 1 got the toasts).
Redis is substituted for the same reason ``test_admin_data_integrity_routes.py``
substitutes it: priming the real ``data_integrity_running`` key would make the live
admin panel believe a sweep was under way.

SCIM is deliberately unlike the rest of the API — mounted at **root**, authenticated
by a Bearer SCIM token rather than a session, and answering with SCIM ``Error``
resources instead of FastAPI's default body. All three are pinned below.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import status
from sqlalchemy.orm import Session

from app.models.prompt import UserSetting
from app.schemas.scim import SCHEMA_GROUP
from app.schemas.scim import SCHEMA_USER
from app.services import scim_token_service

DATA_INTEGRITY = "/api/admin/data-integrity"
CLEAR_ACTIVE_ASR = "/api/asr-settings/clear-active"
SCIM_SCHEMAS = "/scim/v2/Schemas"

ACTIVE_ASR_KEY = "active_asr_config_id"


class _RecordingSweep:
    """Stand-in for the orphan-cleanup task: records dispatches, deletes nothing."""

    def __init__(self) -> None:
        self.dispatches: list[dict] = []

    def delay(self, **kwargs) -> SimpleNamespace:
        self.dispatches.append(kwargs)
        return SimpleNamespace(id="stand-in-integrity-id")


class _StandInRedis:
    """The two reads ``get_integrity_status`` performs, over a dict."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self.store.pop(key, None) is not None)


@pytest.fixture
def integrity_seams():
    """Substitute the sweep task and the lock store together.

    Both live in ``app.tasks.opensearch_integrity_task``; the module binds
    ``get_redis`` at import, so the patch target is the module attribute.
    """
    sweep = _RecordingSweep()
    fake_redis = _StandInRedis()
    with (
        patch("app.tasks.opensearch_integrity_task.opensearch_orphan_cleanup_task", sweep),
        patch("app.tasks.opensearch_integrity_task.get_redis", return_value=fake_redis),
    ):
        yield SimpleNamespace(sweep=sweep, redis=fake_redis)


# ---------------------------------------------------------------------------
# POST /api/admin/data-integrity
# ---------------------------------------------------------------------------
def test_starting_a_sweep_attributes_it_to_the_requesting_admin(
    client, admin_token_headers, admin_user, integrity_seams
):
    """``user_id`` must be the caller, not a hardcoded account (#431).

    Progress notifications are published to that id, so getting it wrong means the
    admin who pressed the button never sees the sweep finish while an unrelated
    account is told about work it did not start. The task is a stand-in — a real
    dispatch deletes OpenSearch documents deployment-wide.
    """
    response = client.post(DATA_INTEGRITY, headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "started"
    assert body["task_id"] == "stand-in-integrity-id"
    assert integrity_seams.sweep.dispatches == [{"user_id": admin_user.id}]


def test_a_sweep_already_running_dispatches_nothing(client, admin_token_headers, integrity_seams):
    """The guard: two concurrent orphan cleanups delete each other's candidates.

    Catches the ``already_running`` check being dropped — every panel visit would
    queue another full-index sweep, and the response would look identical apart from
    a task id nobody reads.
    """
    integrity_seams.redis.store["data_integrity_running"] = "1"

    response = client.post(DATA_INTEGRITY, headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "already_running"}
    assert integrity_seams.sweep.dispatches == []


def test_starting_a_sweep_is_refused_for_a_plain_user(client, user_token_headers, integrity_seams):
    response = client.post(DATA_INTEGRITY, headers=user_token_headers)

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert integrity_seams.sweep.dispatches == []


def test_starting_a_sweep_requires_authentication(client, integrity_seams):
    assert client.post(DATA_INTEGRITY).status_code == status.HTTP_401_UNAUTHORIZED
    assert integrity_seams.sweep.dispatches == []


# ---------------------------------------------------------------------------
# POST /api/asr-settings/clear-active
# ---------------------------------------------------------------------------
def _set_active_asr(db_session, user, value: str = "7") -> UserSetting:
    setting = UserSetting(user_id=user.id, setting_key=ACTIVE_ASR_KEY, setting_value=value)
    db_session.add(setting)
    db_session.commit()
    return setting


def _active_asr_rows(db_session: Session, user) -> list[UserSetting]:
    db_session.expire_all()
    return (
        db_session.query(UserSetting)
        .filter(UserSetting.user_id == user.id, UserSetting.setting_key == ACTIVE_ASR_KEY)
        .all()
    )


def test_clearing_the_active_asr_config_deletes_the_setting_row(
    client, db_session, user_token_headers, normal_user
):
    """Reverting to the local engine means the row is GONE, not blanked.

    ``get_active_asr_config`` treats presence as "a cloud provider is selected", so a
    row left behind with an empty value would keep routing transcription at a
    provider the user just turned off.
    """
    _set_active_asr(db_session, normal_user)
    assert _active_asr_rows(db_session, normal_user), "control: the setting was really there"

    response = client.post(CLEAR_ACTIVE_ASR, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"message": "Reverted to local ASR default"}
    assert _active_asr_rows(db_session, normal_user) == []


def test_clearing_when_nothing_is_active_still_succeeds(
    client, db_session, user_token_headers, normal_user
):
    """Idempotent: the UI's "use local engine" button must not 404 on a second press."""
    response = client.post(CLEAR_ACTIVE_ASR, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert _active_asr_rows(db_session, normal_user) == []


def test_clearing_does_not_touch_another_users_selection(
    client, db_session, user_token_headers, normal_user, other_user
):
    """Self-scoped: the delete is filtered by ``user_id``.

    Without the filter one user pressing "use local engine" would silently revert
    every account on the deployment — a 200 either way.
    """
    _set_active_asr(db_session, normal_user, "7")
    _set_active_asr(db_session, other_user, "9")

    response = client.post(CLEAR_ACTIVE_ASR, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert _active_asr_rows(db_session, normal_user) == []
    (survivor,) = _active_asr_rows(db_session, other_user)
    assert survivor.setting_value == "9"


def test_clearing_the_active_asr_config_requires_authentication(client):
    assert client.post(CLEAR_ACTIVE_ASR).status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /scim/v2/Schemas/{schema_id}
# ---------------------------------------------------------------------------
@pytest.fixture
def scim_headers(db_session, admin_user) -> dict[str, str]:
    """A SCIM bearer token, issued the way a super_admin would."""
    _row, plaintext = scim_token_service.issue_token(
        db_session, name="pytest-schemas", created_by=int(admin_user.id)
    )
    return {"Authorization": f"Bearer {plaintext}"}


@pytest.mark.parametrize(
    ("urn", "expected_name"),
    [(SCHEMA_USER, "User"), (SCHEMA_GROUP, "Group")],
)
def test_a_schema_is_served_by_its_full_urn(client, scim_headers, urn, expected_name):
    """An IdP fetches the attribute definition before mapping its directory fields.

    The ``id`` must round-trip exactly and ``meta.location`` must point back at this
    route: a connector follows that URL, and a relative-vs-absolute or short-name
    mismatch there is a provisioning setup that fails with nothing in the UI to
    explain it.
    """
    response = client.get(f"{SCIM_SCHEMAS}/{urn}", headers=scim_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["id"] == urn
    assert body["name"] == expected_name
    assert body["meta"] == {"resourceType": "Schema", "location": f"{SCIM_SCHEMAS}/{urn}"}
    assert [attr["name"] for attr in body["attributes"]], "a schema with no attributes"


def test_a_short_name_is_not_accepted_for_a_schema(client, scim_headers):
    """Matching is literal against the full URN — ``User`` is not ``…:core:2.0:User``.

    Catches a "helpful" suffix match being added: an IdP would then successfully
    fetch a schema at a URN this server never advertises, and the id it caches would
    not be the one ``GET /Schemas`` lists.
    """
    response = client.get(f"{SCIM_SCHEMAS}/User", headers=scim_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_an_unknown_schema_is_a_scim_error_resource(client, scim_headers):
    """SCIM errors are typed resources, not FastAPI's ``{"detail": …}``.

    RFC 7644 §3.12 also makes ``status`` a **string**, and strict connectors
    validate it — a bare integer is rejected by the client, not by us.
    """
    response = client.get(f"{SCIM_SCHEMAS}/urn:acme:not:a:schema", headers=scim_headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    body = response.json()
    assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert body["status"] == "404"


def test_a_schema_read_requires_a_scim_bearer_token(client):
    """Not anonymous, even though RFC 7644 §4 permits it: it discloses topology."""
    response = client.get(f"{SCIM_SCHEMAS}/{SCHEMA_USER}")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_a_browser_session_does_not_authenticate_a_schema_read(client, admin_token_headers):
    """A user JWT must not work here, or a CSRF could drive provisioning reads."""
    response = client.get(f"{SCIM_SCHEMAS}/{SCHEMA_USER}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
