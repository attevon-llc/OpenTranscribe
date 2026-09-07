"""Regression tests: ``LdapConfig.use_tls`` must gate a real StartTLS upgrade.

``use_tls`` was resolved from ``.env``/DB into ``LdapConfig`` but never consulted
by the bind code (``_bind_service_account`` / ``_verify_user_credentials`` only
branched on ``use_ssl``) — every StartTLS deployment bound in cleartext regardless
of the setting. ``_connect_and_bind`` (``app/auth/ldap_auth.py``) is the fix: it
negotiates StartTLS explicitly when ``use_tls`` is set and ``use_ssl`` is not, and
FAILS CLOSED — refuses the bind entirely — if that negotiation does not succeed,
rather than silently falling back to sending the password over the still-cleartext
connection.
"""

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import _connect_and_bind


class FakeServer:
    pass


class FakeConnection:
    """Stand-in for ldap3's ``Connection`` that records what was called."""

    instances: list["FakeConnection"] = []

    def __init__(self, server, user, password, auto_bind):
        self.server = server
        self.user = user
        self.password = password
        self.auto_bind = auto_bind
        self.open_called = False
        self.start_tls_called = False
        self.bind_called = False
        self.start_tls_result = True
        self.open_result = True
        self.bind_result = True
        FakeConnection.instances.append(self)

    def open(self):
        self.open_called = True
        return self.open_result

    def start_tls(self):
        self.start_tls_called = True
        return self.start_tls_result

    def bind(self):
        self.bind_called = True
        return self.bind_result


class TestConnectAndBindTls:
    def setup_method(self):
        FakeConnection.instances = []

    def _patch(self, monkeypatch, **conn_kwargs):
        def _factory(server, user, password, auto_bind):
            conn = FakeConnection(server, user, password, auto_bind)
            for k, v in conn_kwargs.items():
                setattr(conn, k, v)
            return conn

        monkeypatch.setattr("app.auth.ldap_auth.Connection", _factory)

    def test_use_tls_negotiates_starttls_before_bind(self, monkeypatch):
        self._patch(monkeypatch)
        cfg = LdapConfig(enabled=True, server="ldap.example.com", use_ssl=False, use_tls=True)
        conn = _connect_and_bind(cfg, FakeServer(), "cn=svc", "pw")
        assert conn is not None
        assert conn.start_tls_called is True
        assert conn.bind_called is True

    def test_use_tls_fails_closed_when_negotiation_fails(self, monkeypatch):
        """The core bug: a failed StartTLS negotiation must refuse the bind, not
        fall back to sending the password over the (now-established) cleartext
        connection."""
        self._patch(monkeypatch, start_tls_result=False)
        cfg = LdapConfig(enabled=True, server="ldap.example.com", use_ssl=False, use_tls=True)
        conn = _connect_and_bind(cfg, FakeServer(), "cn=svc", "pw")
        assert conn is None
        created = FakeConnection.instances[0]
        assert created.start_tls_called is True
        assert created.bind_called is False, "must not bind after failed StartTLS negotiation"

    def test_use_ssl_skips_starttls_negotiation(self, monkeypatch):
        """use_ssl already wraps the socket (ldaps://) via the Server object; no
        separate StartTLS step is needed or attempted."""
        self._patch(monkeypatch)
        cfg = LdapConfig(enabled=True, server="ldap.example.com", use_ssl=True, use_tls=False)
        conn = _connect_and_bind(cfg, FakeServer(), "cn=svc", "pw")
        assert conn is not None
        assert conn.start_tls_called is False
        assert conn.bind_called is True

    def test_neither_flag_binds_in_cleartext_unchanged(self, monkeypatch):
        self._patch(monkeypatch)
        cfg = LdapConfig(enabled=True, server="ldap.example.com", use_ssl=False, use_tls=False)
        conn = _connect_and_bind(cfg, FakeServer(), "cn=svc", "pw")
        assert conn is not None
        assert conn.start_tls_called is False
        assert conn.bind_called is True

    def test_bind_failure_after_successful_starttls_returns_none(self, monkeypatch):
        self._patch(monkeypatch, bind_result=False)
        cfg = LdapConfig(enabled=True, server="ldap.example.com", use_ssl=False, use_tls=True)
        conn = _connect_and_bind(cfg, FakeServer(), "cn=svc", "pw")
        assert conn is None
