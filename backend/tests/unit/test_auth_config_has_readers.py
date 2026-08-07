"""Every admin-settable auth-config key must reach behaviour, or say it cannot.

This is the test whose absence let 30 dead configuration keys ship green: the
admin saved, got a success toast, and the running system was unchanged.

The contract
------------
For every key in ``AuthConfigService.CONFIG_CATEGORIES`` exactly one of:

1. a **reader outside the config plane** resolves it through the layered
   DB > .env > coded-default accessor (``DynamicAuthSettings``), or
2. the key is in ``AuthConfigService.RESTART_REQUIRED_KEYS``, which the admin UI
   renders as a "requires restart" badge.

A silent no-op is not an option; an honest badge is.

Why AST and not grep
--------------------
A grep for the key name matches its own docstring, its ``ENV_TO_CONFIG_MAPPING``
entry, a log line, a comment — all of which are exactly what the dead keys had.
A reader here has to be one of:

* a string literal in the **argument position** of a call whose callee name is a
  config accessor (``get`` / ``get_bool`` / ``get_int`` / ``get_str`` and the
  ``_get*`` wrappers ``ldap_auth`` / ``oidc.config`` build, plus
  ``AuthConfigService.get_effective_config``), or
* an attribute access ``<something>.<key>`` where ``<key>`` is a **property
  declared on ``DynamicAuthSettings``** — i.e. the layered accessor, not
  ``settings.<KEY>``, which is the .env-only read the dead keys were stuck on.

Both forms require the value to flow out of the layered accessor into the
module. Neither can be satisfied by naming the key in prose.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.core.auth_settings import DynamicAuthSettings
from app.services.auth_config_service import AuthConfigService

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: Modules that *are* the configuration plane. A key referenced only here is
#: stored, validated, audited and served back — and changes nothing.
_CONFIG_PLANE = {
    _APP_ROOT / "core" / "auth_settings.py",
    _APP_ROOT / "services" / "auth_config_service.py",
    _APP_ROOT / "schemas" / "auth_config.py",
    _APP_ROOT / "models" / "auth_config.py",
    _APP_ROOT / "api" / "endpoints" / "auth_config.py",
}

#: Callee names that mean "resolve this config key through the layered accessor".
#: ``_get*`` are the local wrappers ``ldap_auth``/``oidc.config`` define around
#: ``DynamicAuthSettings.get``.
_ACCESSOR_CALLS = frozenset(
    {
        "get",
        "get_bool",
        "get_int",
        "get_str",
        "_get",
        "_get_bool",
        "_get_int",
        "_get_str",
        "get_effective_config",
    }
)

#: Properties on ``DynamicAuthSettings``; an ``x.<name>`` access to one of these
#: is a layered read by construction.
_ACCESSOR_PROPERTIES = frozenset(
    name
    for name, attr in vars(DynamicAuthSettings).items()
    if isinstance(attr, property) and not name.startswith("_")
)


def _callee_name(node: ast.Call) -> str | None:
    """Return the bare function name of a call node, ignoring the receiver."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _keys_read_by(tree: ast.AST) -> set[str]:
    """Collect config keys this module resolves through the layered accessor."""
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _callee_name(node) not in _ACCESSOR_CALLS:
                continue
            args = list(node.args) + [kw.value for kw in node.keywords]
            found.update(
                arg.value
                for arg in args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            )
        elif isinstance(node, ast.Attribute) and node.attr in _ACCESSOR_PROPERTIES:
            found.add(node.attr)

    return found


def _scan_readers() -> dict[str, set[str]]:
    """Map config key -> set of module paths (repo-relative) that read it."""
    readers: dict[str, set[str]] = {}

    for path in sorted(_APP_ROOT.rglob("*.py")):
        if path in _CONFIG_PLANE:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a syntax error fails elsewhere
            continue
        for key in _keys_read_by(tree):
            readers.setdefault(key, set()).add(str(path.relative_to(_APP_ROOT.parent)))

    return readers


_READERS = _scan_readers()

_ALL_KEYS = sorted({key for keys in AuthConfigService.CONFIG_CATEGORIES.values() for key in keys})

