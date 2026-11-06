"""The admin LDAP "Test connection" resolves unset TLS fields the way a login does.

A TLS field the admin has not set in the panel (no stored row) takes its ``LDAP_*``
environment value at login. The panel submits such a field as ``None`` / ``""``; if the test
read that as "no CA, verify on, no server name" it would report a failure for a directory
every login reaches, or the reverse.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.endpoints.auth_config_connection_tests import _test_ldap_connection
from app.auth import ldap_directory
from app.core.config import settings

STOP = "stopped before connecting"


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []

    def fake_server(cfg: Any) -> object:
        seen.append(cfg)
        return object()

    monkeypatch.setattr(ldap_directory, "_get_ldap_server", fake_server)
    monkeypatch.setattr(ldap_directory, "transport_problem", lambda cfg, server: STOP)
    monkeypatch.setattr(settings, "LDAP_CA_CERT_FILE", "/etc/ssl/env-ca.pem")
    monkeypatch.setattr(settings, "LDAP_TLS_SERVER_NAME", "directory.env.example")
    monkeypatch.setattr(settings, "LDAP_TLS_SKIP_VERIFY", True)
    return seen


def _payload(**tls: Any) -> dict[str, Any]:
    return {"ldap_server": "ldap.example", "ldap_port": 636, "ldap_use_ssl": True, **tls}


@pytest.mark.parametrize(
    "tls",
    [
        {},
        {"ldap_ca_cert_file": "", "ldap_tls_server_name": "", "ldap_tls_skip_verify": None},
        {"ldap_ca_cert_file": None, "ldap_tls_server_name": None},
    ],
    ids=["absent", "blank", "null"],
)
def test_unset_tls_fields_take_the_environment(captured: list[Any], tls: dict[str, Any]) -> None:
    result = _test_ldap_connection(_payload(**tls))

    assert result.message == STOP
    (cfg,) = captured
    assert cfg.ca_cert_file == "/etc/ssl/env-ca.pem"
    assert cfg.tls_server_name == "directory.env.example"
    assert cfg.tls_skip_verify is True


def test_submitted_tls_fields_win(captured: list[Any]) -> None:
    _test_ldap_connection(
        _payload(
            ldap_ca_cert_file="/etc/ssl/panel-ca.pem",
            ldap_tls_server_name="directory.panel.example",
            ldap_tls_skip_verify=False,
        )
    )

    (cfg,) = captured
    assert cfg.ca_cert_file == "/etc/ssl/panel-ca.pem"
    assert cfg.tls_server_name == "directory.panel.example"
    assert cfg.tls_skip_verify is False
