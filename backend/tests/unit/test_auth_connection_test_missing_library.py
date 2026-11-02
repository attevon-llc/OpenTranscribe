"""The admin connection test answers cleanly when its client library cannot be imported.

``_test_ldap_connection`` / ``_test_oidc_connection`` imported ldap3 / httpx inside the
same ``try`` whose ``except`` clauses name ``LDAPException`` / ``httpx.ConnectError``.
When the import itself failed, evaluating those clauses raised ``UnboundLocalError``,
so the request ended in a 500 instead of the "library is not installed" answer.
"""

from __future__ import annotations

import asyncio
import builtins
from typing import Any

import pytest

from app.api.endpoints.auth_config import _test_ldap_connection
from app.api.endpoints.auth_config import _test_oidc_connection

_real_import = builtins.__import__


def _refuse_imports(monkeypatch: pytest.MonkeyPatch, *prefixes: str) -> None:
    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith(prefixes):
            raise ImportError(f"No module named {name!r}")
        return _real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_ldap_test_reports_missing_library(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse_imports(monkeypatch, "ldap3")

    result = _test_ldap_connection({"ldap_server": "ldap.example"})

    assert result.success is False
    assert result.message == "LDAP library (ldap3) is not installed"


def test_ldap_test_without_a_server_still_answers_when_library_present() -> None:
    result = _test_ldap_connection({"ldap_server": ""})

    assert result.success is False
    assert result.message == "LDAP server address is required"


def test_oidc_test_reports_missing_library(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse_imports(monkeypatch, "httpx")

    result = asyncio.run(_test_oidc_connection({"oidc_discovery_url": "https://idp.example/x"}))

    assert result.success is False
    assert result.message == "HTTP client library (httpx) is not installed"


def test_oidc_test_without_a_url_still_answers_when_library_present() -> None:
    result = asyncio.run(_test_oidc_connection({}))

    assert result.success is False
    assert result.message == "Provide either a discovery URL or a server URL"
