"""A setting that lives in the admin UI must not also ship as an env var.

This repo deliberately prefers DB-backed settings edited in the admin UI over
environment variables (``backend/CLAUDE.md``). The resolution order is
**database > .env > coded default**, so once a setting is UI-managed, the env
var is a second copy of the same knob that the UI silently overrides.

Nothing enforced that, and the result is the reason this test exists:
``.env.example`` reached **1937 lines**, of which ~97 keys documented settings
the admin UI had already taken over — LDAP, OIDC and SAML were **100%**
UI-managed while still shipping a full env block each. Two copies of one setting
is how they drift apart, and a template that documents a var the UI overrides
teaches operators something false.

The invariant, in one line: **if a key appears in ``CATEGORY_SCHEMAS``
(the admin UI's own field list), it must not be an ACTIVE assignment in
``.env.example``.**

Deliberately narrow:

* Only **active** assignments fail. A commented ``# KEY=value`` is documentation
  of an escape hatch, which zero-touch provisioning genuinely needs.
* Only **auth_config** categories are checked. That set is machine-readable and
  authoritative. ``SystemSettings`` keys are strings scattered across services
  with no registry to diff against — a heuristic there would produce false
  positives, and a gate people learn to override is worse than no gate.
* ``BOOTSTRAP_EXEMPT`` is a written allowlist, not a silent skip.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: Keys that are UI-managed AND must stay in the template, each with the reason.
#:
#: All four are read by a boot guard in ``backend/app/main.py`` that runs BEFORE
#: any database session exists. In a hardened deployment the app refuses to start
#: when PKI or proxy auth is enabled with an empty trust list — an empty list
#: means "reject every header-sourced identity assertion", which is the
#: fail-closed behaviour. Removing them from the template would delete a
#: security gate that the DB coverage makes look safely redundant.
BOOTSTRAP_EXEMPT = {
    "PKI_ENABLED": "read by main.py's pre-DB boot guard; hardened start refuses on empty trust list",
    "PKI_TRUSTED_PROXIES": "the empty-list fail-closed check itself, evaluated before any DB session",
    "PROXY_ENABLED": "read by main.py's pre-DB boot guard; hardened start refuses on empty trust list",
    "PROXY_TRUSTED_PROXIES": "the empty-list fail-closed check itself, evaluated before any DB session",
}


@lru_cache(maxsize=1)
def _ui_managed_keys() -> frozenset[str]:
    """Every setting the admin UI owns, from the app's own schema registry.

    Derived from ``CATEGORY_SCHEMAS`` rather than listed here, so a new UI field
    is covered the moment it is added — a hand-maintained copy would rot exactly
    the way the template did.
    """
    from app.schemas.auth_config import CATEGORY_SCHEMAS

    return frozenset(
        field.upper() for schema in CATEGORY_SCHEMAS.values() for field in schema.model_fields
    )


def _active_env_keys(text: str) -> dict[str, int]:
    """``KEY -> line number`` for uncommented assignments only."""
    found: dict[str, int] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", raw)
        if match:
            found.setdefault(match.group(1), lineno)
    return found


# ─── guard the guard ────────────────────────────────────────────────────────


def test_the_ui_registry_is_populated():
    """If this returns nothing the real test below passes vacuously."""
    keys = _ui_managed_keys()
    assert len(keys) > 50, f"expected 100+ UI-managed fields, got {len(keys)}"
    for expected in ("LDAP_SERVER", "OIDC_CLIENT_ID", "SAML_SP_ENTITY_ID", "PASSWORD_MIN_LENGTH"):
        assert expected in keys, f"{expected} missing — CATEGORY_SCHEMAS shape changed?"


def test_the_detector_fires_on_a_known_ui_key():
    """A must-fire case: an active UI-managed assignment is a finding."""
    offenders = {
        k: n
        for k, n in _active_env_keys("LDAP_SERVER=ldaps://x\n").items()
        if k in _ui_managed_keys() and k not in BOOTSTRAP_EXEMPT
    }
    assert offenders == {"LDAP_SERVER": 1}, f"detector missed a UI key: {offenders}"


def test_a_commented_example_is_not_a_finding():
    """Must-stay-clean: commented escape hatches are allowed, by design."""
    assert _active_env_keys("# LDAP_SERVER=ldaps://x\n#OIDC_REALM=y\n") == {}


def test_every_exemption_names_a_real_ui_key_and_gives_a_reason():
    """A stale exemption must fail, the way the repo's other allowlists do."""
    ui = _ui_managed_keys()
    for key, reason in BOOTSTRAP_EXEMPT.items():
        assert key in ui, f"exemption {key} is no longer a UI-managed key — delete the entry"
        assert len(reason) > 30, f"exemption {key} needs a real written reason, got {reason!r}"


# ─── the gate ───────────────────────────────────────────────────────────────


def test_no_ui_managed_setting_ships_as_an_active_env_var():
    """The invariant. Fix by deleting the line, not by widening the allowlist."""
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} is missing"
    active = _active_env_keys(ENV_EXAMPLE.read_text())
    offenders = sorted(
        (n, k) for k, n in active.items() if k in _ui_managed_keys() and k not in BOOTSTRAP_EXEMPT
    )
    assert not offenders, "\n".join(
        [
            f"{len(offenders)} setting(s) are editable in the admin UI AND shipped as active",
            "env vars in .env.example. The UI value silently wins, so the template is",
            "documenting something untrue. Delete the line (a commented example is fine),",
            "or add a BOOTSTRAP_EXEMPT entry with a written reason if it is genuinely read",
            "before the database is reachable.",
            "",
        ]
        + [f"  .env.example:{n}  {k}" for n, k in offenders]
    )


@pytest.mark.skipif(not (REPO_ROOT / ".env").is_file(), reason="no local .env (CI)")
def test_the_local_env_has_not_drifted_from_the_template():
    """`.env` must not re-add what the template dropped.

    The local file is gitignored and hand-edited, so it drifts independently —
    and a stale `.env` is what kept the original bug alive through several
    template fixes.
    """
    active = _active_env_keys((REPO_ROOT / ".env").read_text())
    offenders = sorted(
        (n, k) for k, n in active.items() if k in _ui_managed_keys() and k not in BOOTSTRAP_EXEMPT
    )
    assert not offenders, "\n".join(
        ["your local .env sets settings the admin UI owns (the UI value wins):", ""]
        + [f"  .env:{n}  {k}" for n, k in offenders]
    )
