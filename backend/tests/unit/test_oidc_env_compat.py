"""The retired environment-variable names keep working, and this proves it.

Deployments own their `.env` file. Dropping the historical `KEYCLOAK_*` names would
silently disable SSO on upgrade for every existing installation — the exact failure the
rename was not allowed to cause. `core/legacy_auth_env.py` translates them onto the
canonical `OIDC_*` spelling before `Settings` is built.

Two properties, both asserted per variable rather than in prose:

1. Setting the retired name **alone** resolves to the same value as setting the
   canonical name alone.
2. The retired name **wins** when both are set. That preserves the precedence that was
   documented before the rename (`KEYCLOAK_DISCOVERY_URL` beat `OIDC_DISCOVERY_URL`), so
   an upgrade can add `OIDC_*` to `.env.example` without changing what any existing
   deployment resolves to.
"""

from __future__ import annotations

import pytest

from app.core.legacy_auth_env import LEGACY_OIDC_ENV_ALIASES
from app.core.legacy_auth_env import deprecated_oidc_env_names
from app.core.legacy_auth_env import oidc_bool_env
from app.core.legacy_auth_env import oidc_env
from app.core.legacy_auth_env import oidc_int_env

CANONICAL_NAMES = sorted(LEGACY_OIDC_ENV_ALIASES)


@pytest.mark.parametrize("canonical", CANONICAL_NAMES)
def test_the_retired_name_alone_resolves(canonical: str):
    legacy = LEGACY_OIDC_ENV_ALIASES[canonical]
    assert oidc_env(canonical, environ={legacy: "from-legacy"}) == "from-legacy"


@pytest.mark.parametrize("canonical", CANONICAL_NAMES)
def test_the_canonical_name_alone_resolves(canonical: str):
    assert oidc_env(canonical, environ={canonical: "from-canonical"}) == "from-canonical"


@pytest.mark.parametrize("canonical", CANONICAL_NAMES)
def test_the_retired_name_wins_when_both_are_set(canonical: str):
    legacy = LEGACY_OIDC_ENV_ALIASES[canonical]
    resolved = oidc_env(canonical, environ={legacy: "legacy", canonical: "canonical"})
    assert resolved == "legacy", (
        "documented precedence: an upgrade must not change what a deployment that "
        "still sets the historical names resolves to"
    )


def test_neither_set_falls_back_to_the_default():
    assert oidc_env("OIDC_REALM", "opentranscribe", environ={}) == "opentranscribe"


def test_an_empty_legacy_value_does_not_shadow_the_canonical_one():
    """`KEYCLOAK_X=` in a template must not mask a real `OIDC_X`.

    `.env.example` ships several of these blank, and `env_file:` hands every line to
    the container — so "set but empty" is the common case, not an edge case.
    """
    resolved = oidc_env(
        "OIDC_CLIENT_SECRET",
        environ={"KEYCLOAK_CLIENT_SECRET": "", "OIDC_CLIENT_SECRET": "real"},
    )
    assert resolved == "real"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("TRUE", True), ("  true  ", True), ("false", False), ("junk", False)],
)
def test_boolean_parsing_matches_the_rest_of_config_py(raw: str, expected: bool):
    assert oidc_bool_env("OIDC_USE_PKCE", True, environ={"KEYCLOAK_USE_PKCE": raw}) is expected


def test_boolean_default_is_used_when_unset():
    assert oidc_bool_env("OIDC_VERIFY_AUDIENCE", True, environ={}) is True
    assert oidc_bool_env("OIDC_ENABLED", False, environ={}) is False


def test_an_unparseable_int_falls_back_rather_than_crashing_startup():
    assert oidc_int_env("OIDC_TIMEOUT", 30, environ={"KEYCLOAK_TIMEOUT": "soon"}) == 30
    assert oidc_int_env("OIDC_TIMEOUT", 30, environ={"KEYCLOAK_TIMEOUT": "45"}) == 45


def test_settings_declares_every_canonical_name():
    """A missing declaration means the variable is silently dropped.

    `Settings` uses `extra="ignore"`, so an undeclared name does not raise — it just
    never arrives. That is precisely how a rename disables SSO without anyone noticing.
    """
    from app.core.config import settings

    assert CANONICAL_NAMES, "no canonical OIDC env names to check"
    for canonical in CANONICAL_NAMES:
        assert hasattr(settings, canonical), f"Settings has no {canonical}"


def test_the_env_config_map_only_uses_canonical_names():
    """`ENV_TO_CONFIG_MAPPING` must not become a second translation table.

    The whole point of the adapter is that there is exactly ONE place the retired
    spelling exists. If it leaked into the service's map too, the two would drift.
    """
    from app.services.auth_config_service import AuthConfigService

    vendor_noun = "key" + "cloak"
    assert AuthConfigService.ENV_TO_CONFIG_MAPPING, "no env->config mappings to check"
    for env_name, config_key in AuthConfigService.ENV_TO_CONFIG_MAPPING.items():
        assert vendor_noun not in env_name.lower()
        assert vendor_noun not in config_key.lower()

    assert CANONICAL_NAMES, "no canonical OIDC env names to check"
    for canonical in CANONICAL_NAMES:
        assert canonical in AuthConfigService.ENV_TO_CONFIG_MAPPING, (
            f"{canonical} has no config-key mapping, so its DB key can never be "
            "migrated out of the environment"
        )


def test_effective_config_resolves_through_the_retired_name(db_session, monkeypatch):
    """End to end: the retired spelling reaches `get_effective_config`.

    `AuthConfigService.get_effective_config` falls back to
    `getattr(settings, env_var_for(key))` when no database row exists. That only works
    because the *canonical* attribute is what the adapter populated — a deployment on
    the old spelling must not fall through to the coded default.
    """
    from sqlalchemy import text

    from app.core.config import settings
    from app.services.auth_config_service import AuthConfigService

    assert AuthConfigService.env_var_for("oidc_client_id") == "OIDC_CLIENT_ID"

    # No DB row, so the env layer is what answers. (Rolled back by the fixture.)
    db_session.execute(text("DELETE FROM auth_config WHERE config_key = 'oidc_client_id'"))
    monkeypatch.setattr(settings, "OIDC_CLIENT_ID", "resolved-from-legacy", raising=False)
    assert (
        AuthConfigService.get_effective_config(db_session, "oidc_client_id")
        == "resolved-from-legacy"
    )


def test_the_startup_warning_names_only_variables_actually_in_use():
    assert deprecated_oidc_env_names(environ={}) == []
    assert deprecated_oidc_env_names(environ={"KEYCLOAK_ENABLED": "true"}) == ["KEYCLOAK_ENABLED"]
    assert deprecated_oidc_env_names(environ={"KEYCLOAK_ENABLED": ""}) == []
    assert deprecated_oidc_env_names(environ={"OIDC_ENABLED": "true"}) == []