#: Keys known to be inert, tracked as **strict** xfails rather than quietly
#: excluded: the moment one is bridged this test XPASSes and fails, forcing the
#: entry to be deleted, and until then the failure is visible in the report.
#:
#: All of these live in ``auth/pki_auth.py``, which reads ``settings.PKI_*``
#: directly for everything except ``pki_enabled`` / ``pki_admin_dns`` /
#: ``pki_mode`` / ``pki_allow_password_fallback``. Bridging them is the same
#: change made here for the password-policy and lockout planes: resolve through
#: ``DynamicAuthSettings`` at the call site (``api/endpoints/auth/pki.py`` holds a
#: session; ``pki_auth`` helpers do not and would use
#: ``get_process_auth_settings()``).
_NOT_YET_BRIDGED = {
    "pki_ca_cert_path": "auth/pki_auth.py reads settings.PKI_CA_CERT_PATH",
    "pki_verify_revocation": "auth/pki_auth.py reads settings.PKI_VERIFY_REVOCATION",
    "pki_cert_header": "auth/pki_auth.py reads settings.PKI_CERT_HEADER",
    "pki_cert_dn_header": "auth/pki_auth.py reads settings.PKI_CERT_DN_HEADER",
    "pki_ocsp_timeout_seconds": "auth/pki_auth.py reads settings.PKI_OCSP_TIMEOUT_SECONDS",
    "pki_crl_cache_seconds": "auth/pki_auth.py reads settings.PKI_CRL_CACHE_SECONDS",
    "pki_revocation_soft_fail": "auth/pki_auth.py reads settings.PKI_REVOCATION_SOFT_FAIL",
    "pki_trusted_proxies": "auth/pki_auth.py reads settings.PKI_TRUSTED_PROXIES",
}


def _case(key: str) -> Any:
    """Build the parametrize entry for *key*, xfailing the known-inert ones."""
    reason = _NOT_YET_BRIDGED.get(key)
    if reason is None:
        return key
    return pytest.param(key, marks=pytest.mark.xfail(strict=True, reason=reason))


@pytest.mark.parametrize("key", [_case(key) for key in _ALL_KEYS])
def test_settable_key_changes_behaviour_or_declares_a_restart(key: str) -> None:
    """A settable key must have a reader outside the config plane, or a badge."""
    if key in AuthConfigService.RESTART_REQUIRED_KEYS:
        assert key not in _READERS, (
            f"'{key}' is marked requires_restart but is read live outside the config "
            "plane. Either it is live (drop it from RESTART_REQUIRED_KEYS) or the "
            "reader is misleading."
        )
        return

    assert key in _READERS, (
        f"Auth config key '{key}' is settable in the admin UI and nothing outside the "
        "configuration plane resolves it through DynamicAuthSettings. Saving it does "
        "nothing. Either bridge it to its consumer, or — if it is frozen at import or "
        "decoration time — add it to AuthConfigService.RESTART_REQUIRED_KEYS so the UI "
        "says so."
    )


def test_restart_required_keys_are_real_keys() -> None:
    """A badge on a key nobody can set is a lie in the other direction."""
    unknown = AuthConfigService.RESTART_REQUIRED_KEYS - set(_ALL_KEYS)
    assert not unknown, f"RESTART_REQUIRED_KEYS names keys no category declares: {unknown}"


def test_retired_keys_are_no_longer_settable() -> None:
    """The deleted orphan aliases must not come back as writable keys."""
    still_settable = AuthConfigService.RETIRED_KEYS & set(_ALL_KEYS)
    assert not still_settable, (
        f"Retired alias key(s) reintroduced into a category schema: {still_settable}. "
        "Each duplicates a real key; two writable spellings of one control is how "
        "the two end up disagreeing."
    )


def test_scanner_finds_a_known_reader() -> None:
    """Guard the guard: a scanner that silently matches nothing passes everything."""
    assert "ldap_server" in _READERS, "AST scan found no reader for ldap_server — it is wired"
    assert "app/auth/ldap_auth.py" in _READERS["ldap_server"]
    assert _ACCESSOR_PROPERTIES, "DynamicAuthSettings exposes no properties — scan is blind"
