"""``services/asr_provider_policy``: storage, fail-closed reads and role rules (issue #1246)."""

from __future__ import annotations

import copy

import pytest
from fastapi import HTTPException

from app.core.constants import ASR_CLOUD_PROVIDERS_FOR_USERS_KEY
from app.core.constants import ASR_USERS_OWN_KEYS_KEY
from app.services import asr_provider_policy as policy
from app.services import system_settings_service
from app.services.asr.factory import ASR_PROVIDER_CATALOG

pytestmark = pytest.mark.xdist_group("asr_provider_policy_system_settings")


def test_nothing_stored_allows_every_provider_and_own_keys(db_session):
    loaded = policy.load_policy(db_session)
    assert loaded.allowed is None
    assert loaded.own_keys is True


def test_save_then_load_round_trips_in_catalog_order(db_session):
    saved = policy.save_policy(db_session, ["gladia", "deepgram"], own_keys=False)
    assert saved == policy.load_policy(db_session)
    assert saved.allowed == frozenset({"deepgram", "gladia"})
    assert saved.own_keys is False


@pytest.mark.parametrize("garbage", ["not json", '{"a": 1}', "[1, 2]", '"deepgram"'])
def test_an_unreadable_list_fails_closed_not_open(db_session, garbage):
    system_settings_service.set_setting(db_session, ASR_CLOUD_PROVIDERS_FOR_USERS_KEY, garbage)
    loaded = policy.load_policy(db_session)
    assert loaded.allowed == frozenset()
    assert policy.provider_allowed("user", "deepgram", loaded) is False
    assert policy.provider_allowed("user", "local", loaded) is True


def test_null_means_all_and_an_unreadable_own_keys_flag_keeps_the_default(db_session):
    system_settings_service.set_setting(db_session, ASR_CLOUD_PROVIDERS_FOR_USERS_KEY, "null")
    loaded = policy.load_policy(db_session)
    assert loaded.allowed is None
    system_settings_service.set_setting(db_session, ASR_USERS_OWN_KEYS_KEY, "false")
    assert policy.load_policy(db_session).own_keys is False


def test_save_rejects_unknown_and_local_ids_and_stores_nothing(db_session):
    for bad in (["nope"], ["local"]):
        with pytest.raises(ValueError, match="unknown provider"):
            policy.save_policy(db_session, bad, own_keys=True)
    assert policy.load_policy(db_session).allowed is None


@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_elevated_roles_are_never_bound(role):
    closed = policy.ProviderPolicy(allowed=frozenset(), own_keys=False)
    assert policy.provider_allowed(role, "deepgram", closed) is True
    assert policy.may_store_own_key(role, closed) is True


def test_a_plain_user_is_bound():
    closed = policy.ProviderPolicy(allowed=frozenset({"gladia"}), own_keys=False)
    assert policy.provider_allowed("user", "gladia", closed) is True
    assert policy.provider_allowed("user", "deepgram", closed) is False
    assert policy.provider_allowed(None, "deepgram", closed) is False
    assert policy.may_store_own_key("user", closed) is False


def test_visible_providers_copies_instead_of_mutating_the_catalog():
    before = copy.deepcopy(ASR_PROVIDER_CATALOG)
    rules = policy.ProviderPolicy(allowed=frozenset({"deepgram"}), own_keys=True)
    seen = policy.visible_providers(list(ASR_PROVIDER_CATALOG.values()), "admin", rules)
    assert {p["id"]: p["allowed_for_users"] for p in seen}["deepgram"] is True
    assert before == ASR_PROVIDER_CATALOG
    assert all("allowed_for_users" not in p for p in ASR_PROVIDER_CATALOG.values())


class _User:
    def __init__(self, role: str) -> None:
        self.role = role


def test_refuse_config_change_has_fixed_sentences_and_skips_local(db_session):
    policy.save_policy(db_session, ["gladia"], own_keys=False)
    policy.refuse_config_change(db_session, _User("user"), "local", carries_credential=True)
    policy.refuse_config_change(db_session, _User("admin"), "deepgram", carries_credential=True)
    with pytest.raises(HTTPException) as disallowed:
        policy.refuse_config_change(db_session, _User("user"), "deepgram", carries_credential=False)
    assert disallowed.value.status_code == 409
    assert disallowed.value.detail["code"] == "asr_provider_not_allowed"
    with pytest.raises(HTTPException) as own_keys:
        policy.refuse_config_change(db_session, _User("user"), "gladia", carries_credential=True)
    assert own_keys.value.detail == {
        "code": "own_asr_keys_disabled",
        "message": policy.OWN_KEYS_DISABLED_MESSAGE,
    }
    # No key supplied (a rename / re-point at an allowed provider): not an own-key event.
    policy.refuse_config_change(db_session, _User("user"), "gladia", carries_credential=False)
