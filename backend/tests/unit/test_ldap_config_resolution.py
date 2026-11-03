"""How LDAP configuration is resolved: the ``LDAP_*`` settings, ``from_env``, ``from_db``.

Every login and every directory probe starts from one immutable ``LdapConfig``. Nothing pinned
how it is built: that each ``LDAP_*`` setting lands in the field of the same meaning, how the
environment is parsed, which ``auth_config`` keys the DB path asks for, how stored values are
coerced, and that a failing DB read falls back to the environment instead of refusing every
login. Written against the public contract (the ``Settings`` class, ``LdapConfig`` and its two
constructors, ``ldap_authenticate``), so it holds for any implementation of that contract.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

import pytest

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import ldap_authenticate
from app.core import config as core_config
from tests.fixtures.ldap_directory_fake import SEARCH_BASE
from tests.fixtures.ldap_directory_fake import SERVICE_CREDENTIAL
from tests.fixtures.ldap_directory_fake import SERVICE_DN
from tests.fixtures.ldap_directory_fake import FakeEntry
from tests.fixtures.ldap_directory_fake import install

#: setting name -> ``LdapConfig`` field. All eighteen, so a dropped mapping fails here.
SETTING_TO_FIELD = {
    "LDAP_ENABLED": "enabled",
    "LDAP_SERVER": "server",
    "LDAP_PORT": "port",
    "LDAP_USE_SSL": "use_ssl",
    "LDAP_USE_TLS": "use_tls",
    "LDAP_BIND_DN": "bind_dn",
    "LDAP_BIND_PASSWORD": "bind_password",
    "LDAP_SEARCH_BASE": "search_base",
    "LDAP_USERNAME_ATTR": "username_attr",
    "LDAP_EMAIL_ATTR": "email_attr",
    "LDAP_NAME_ATTR": "name_attr",
    "LDAP_GROUP_ATTR": "group_attr",
    "LDAP_USER_SEARCH_FILTER": "user_search_filter",
    "LDAP_ADMIN_USERS": "admin_users",
    "LDAP_ADMIN_GROUPS": "admin_groups",
    "LDAP_USER_GROUPS": "user_groups",
    "LDAP_RECURSIVE_GROUPS": "recursive_groups",
    "LDAP_TIMEOUT": "timeout",
}

#: A distinct, non-default value for every setting, so a swapped pair cannot pass.
DISTINCT_VALUES: dict[str, Any] = {
    "LDAP_ENABLED": True,
    "LDAP_SERVER": "ldaps://dir.test",
    "LDAP_PORT": 1636,
    "LDAP_USE_SSL": False,
    "LDAP_USE_TLS": True,
    "LDAP_BIND_DN": "cn=reader,dc=test",
    "LDAP_BIND_PASSWORD": "reader-credential",
    "LDAP_SEARCH_BASE": "ou=people,dc=test",
    "LDAP_USERNAME_ATTR": "sAMAccountName",
    "LDAP_EMAIL_ATTR": "userMail",
    "LDAP_NAME_ATTR": "displayName",
    "LDAP_GROUP_ATTR": "isMemberOf",
    "LDAP_USER_SEARCH_FILTER": "(&(objectClass=person)(sAMAccountName={username}))",
    "LDAP_ADMIN_USERS": "root,ops",
    "LDAP_ADMIN_GROUPS": "CN=Admins,DC=test",
    "LDAP_USER_GROUPS": "CN=Staff,DC=test;CN=Ops,DC=test",
    "LDAP_RECURSIVE_GROUPS": True,
    "LDAP_TIMEOUT": 42,
}

#: The ``auth_config`` keys of category ``ldap`` (the admin UI / key registry contract).
DB_KEYS = {name.lower() for name in SETTING_TO_FIELD}


def _patch_settings(monkeypatch, values: dict[str, Any]) -> None:
    for name, value in values.items():
        monkeypatch.setattr(core_config.settings, name, value)


class _StoredAuthSettings:
    """The dynamic auth-settings object ``from_db`` consults; it only implements ``get``."""

    def __init__(self, stored: dict[str, Any]) -> None:
        self._stored = stored
        self.asked: list[str] = []

    def get(self, key: str) -> Any:
        self.asked.append(key)
        return self._stored.get(key)


def _from_db(monkeypatch, stored: dict[str, Any]) -> tuple[LdapConfig, _StoredAuthSettings]:
    fake = _StoredAuthSettings(stored)
    monkeypatch.setattr("app.core.auth_settings.get_auth_settings", lambda db: fake)
    return LdapConfig.from_db(object()), fake


class TestFromEnv:
    def test_every_setting_lands_in_the_field_of_the_same_meaning(self, monkeypatch):
        _patch_settings(monkeypatch, DISTINCT_VALUES)

        expected = LdapConfig(
            **{SETTING_TO_FIELD[name]: value for name, value in DISTINCT_VALUES.items()}
        )
        assert LdapConfig.from_env() == expected

    def test_the_mapping_covers_every_config_field(self):
        assert set(SETTING_TO_FIELD.values()) == {f.name for f in dataclasses.fields(LdapConfig)}


class TestFromDb:
    def test_it_asks_for_exactly_the_eighteen_ldap_keys(self, monkeypatch):
        _patch_settings(monkeypatch, DISTINCT_VALUES)

        _, fake = _from_db(monkeypatch, {})

        assert set(fake.asked) == DB_KEYS

    def test_an_unset_key_uses_the_environment_value(self, monkeypatch):
        _patch_settings(monkeypatch, DISTINCT_VALUES)

        cfg, _ = _from_db(monkeypatch, {})

        assert cfg == LdapConfig.from_env()

    def test_stored_values_beat_the_environment(self, monkeypatch):
        _patch_settings(monkeypatch, DISTINCT_VALUES)

        cfg, _ = _from_db(
            monkeypatch,
            {
                "ldap_server": "db.test",
                "ldap_port": 389,
                "ldap_use_ssl": True,
                "ldap_bind_password": "from-the-db",
                "ldap_timeout": 5,
                "ldap_admin_groups": "CN=DbAdmins,DC=test",
            },
        )

        assert (cfg.server, cfg.port, cfg.use_ssl, cfg.bind_password, cfg.timeout) == (
            "db.test",
            389,
            True,
            "from-the-db",
            5,
        )
        assert cfg.admin_groups == "CN=DbAdmins,DC=test"
        assert cfg.bind_dn == DISTINCT_VALUES["LDAP_BIND_DN"]

    @pytest.mark.parametrize(
        "stored,expected",
        [(True, True), (False, False), ("true", True), ("false", False), (1, True), (0, False)],
    )
    def test_booleans_are_coerced(self, monkeypatch, stored, expected):
        _patch_settings(monkeypatch, {**DISTINCT_VALUES, "LDAP_RECURSIVE_GROUPS": not expected})

        cfg, _ = _from_db(monkeypatch, {"ldap_recursive_groups": stored})

        assert cfg.recursive_groups is expected

    def test_a_boolean_string_goes_through_the_repo_truthy_helper(self, monkeypatch):
        from app.core.env import is_truthy

        _patch_settings(monkeypatch, DISTINCT_VALUES)
        for text in ("yes", "on", "1", "TRUE", "no", "off", "0", "nonsense"):
            cfg, _ = _from_db(monkeypatch, {"ldap_enabled": text})
            assert cfg.enabled is is_truthy(text), text

    def test_integers_are_coerced_and_a_bad_one_uses_the_environment(self, monkeypatch):
        _patch_settings(monkeypatch, DISTINCT_VALUES)

        good, _ = _from_db(monkeypatch, {"ldap_port": "389", "ldap_timeout": "25"})
        bad, _ = _from_db(monkeypatch, {"ldap_port": "not-a-port", "ldap_timeout": [3]})

        assert (good.port, good.timeout) == (389, 25)
        assert (bad.port, bad.timeout) == (1636, 42)

    def test_empty_strings_fall_back_per_field(self, monkeypatch):
        _patch_settings(monkeypatch, DISTINCT_VALUES)
        empty = {key: "" for key in DB_KEYS if key not in _NON_STRING_KEYS}

        cfg, _ = _from_db(monkeypatch, empty)

        assert cfg.username_attr == "uid"
        assert cfg.email_attr == "mail"
        assert cfg.name_attr == "cn"
        assert cfg.user_search_filter == "(uid={username})"
        # Not "memberOf": an explicitly stored empty group attribute disables group retrieval.
        assert cfg.group_attr == ""
        for field in ("server", "bind_dn", "bind_password", "search_base"):
            assert getattr(cfg, field) == "", field
        for field in ("admin_users", "admin_groups", "user_groups"):
            assert getattr(cfg, field) == "", field

    def test_a_non_string_value_is_stringified(self, monkeypatch):
        _patch_settings(monkeypatch, DISTINCT_VALUES)

        cfg, _ = _from_db(monkeypatch, {"ldap_server": 10, "ldap_admin_users": 7})

        assert (cfg.server, cfg.admin_users) == ("10", "7")

    def test_the_username_attr_placeholder_is_resolved(self, monkeypatch):
        _patch_settings(monkeypatch, DISTINCT_VALUES)

        cfg, _ = _from_db(
            monkeypatch,
            {
                "ldap_username_attr": "mailNickname",
                "ldap_user_search_filter": "(&(objectClass=user)({username_attr}={username}))",
            },
        )

        assert cfg.user_search_filter == "(&(objectClass=user)(mailNickname={username}))"

    def test_the_result_is_frozen(self, monkeypatch):
        _patch_settings(monkeypatch, DISTINCT_VALUES)
        cfg, _ = _from_db(monkeypatch, {})
        field = "server"

        with pytest.raises(AttributeError):
            setattr(cfg, field, "elsewhere")
        assert cfg.server == DISTINCT_VALUES["LDAP_SERVER"]


_NON_STRING_KEYS = {
    "ldap_enabled",
    "ldap_port",
    "ldap_use_ssl",
    "ldap_use_tls",
    "ldap_recursive_groups",
    "ldap_timeout",
}


class TestLoginConfigSource:
    def _directory(self, monkeypatch):
        d = install(monkeypatch)
        entry = FakeEntry("uid=alice,dc=test", {"uid": "alice", "mail": "alice@test.example"})
        d.add_user(entry, "alice-pw")
        return d

    def _env_cfg(self) -> LdapConfig:
        return LdapConfig(
            enabled=True,
            server="env.test",
            bind_dn=SERVICE_DN,
            bind_password=SERVICE_CREDENTIAL,
            search_base=SEARCH_BASE,
        )

    def test_a_db_session_uses_from_db(self, monkeypatch):
        d = self._directory(monkeypatch)
        db_cfg = dataclasses.replace(self._env_cfg(), server="db.test")
        monkeypatch.setattr(LdapConfig, "from_db", classmethod(lambda cls, db: db_cfg))
        monkeypatch.setattr(LdapConfig, "from_env", classmethod(lambda cls: self._env_cfg()))

        assert ldap_authenticate("alice", "alice-pw", db=object()) is not None
        assert [c[1] for c in d.calls if c[0] == "server"] == ["db.test"]

    def test_a_failing_db_read_falls_back_to_the_environment_with_a_warning(
        self, monkeypatch, caplog
    ):
        d = self._directory(monkeypatch)

        def _broken(cls, db):
            raise RuntimeError("auth_config unreadable")

        monkeypatch.setattr(LdapConfig, "from_db", classmethod(_broken))
        monkeypatch.setattr(LdapConfig, "from_env", classmethod(lambda cls: self._env_cfg()))

        with caplog.at_level(logging.WARNING):
            result = ldap_authenticate("alice", "alice-pw", db=object())

        assert result is not None and result["username"] == "alice"
        assert [c[1] for c in d.calls if c[0] == "server"] == ["env.test"]
        assert any(
            r.levelno == logging.WARNING and r.name.startswith("app.auth") for r in caplog.records
        )

    def test_without_a_db_session_from_db_is_never_called(self, monkeypatch):
        self._directory(monkeypatch)

        def _must_not_run(cls, db):
            raise AssertionError("from_db called without a db session")

        monkeypatch.setattr(LdapConfig, "from_db", classmethod(_must_not_run))
        monkeypatch.setattr(LdapConfig, "from_env", classmethod(lambda cls: self._env_cfg()))

        result = ldap_authenticate("alice", "alice-pw")
        assert result is not None and result["username"] == "alice"


_PRINT_LDAP_SETTINGS = (
    "import json; from app.core.config import Settings; s = Settings(); "
    "print(json.dumps({k: getattr(s, k) for k in " + repr(sorted(SETTING_TO_FIELD)) + "}))"
)


class TestEnvironmentParsing:
    """Resolved in a clean child process: the class defaults bind once, at import."""

    def _settings(self, run_in_clean_process, tmp_path, **env: str) -> dict[str, Any]:
        out = run_in_clean_process(
            _PRINT_LDAP_SETTINGS,
            unset=tuple(SETTING_TO_FIELD),
            TESTING="true",
            DATA_DIR=str(tmp_path / "data"),
            MODELS_DIR=str(tmp_path / "models"),
            TEMP_DIR=str(tmp_path / "temp"),
            UPLOAD_DIR=str(tmp_path / "up"),
            **env,
        )
        parsed: dict[str, Any] = json.loads(out)
        return parsed

    def test_the_shipped_defaults(self, run_in_clean_process, tmp_path):
        assert self._settings(run_in_clean_process, tmp_path) == {
            "LDAP_ENABLED": False,
            "LDAP_SERVER": "",
            "LDAP_PORT": 636,
            "LDAP_USE_SSL": True,
            "LDAP_USE_TLS": False,
            "LDAP_BIND_DN": "",
            "LDAP_BIND_PASSWORD": "",
            "LDAP_SEARCH_BASE": "",
            "LDAP_USERNAME_ATTR": "sAMAccountName",
            # The placeholder default, resolved against the username attribute.
            "LDAP_USER_SEARCH_FILTER": "(sAMAccountName={username})",
            "LDAP_EMAIL_ATTR": "mail",
            "LDAP_NAME_ATTR": "cn",
            "LDAP_TIMEOUT": 10,
            "LDAP_ADMIN_USERS": "",
            "LDAP_ADMIN_GROUPS": "",
            "LDAP_USER_GROUPS": "",
            "LDAP_RECURSIVE_GROUPS": False,
            "LDAP_GROUP_ATTR": "memberOf",
        }

    def test_every_value_is_read_from_the_environment(self, run_in_clean_process, tmp_path):
        env = {
            "LDAP_ENABLED": "true",
            "LDAP_SERVER": "ldap://dir.test",
            "LDAP_PORT": "389",
            "LDAP_USE_SSL": "false",
            "LDAP_USE_TLS": "true",
            "LDAP_BIND_DN": "cn=reader,dc=test",
            "LDAP_BIND_PASSWORD": "reader-credential",
            "LDAP_SEARCH_BASE": "dc=test",
            "LDAP_USERNAME_ATTR": "uid",
            "LDAP_USER_SEARCH_FILTER": "(&(objectClass=person)({username_attr}={username}))",
            "LDAP_EMAIL_ATTR": "userMail",
            "LDAP_NAME_ATTR": "displayName",
            "LDAP_TIMEOUT": "33",
            "LDAP_ADMIN_USERS": "root",
            "LDAP_ADMIN_GROUPS": "CN=Admins,DC=test",
            "LDAP_USER_GROUPS": "CN=Staff,DC=test",
            "LDAP_RECURSIVE_GROUPS": "true",
            "LDAP_GROUP_ATTR": "isMemberOf",
        }

        got = self._settings(run_in_clean_process, tmp_path, **env)

        assert got["LDAP_USER_SEARCH_FILTER"] == "(&(objectClass=person)(uid={username}))"
        assert (got["LDAP_PORT"], got["LDAP_TIMEOUT"]) == (389, 33)
        assert (got["LDAP_ENABLED"], got["LDAP_USE_SSL"], got["LDAP_USE_TLS"]) == (
            True,
            False,
            True,
        )
        assert got["LDAP_RECURSIVE_GROUPS"] is True
        for name in (
            "LDAP_SERVER",
            "LDAP_BIND_DN",
            "LDAP_BIND_PASSWORD",
            "LDAP_SEARCH_BASE",
            "LDAP_USERNAME_ATTR",
            "LDAP_EMAIL_ATTR",
            "LDAP_NAME_ATTR",
            "LDAP_ADMIN_USERS",
            "LDAP_ADMIN_GROUPS",
            "LDAP_USER_GROUPS",
            "LDAP_GROUP_ATTR",
        ):
            assert got[name] == env[name], name

    @pytest.mark.parametrize(
        "text,expected",
        [("TRUE", True), ("1", True), ("yes", True), ("on", True), ("0", False), ("off", False)],
    )
    def test_booleans_use_the_settings_parser(self, run_in_clean_process, tmp_path, text, expected):
        got = self._settings(
            run_in_clean_process, tmp_path, LDAP_RECURSIVE_GROUPS=text, LDAP_USE_TLS=text
        )

        assert (got["LDAP_RECURSIVE_GROUPS"], got["LDAP_USE_TLS"]) == (expected, expected)
