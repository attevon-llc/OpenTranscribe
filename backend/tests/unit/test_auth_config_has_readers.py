"""Every admin-settable auth-config key must reach behaviour, or say it cannot.

This is the test whose absence let 30 dead configuration keys ship green: the
admin saved, got a success toast, and the running system was unchanged.

The contract
------------
For every key in ``auth_config_keys.CONFIG_CATEGORIES`` exactly one of:

1. a **reader outside the config plane** resolves it through the layered
   DB > .env > coded-default accessor (``DynamicAuthSettings``), or
2. the key is in ``auth_config_keys.RESTART_REQUIRED_KEYS``, which the admin UI
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

* a string literal passed to a **forwarding helper**: a function in the same
  module with a parameter that its body hands to one of the accessor calls above
  (``def number(key, fallback): return _as_int(stored.get(key), fallback)``).
  The helper is the reader; each literal handed to it is a key it resolves. This
  is how ``LdapConfig.from_db`` reads its seventeen keys without repeating the
  accessor call at every line.

All forms require the value to flow out of the layered accessor into the
module. None can be satisfied by naming the key in prose.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.core.auth_settings import DynamicAuthSettings
from app.services import auth_config_keys

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: Modules that *are* the configuration plane. A key referenced only here is
#: stored, validated, audited and served back — and changes nothing.
_CONFIG_PLANE = {
    _APP_ROOT / "core" / "auth_settings.py",
    _APP_ROOT / "core" / "process_auth_settings.py",
    _APP_ROOT / "services" / "auth_config_service.py",
    _APP_ROOT / "services" / "auth_config_keys.py",
    _APP_ROOT / "schemas" / "auth_config.py",
    _APP_ROOT / "schemas" / "auth_config_validation.py",
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


def _literal_args(node: ast.Call) -> list[str]:
    """String literals in any argument position of a call."""
    args = list(node.args) + [kw.value for kw in node.keywords]
    return [
        arg.value for arg in args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]


def _forwarding_helpers(tree: ast.AST) -> dict[str, set[int | str]]:
    """Find functions that pass one of their parameters to an accessor call.

    Returns:
        Function name -> the positions (``int``) and names (``str``) of the parameters
        it forwards. A call to the function with a string literal at one of those
        positions is a read of that key.
    """
    helpers: dict[str, set[int | str]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs]
        forwarded: set[int | str] = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or _callee_name(node) not in _ACCESSOR_CALLS:
                continue
            # Only the key is forwarded; a later positional is the default value.
            key_args = node.args[:1] + [kw.value for kw in node.keywords if kw.arg == "key"]
            for arg in key_args:
                if isinstance(arg, ast.Name) and arg.id in params:
                    forwarded.update({arg.id, params.index(arg.id)})
        if forwarded:
            helpers[fn.name] = forwarded
    return helpers


def _keys_read_by(tree: ast.AST) -> set[str]:
    """Collect config keys this module resolves through the layered accessor."""
    found: set[str] = set()
    helpers = _forwarding_helpers(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = _callee_name(node)
            if callee in _ACCESSOR_CALLS:
                found.update(_literal_args(node))
            elif callee in helpers:
                for index, arg in enumerate(node.args):
                    if index in helpers[callee] and isinstance(arg, ast.Constant):
                        found.add(str(arg.value))
                found.update(
                    str(kw.value.value)
                    for kw in node.keywords
                    if kw.arg in helpers[callee] and isinstance(kw.value, ast.Constant)
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

_ALL_KEYS = sorted({key for keys in auth_config_keys.CONFIG_CATEGORIES.values() for key in keys})

#: Keys known to be inert, tracked as **strict** xfails rather than quietly
#: excluded: the moment one is bridged this test XPASSes and fails, forcing the
#: entry to be deleted, and until then the failure is visible in the report.
#:
#: **Empty, and that is the point** — keep it that way. The last five entries were
#: the revocation plane (``pki_ca_cert_path``, ``pki_verify_revocation``,
#: ``pki_ocsp_timeout_seconds``, ``pki_crl_cache_seconds``,
#: ``pki_revocation_soft_fail``), which the Settings UI wrote while
#: ``auth/pki_auth.py`` read ``settings.PKI_*`` from ``.env`` — so an operator
#: turning revocation checking off, or widening the OCSP timeout, changed nothing
#: at all. They are bridged in issue #498 the same way the password-policy and
#: lockout planes were: resolve through ``get_process_auth_settings()`` at the
#: call site, because the ``pki_auth`` helpers run deep inside certificate
#: verification and hold no session.
#:
#: A new entry here is a claim that a key the admin UI offers does nothing. Prefer
#: bridging it in the same PR that adds it.
_NOT_YET_BRIDGED: dict[str, str] = {}


def _case(key: str) -> Any:
    """Build the parametrize entry for *key*, xfailing the known-inert ones."""
    reason = _NOT_YET_BRIDGED.get(key)
    if reason is None:
        return key
    return pytest.param(key, marks=pytest.mark.xfail(strict=True, reason=reason))


@pytest.mark.parametrize("key", [_case(key) for key in _ALL_KEYS])
def test_settable_key_changes_behaviour_or_declares_a_restart(key: str) -> None:
    """A settable key must have a reader outside the config plane, or a badge."""
    if key in auth_config_keys.RESTART_REQUIRED_KEYS:
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
        "decoration time — add it to auth_config_keys.RESTART_REQUIRED_KEYS so the UI "
        "says so."
    )


def test_restart_required_keys_are_real_keys() -> None:
    """A badge on a key nobody can set is a lie in the other direction."""
    unknown = auth_config_keys.RESTART_REQUIRED_KEYS - set(_ALL_KEYS)
    assert not unknown, f"RESTART_REQUIRED_KEYS names keys no category declares: {unknown}"


def test_retired_keys_are_no_longer_settable() -> None:
    """The deleted orphan aliases must not come back as writable keys."""
    still_settable = auth_config_keys.RETIRED_KEYS & set(_ALL_KEYS)
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


_FORWARDED = """
def reader(stored):
    def flag(key, fallback):
        return bool(stored.get(key, fallback))

    return flag("ldap_forwarded_key", False)
"""

_NOT_FORWARDED = """
def reader(stored):
    def flag(key, fallback):
        return bool(fallback)

    return flag("ldap_unforwarded_key", False), print("ldap_prose_key")
"""


def test_scanner_follows_a_forwarding_helper() -> None:
    """A literal handed to a helper that feeds the accessor is a read."""
    assert _keys_read_by(ast.parse(_FORWARDED)) == {"ldap_forwarded_key"}


def test_scanner_ignores_a_helper_that_never_reaches_the_accessor() -> None:
    """A helper that drops its parameter, or a bare call naming a key, is no reader."""
    assert _keys_read_by(ast.parse(_NOT_FORWARDED)) == set()
