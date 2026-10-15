"""Which cloud ASR providers users may choose, and who holds the credentials (issue #1246 WP6).

``/api/admin/asr-providers`` (super_admin) stores two deployment-wide settings: the list of
cloud providers a plain user may send audio to (absent = all, today's behaviour) and whether
users may add keys of their own. The enforcement is the pipeline plan's, so the same rule
refuses dispatch, set-active and config creation. Credentials are admin-shared
``UserASRSettings`` rows and are never returned.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import status

from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.system_settings import SystemSettings
from app.models.user_asr_settings import UserASRSettings
from app.services.pipeline_plan import ISSUE_MESSAGES

URL = "/api/admin/asr-providers"
_ASR = "/api/asr-settings"
#: Generated, not a literal: the tests assert that a saved key never comes back, and a fixed
#: key-shaped string in the tree is what secret scanners are written to flag.
SECRET = "k-" + uuid_pkg.uuid4().hex

pytestmark = pytest.mark.usefixtures("pipeline_env")


@pytest.fixture
def audit_events(monkeypatch):
    from app.api.endpoints import asr_provider_policy

    captured: list[dict] = []
    monkeypatch.setattr(asr_provider_policy.audit_logger, "log", lambda **kw: captured.append(kw))
    return captured


def _put(client, headers, allowed, own_keys=True):
    return client.put(
        URL,
        json={"allowed_for_users": allowed, "users_may_add_own_keys": own_keys},
        headers=headers,
    )


def _ids(response) -> list[str]:
    return [p["id"] for p in response.json()["providers"]]


def assert_refused(response, code: str) -> None:
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert isinstance(detail["message"], str)
    assert detail["message"]


def _make_config(client, headers, provider="assemblyai", model="universal", **extra):
    body = {
        "name": f"{provider}-{uuid_pkg.uuid4().hex[:6]}",
        "provider": provider,
        "model_name": model,
        **extra,
    }
    return client.post(_ASR, json=body, headers=headers)


class TestWhoMayCall:
    @pytest.mark.parametrize("method", ["get", "put"])
    def test_anonymous_is_401(self, client, method):
        kwargs = {"json": {"allowed_for_users": None, "users_may_add_own_keys": True}}
        response = getattr(client, method)(URL, **(kwargs if method == "put" else {}))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.parametrize("who", ["user_token_headers", "admin_token_headers"])
    @pytest.mark.parametrize("method", ["get", "put"])
    def test_below_super_admin_is_403(self, client, request, who, method):
        headers = request.getfixturevalue(who)
        kwargs = {"json": {"allowed_for_users": ["deepgram"], "users_may_add_own_keys": True}}
        response = getattr(client, method)(
            URL, headers=headers, **(kwargs if method == "put" else {})
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_refused_put_changes_nothing(self, client, admin_token_headers, db_session):
        client.put(
            URL,
            json={"allowed_for_users": ["deepgram"], "users_may_add_own_keys": False},
            headers=admin_token_headers,
        )
        stored = db_session.query(SystemSettings).filter(SystemSettings.key.like("asr.%")).count()
        assert stored == 0

    def test_super_admin_reads_and_writes(self, client, super_admin_token_headers):
        assert client.get(URL, headers=super_admin_token_headers).status_code == 200
        assert _put(client, super_admin_token_headers, ["deepgram"]).status_code == 200


class TestPolicyDocument:
    def test_absent_policy_allows_every_cloud_provider(self, client, super_admin_token_headers):
        body = client.get(URL, headers=super_admin_token_headers).json()
        assert body["users_may_add_own_keys"] is True
        assert len(body["providers"]) >= 8
        assert "local" not in _ids(client.get(URL, headers=super_admin_token_headers))
        assert all(p["allowed_for_users"] is True for p in body["providers"])

    def test_put_round_trips_and_flags_only_the_chosen(self, client, super_admin_token_headers):
        response = _put(client, super_admin_token_headers, ["deepgram", "gladia"], own_keys=False)
        assert response.status_code == 200
        body = client.get(URL, headers=super_admin_token_headers).json()
        assert body["users_may_add_own_keys"] is False
        flags = {p["id"]: p["allowed_for_users"] for p in body["providers"]}
        assert {k for k, v in flags.items() if v} == {"deepgram", "gladia"}
        assert response.json() == body

    def test_null_restores_all_allowed(self, client, super_admin_token_headers):
        _put(client, super_admin_token_headers, ["deepgram"])
        _put(client, super_admin_token_headers, None)
        body = client.get(URL, headers=super_admin_token_headers).json()
        assert all(p["allowed_for_users"] for p in body["providers"])

    def test_an_empty_list_allows_no_cloud_provider(self, client, super_admin_token_headers):
        _put(client, super_admin_token_headers, [])
        body = client.get(URL, headers=super_admin_token_headers).json()
        assert not any(p["allowed_for_users"] for p in body["providers"])

    @pytest.mark.parametrize("bad", [["nope"], ["local"], ["deepgram", "nope"]])
    def test_unknown_or_local_id_is_422_and_changes_nothing(
        self, client, super_admin_token_headers, bad
    ):
        _put(client, super_admin_token_headers, ["deepgram"])
        response = _put(client, super_admin_token_headers, bad)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        after = client.get(URL, headers=super_admin_token_headers).json()
        assert {p["id"] for p in after["providers"] if p["allowed_for_users"]} == {"deepgram"}

    def test_the_update_is_audited_with_ids_and_no_secret(
        self, client, super_admin_token_headers, audit_events
    ):
        _put(client, super_admin_token_headers, ["deepgram"], own_keys=False)
        [event] = audit_events
        assert event["details"]["setting"] == "asr_provider_policy"
        assert event["details"]["action"] == "update"
        assert event["details"]["allowed_for_users"] == ["deepgram"]


class TestUsersSeeOnlyWhatIsAllowed:
    def test_without_a_policy_nothing_changes(
        self, client, user_token_headers, admin_token_headers
    ):
        user = _ids(client.get(f"{_ASR}/providers", headers=user_token_headers))
        admin = _ids(client.get(f"{_ASR}/providers", headers=admin_token_headers))
        assert user == admin
        assert {"local", "deepgram", "assemblyai", "gladia"} <= set(user)

    def test_a_user_gets_local_plus_the_allowed_providers(
        self, client, super_admin_token_headers, user_token_headers
    ):
        _put(client, super_admin_token_headers, ["deepgram"])
        user = client.get(f"{_ASR}/providers", headers=user_token_headers)
        assert _ids(user) == ["local", "deepgram"]

    def test_an_admin_still_sees_everything_with_the_flag(
        self, client, super_admin_token_headers, admin_token_headers
    ):
        _put(client, super_admin_token_headers, ["deepgram"])
        providers = client.get(f"{_ASR}/providers", headers=admin_token_headers).json()["providers"]
        flags = {p["id"]: p.get("allowed_for_users") for p in providers}
        assert flags["deepgram"] is True
        assert flags["assemblyai"] is False
        assert len(providers) > 2


class TestCreatingConfigs:
    def test_a_user_cannot_create_a_disallowed_provider(
        self, client, super_admin_token_headers, user_token_headers
    ):
        _put(client, super_admin_token_headers, ["deepgram"])
        response = _make_config(client, user_token_headers, "assemblyai", "universal")
        assert_refused(response, "asr_provider_not_allowed")
        assert response.json()["detail"]["message"] == ISSUE_MESSAGES["asr_provider_not_allowed"]

    def test_a_user_can_create_an_allowed_provider(
        self, client, super_admin_token_headers, user_token_headers
    ):
        _put(client, super_admin_token_headers, ["deepgram"])
        response = _make_config(client, user_token_headers, "deepgram", "nova-3")
        assert response.status_code == status.HTTP_201_CREATED, response.text

    def test_an_admin_can_create_a_disallowed_provider(
        self, client, super_admin_token_headers, admin_token_headers
    ):
        _put(client, super_admin_token_headers, ["deepgram"])
        response = _make_config(client, admin_token_headers, "assemblyai", "universal")
        assert response.status_code == status.HTTP_201_CREATED, response.text

    def test_a_user_cannot_move_a_config_onto_a_disallowed_provider(
        self, client, super_admin_token_headers, user_token_headers
    ):
        created = _make_config(client, user_token_headers, "deepgram", "nova-3").json()
        _put(client, super_admin_token_headers, ["gladia"])
        response = client.put(
            f"{_ASR}/config/{created['uuid']}",
            json={"provider": "assemblyai"},
            headers=user_token_headers,
        )
        assert_refused(response, "asr_provider_not_allowed")

    def test_renaming_a_config_does_not_need_the_provider_to_be_allowed(
        self, client, super_admin_token_headers, user_token_headers
    ):
        created = _make_config(client, user_token_headers, "deepgram", "nova-3").json()
        _put(client, super_admin_token_headers, ["gladia"])
        response = client.put(
            f"{_ASR}/config/{created['uuid']}",
            json={"name": f"renamed-{uuid_pkg.uuid4().hex[:6]}"},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text

    def test_own_keys_off_refuses_a_user_cloud_config(
        self, client, super_admin_token_headers, user_token_headers
    ):
        _put(client, super_admin_token_headers, None, own_keys=False)
        assert_refused(
            _make_config(client, user_token_headers, "deepgram", "nova-3"),
            "own_asr_keys_disabled",
        )

    def test_own_keys_off_refuses_a_user_key_on_an_existing_config(
        self, client, super_admin_token_headers, user_token_headers
    ):
        created = _make_config(client, user_token_headers, "deepgram", "nova-3").json()
        _put(client, super_admin_token_headers, None, own_keys=False)
        response = client.put(
            f"{_ASR}/config/{created['uuid']}",
            json={"api_key": SECRET},
            headers=user_token_headers,
        )
        assert_refused(response, "own_asr_keys_disabled")

    def test_own_keys_off_still_lets_an_admin_create(
        self, client, super_admin_token_headers, admin_token_headers
    ):
        _put(client, super_admin_token_headers, None, own_keys=False)
        response = _make_config(client, admin_token_headers, "deepgram", "nova-3")
        assert response.status_code == status.HTTP_201_CREATED, response.text


class TestEnforcementThroughThePlan:
    def test_set_active_of_a_shared_disallowed_config_is_refused(
        self,
        client,
        super_admin_token_headers,
        user_token_headers,
        admin_user,
        add_asr_config,
        db_session,
    ):
        shared = add_asr_config("assemblyai", "universal", owner=admin_user, shared=True)
        _put(client, super_admin_token_headers, ["deepgram"])
        response = client.post(
            f"{_ASR}/set-active", json={"config_uuid": str(shared.uuid)}, headers=user_token_headers
        )
        assert_refused(response, "asr_provider_not_allowed")
        assert response.json()["detail"]["message"] == ISSUE_MESSAGES["asr_provider_not_allowed"]

    def test_set_active_of_a_shared_allowed_config_still_works_with_own_keys_off(
        self,
        client,
        super_admin_token_headers,
        user_token_headers,
        admin_user,
        add_asr_config,
    ):
        shared = add_asr_config("deepgram", "nova-3", owner=admin_user, shared=True)
        _put(client, super_admin_token_headers, ["deepgram"], own_keys=False)
        response = client.post(
            f"{_ASR}/set-active", json={"config_uuid": str(shared.uuid)}, headers=user_token_headers
        )
        assert response.status_code == 200, response.text

    def test_an_admin_may_activate_a_disallowed_provider(
        self, client, super_admin_token_headers, admin_token_headers, admin_user, add_asr_config
    ):
        own = add_asr_config("assemblyai", "universal", owner=admin_user)
        _put(client, super_admin_token_headers, ["deepgram"])
        response = client.post(
            f"{_ASR}/set-active", json={"config_uuid": str(own.uuid)}, headers=admin_token_headers
        )
        assert response.status_code == 200, response.text

    def test_dispatch_refuses_a_file_whose_active_config_is_now_disallowed(
        self, db_session, normal_user, add_asr_config
    ):
        from app.core.exceptions import PipelinePlanRefusedError
        from app.models.media import FileStatus
        from app.models.media import MediaFile
        from app.models.media import Task
        from app.services import asr_provider_policy as policy
        from app.tasks.transcription.dispatch import _plan_for_dispatch

        add_asr_config("assemblyai", "universal", active=True)
        media = MediaFile(
            uuid=str(uuid_pkg.uuid4()),
            filename="policy.wav",
            title="policy",
            storage_path="media/test/policy.wav",
            content_type="audio/wav",
            file_size=1024,
            status=FileStatus.PENDING,
            is_public=False,
            user_id=normal_user.id,
        )
        db_session.add(media)
        db_session.commit()

        # Control: with no policy the same file plans cleanly.
        plan = _plan_for_dispatch(db_session, media, whisper_model=None, skip_diarization=False)
        assert plan.transcription.engine == "cloud"

        policy.save_policy(db_session, ["deepgram"], True)
        with pytest.raises(PipelinePlanRefusedError) as refused:
            _plan_for_dispatch(db_session, media, whisper_model=None, skip_diarization=False)
        assert refused.value.code == "asr_provider_not_allowed"
        db_session.refresh(media)
        assert media.status == FileStatus.ERROR
        assert media.last_error_message == ISSUE_MESSAGES["asr_provider_not_allowed"]
        assert db_session.query(Task).filter(Task.media_file_id == media.id).first() is None

    def test_the_environment_default_provider_is_never_blocked(
        self, db_session, normal_user, monkeypatch
    ):
        from app.services import asr_provider_policy as policy
        from app.services.pipeline_plan_inputs import plan_for_user

        policy.save_policy(db_session, [], False)
        monkeypatch.setenv("ASR_PROVIDER", "deepgram")
        plan = plan_for_user(db_session, normal_user.id)
        assert not [i for i in plan.blocking() if i.code == "asr_provider_not_allowed"]


class TestCredentialsAreWriteOnly:
    def test_the_key_reveal_route_is_gone(self, client, user_token_headers):
        created = _make_config(
            client, user_token_headers, "deepgram", "nova-3", api_key=SECRET
        ).json()
        response = client.get(
            f"{_ASR}/config/{created['uuid']}/api-key", headers=user_token_headers
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert SECRET not in response.text

    def test_no_route_under_asr_settings_returns_a_key(self):
        from app.main import app

        paths = [getattr(r, "path", "") for r in app.routes]
        assert not [p for p in paths if p.startswith("/api/asr-settings") and "api-key" in p]

    def test_shared_credentials_are_listed_as_flags_never_values(
        self, client, super_admin_token_headers, admin_token_headers, admin_user
    ):
        created = _make_config(
            client,
            admin_token_headers,
            "deepgram",
            "nova-3",
            api_key=SECRET,
            is_shared=True,
        )
        assert created.status_code == 201, created.text
        unshared = _make_config(client, admin_token_headers, "gladia", "standard", api_key=SECRET)
        assert unshared.status_code == 201, unshared.text

        response = client.get(URL, headers=super_admin_token_headers)
        assert SECRET not in response.text
        by_id = {p["id"]: p for p in response.json()["providers"]}
        [shared] = by_id["deepgram"]["shared_credentials"]
        assert shared["config_uuid"] == created.json()["uuid"]
        assert shared["has_api_key"] is True
        assert shared["owner_name"] == admin_user.full_name
        assert set(shared) == {
            "config_uuid",
            "name",
            "has_api_key",
            "has_access_key_id",
            "owner_name",
        }
        assert by_id["gladia"]["shared_credentials"] == []


class TestSharedCredentialsPerOrganisation:
    @staticmethod
    def _org(db, label, *members):
        org = Organization(
            external_org_id=f"org_{label}_{uuid_pkg.uuid4().hex[:8]}",
            name=f"{label} Org",
            is_active=True,
        )
        db.add(org)
        db.commit()
        db.refresh(org)
        for user in members:
            db.add(
                OrganizationMembership(organization_id=org.id, user_id=user.id, role="org:member")
            )
        db.commit()
        return org

    def test_only_the_active_organisations_shared_credentials_are_listed(
        self,
        client,
        db_session,
        org_context,
        super_admin_user,
        super_admin_token_headers,
        admin_user,
        add_asr_config,
    ):
        from app.core.security import get_password_hash
        from app.models import User

        other_admin = User(
            email=f"otheradmin_{uuid_pkg.uuid4().hex[:8]}@example.com",
            full_name="Other Org Admin",
            hashed_password=get_password_hash("x"),
            is_active=True,
            is_superuser=False,
            role="admin",
        )
        db_session.add(other_admin)
        db_session.commit()
        org_a = self._org(db_session, "a", super_admin_user, admin_user)
        self._org(db_session, "b", other_admin)
        mine = add_asr_config("deepgram", "nova-3", owner=admin_user, shared=True)
        add_asr_config("deepgram", "nova-2", owner=other_admin, shared=True)

        org_context(org_id=org_a.id, org_role="org:admin", only_for=super_admin_user.id)
        body = client.get(URL, headers=super_admin_token_headers).json()
        [deepgram] = [p for p in body["providers"] if p["id"] == "deepgram"]
        assert [c["config_uuid"] for c in deepgram["shared_credentials"]] == [str(mine.uuid)]
        assert db_session.query(UserASRSettings).filter_by(provider="deepgram").count() == 2
