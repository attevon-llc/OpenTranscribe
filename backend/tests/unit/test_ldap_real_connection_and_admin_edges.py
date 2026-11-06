"""LDAP behaviour the other LDAP unit tests do not pin.

* ``test_starttls_is_negotiated_after_a_real_ldap3_open`` uses ldap3's own ``Connection``:
  its ``open()`` is the strategy's (``ldap3/strategy/base.py``), which returns ``None`` on
  success and raises on failure. Every scripted fake used to return ``True`` from ``open()``,
  which hid that ``_connect_and_bind`` read the ``None`` as "could not open" and so never
  attempted StartTLS.
* ``test_the_connection_test_keeps_the_timeout_in_the_settings_range``: the admin "Test
  connection" must not build a config whose timeout is outside the saved setting's 1-300 s.
* The two ``*_stays_refused`` tests pin two places where the code is deliberately strict
  (an address with a trailing newline; an empty username against an empty admin entry).
* ``test_admin_users_matches_a_directory_username_in_any_case`` pins that the directory-side
  username is lower-cased before it is compared with ``admin_users``.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from typing import Any

import ldap3
import pytest

from app.api.endpoints import auth_config_connection_tests
from app.auth import ldap_directory
from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import _is_ldap_admin
from app.auth.ldap_auth import _is_valid_email


@pytest.fixture
def silent_listener() -> Iterator[int]:
    """A TCP port that accepts connections and never speaks LDAP."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    accepted: list[socket.socket] = []
    thread = threading.Thread(target=lambda: accepted.append(listener.accept()[0]), daemon=True)
    thread.start()
    yield listener.getsockname()[1]
    listener.close()
    for conn in accepted:
        conn.close()


def test_starttls_is_negotiated_after_a_real_ldap3_open(monkeypatch, silent_listener):
    negotiated: list[str] = []

    class _RealConnection(ldap3.Connection):
        """ldap3's own Connection; only the StartTLS exchange is replaced (it needs a server)."""

        def start_tls(self, read_server_info: bool = True) -> bool:
            negotiated.append("start_tls")
            return False

    monkeypatch.setattr(ldap_directory, "Connection", _RealConnection)
    cfg = LdapConfig(
        server="127.0.0.1", port=silent_listener, use_ssl=False, use_tls=True, timeout=2
    )
    # get_info=NONE keeps ldap3 from reading the rootDSE off a listener that never answers;
    # open() is the same strategy method with or without it.
    server = ldap3.Server(
        "127.0.0.1", port=silent_listener, use_ssl=False, get_info=ldap3.NONE, connect_timeout=2
    )

    result = ldap_directory._connect_and_bind(cfg, server, "cn=svc,dc=test", "pw")

    assert negotiated == ["start_tls"], "a successful open() never reached StartTLS"
    assert result is None


@pytest.mark.parametrize("submitted", [301, 100_000])
def test_the_connection_test_keeps_the_timeout_in_the_settings_range(monkeypatch, submitted):
    seen: list[int] = []

    def _record_and_stop(cfg: Any) -> Any:
        seen.append(cfg.timeout)
        raise RuntimeError("stop before any network I/O")

    monkeypatch.setattr(ldap_directory, "_get_ldap_server", _record_and_stop)
    auth_config_connection_tests._test_ldap_connection(
        {
            "ldap_server": "ldaps://ad.example.invalid",
            "ldap_use_ssl": True,
            "ldap_bind_dn": "cn=svc",
            "ldap_bind_password": "pw",
            "ldap_timeout": submitted,
        }
    )

    assert all(1 <= t <= 300 for t in seen), f"connection test built a config with timeout={seen}"


def test_an_address_with_a_trailing_newline_stays_refused():
    assert _is_valid_email("jane@corp.example\n") is False
    assert _is_valid_email("jane@corp.example") is True


@pytest.mark.parametrize("admin_users", ["alice,", " , bob", "alice,,bob"])
def test_an_empty_username_against_an_empty_admin_users_entry_stays_refused(admin_users):
    cfg = LdapConfig(admin_users=admin_users)

    assert _is_ldap_admin(cfg, "", []) is False
    assert _is_ldap_admin(cfg, "  ", []) is False
    assert _is_ldap_admin(cfg, "alice" if "alice" in admin_users else "bob", []) is True


def test_admin_users_matches_a_directory_username_in_any_case():
    """Kills mutant M22: no acceptance test upper-cases the DIRECTORY side of the comparison."""
    cfg = LdapConfig(admin_users="jane@corp.example")

    assert _is_ldap_admin(cfg, "JANE@Corp.Example", []) is True
    assert _is_ldap_admin(cfg, "john@corp.example", []) is False
