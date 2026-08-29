"""Regression: ``LdapConfig.from_db`` must resolve the ``{username_attr}`` placeholder.

Same bug class already fixed for the ``.env`` path in
``core/config.py:Settings.validate_auth_settings``, which does
``self.LDAP_USER_SEARCH_FILTER.replace("{username_attr}", self.LDAP_USERNAME_ATTR)``.
That substitution was never ported to the DB-backed loader
(``app/auth/ldap_auth.py:LdapConfig.from_db``), so a DB-configured search filter
using the documented ``{username_attr}`` placeholder — as opposed to a hardcoded
attribute name like ``sAMAccountName`` — carried the literal placeholder into
every LDAP search and matched no real attribute, breaking every DB-configured
LDAP login. The fix extracts the substitution into
``core/config.resolve_ldap_search_filter`` and calls it from both loaders.

A full DB-round-trip version of this also lives in
``tests/test_auth_config_integration.py::TestLdapConfigFromDb`` — this sibling
patches ``get_auth_settings`` so it needs no live Postgres and stays runnable
anywhere, including a ``git archive HEAD`` red-check tree with no ``.env``.
"""

from app.auth.ldap_auth import LdapConfig


class FakeAuthSettings:
    def __init__(self, values):
        self._values = values

    def get(self, key):
        return self._values.get(key)


class TestFromDbResolvesUsernameAttrPlaceholder:
    def test_placeholder_is_substituted(self, monkeypatch):
        fake = FakeAuthSettings(
            {
                "ldap_enabled": True,
                "ldap_server": "ldap.example.com",
                "ldap_search_base": "DC=example,DC=com",
                "ldap_username_attr": "uid",
                "ldap_user_search_filter": "({username_attr}={username})",
            }
        )
        monkeypatch.setattr("app.core.auth_settings.get_auth_settings", lambda db: fake)

        cfg = LdapConfig.from_db(db=object())

        assert "{username_attr}" not in cfg.user_search_filter
        assert cfg.user_search_filter == "(uid={username})"

    def test_placeholder_is_substituted_for_a_non_default_attr(self, monkeypatch):
        fake = FakeAuthSettings(
            {
                "ldap_username_attr": "sAMAccountName",
                "ldap_user_search_filter": "({username_attr}={username})",
            }
        )
        monkeypatch.setattr("app.core.auth_settings.get_auth_settings", lambda db: fake)

        cfg = LdapConfig.from_db(db=object())

        assert cfg.user_search_filter == "(sAMAccountName={username})"
