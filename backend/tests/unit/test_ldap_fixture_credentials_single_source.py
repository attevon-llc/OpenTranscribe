"""The LLDAP fixture accounts must have exactly ONE spelling of each password.

Three places set or used ``ldap-admin``'s password and two of them disagreed:
``scripts/lib/dev-test-overlays.sh`` seeded ``admin_password`` before pytest started,
``tests/e2e/test_auth_buttons.py`` logged in with ``admin_password``, and
``tests/e2e/test_ldap_oidc.py`` declared ``LdapAdmin123`` and — from a **session-autouse**
fixture — ``ldappasswd``'d the account to it.

Whichever ran last won. Under ``--dist loadfile`` that is a race between workers, and the loser
does not merely fail: an LDAP bind rejection falls through to LOCAL auth, which increments the
lockout counter on the resolved account (``canonical_identifier`` collapses an ``ldap_uid`` onto
the account's email). Lockout is progressive, so a few gate runs lock ``ldap-admin`` for 15
minutes and poison every later test that authenticates as it. Both gated ``TestLDAPLogin`` tests
failed at ``_wait_for_gallery`` in the 2026-09-04 ``--full`` log for exactly this.

This is a static check on purpose: the failure it guards needs a live LLDAP, two pytest workers
and a lockout window to reproduce, so a behavioural version could not run in the fast suite —
and by the time it could reproduce, the account is already locked.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_MODULE = REPO_ROOT / "backend" / "tests" / "e2e" / "ldap_fixture_users.py"
OVERLAY_LIB = REPO_ROOT / "scripts" / "lib" / "dev-test-overlays.sh"
E2E_DIR = REPO_ROOT / "backend" / "tests" / "e2e"

pytestmark = pytest.mark.skipif(
    not FIXTURE_MODULE.exists(), reason="backend/tests/e2e/ldap_fixture_users.py not in checkout"
)


def _fixture_passwords() -> dict[str, str]:
    """uid -> password, read the same way the shell script reads it."""
    proc = subprocess.run(
        [sys.executable, str(FIXTURE_MODULE)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"module failed: {proc.stderr}"
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "USER":
            out[parts[1]] = parts[2]
    return out


def test_the_fixture_module_declares_the_accounts_the_suite_uses():
    """Non-vacuity: everything below compares against this set, so it must be real."""
    passwords = _fixture_passwords()
    for uid in ("ldap-admin", "ldap-user", "ldap-negative"):
        assert uid in passwords, f"{uid} missing from ldap_fixture_users; found {sorted(passwords)}"
        assert passwords[uid], f"{uid} has an empty password"


@pytest.mark.skipif(not OVERLAY_LIB.exists(), reason="scripts/lib/dev-test-overlays.sh not present")
def test_the_overlay_seeder_reads_the_module_rather_than_hardcoding():
    text = OVERLAY_LIB.read_text(encoding="utf-8")
    parts = text.split("seed_ldap_fixture_users() {", 1)
    assert len(parts) == 2, "seed_ldap_fixture_users is gone — did the seeding move?"
    body = parts[1].split("\n}", 1)[0]

    assert "LDAP_FIXTURE_USERS_PY" in text, (
        "dev-test-overlays.sh no longer reads backend/tests/e2e/ldap_fixture_users.py. It must: "
        "a password spelled in the shell script is a second source of truth, and the last "
        "writer wins over test_ldap_oidc.py's session-autouse ldappasswd."
    )
    for uid, password in _fixture_passwords().items():
        assert password not in body, (
            f"{uid}'s password is hardcoded in seed_ldap_fixture_users. Read it from "
            f"{FIXTURE_MODULE.name} instead — see this test's docstring for the lockout cascade."
        )


def _ldap_password_literals(source: str) -> dict[str, str]:
    """Module-level ``*LDAP*PASSWORD* = "literal"`` assignments, by target name.

    An AST walk rather than a substring search for the canonical values, because the failure
    being guarded is a copy that DIVERGED — the original bug spelled ``admin_password`` where
    the fixture module says ``LdapAdmin123``, so a "does the real password appear here" check
    would have passed on the broken tree.
    """
    found: dict[str, str] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            name = getattr(target, "id", "")
            if "LDAP" in name.upper() and "PASSWORD" in name.upper():
                found[name] = node.value.value
    return found


@pytest.mark.parametrize("module_name", ["test_auth_buttons.py", "test_ldap_oidc.py"])
def test_no_e2e_module_declares_its_own_fixture_password(module_name: str):
    """No LDAP password literal anywhere but ldap_fixture_users.py — same value or different."""
    path = E2E_DIR / module_name
    if not path.exists():
        pytest.skip(f"{module_name} not in this checkout")
    text = path.read_text(encoding="utf-8")

    literals = _ldap_password_literals(text)
    assert literals == {}, (
        f"{module_name} declares LDAP password literals {literals}. Import them from "
        f"ldap_fixture_users instead: a second copy diverges the moment either side is edited, "
        f"and the divergence surfaces as ldap-admin's progressive lockout rather than as a "
        f"failing assertion — see this module's docstring."
    )
    for uid, password in _fixture_passwords().items():
        assert password not in text, (
            f"{module_name} spells {uid}'s password ({password!r}) itself, under a name this "
            f"check does not recognise. Import it from ldap_fixture_users."
        )


def test_the_literal_detector_fires_on_both_a_copy_and_a_divergence():
    """Guard the guard, in both the shapes that have actually occurred.

    ``test_auth_buttons.py`` used to assign an LDAP password literal whose value was NOT any
    fixture account's password — that divergence WAS the bug, and it is precisely why a search
    for the canonical values could not see it. The synthetic values below are deliberately not
    credential-shaped: the detector keys on the assignment, not on the string, so using a real
    -looking password here would add a secret-scanner finding and prove nothing extra.
    """
    divergent = 'LDAP_USERNAME = "ldap-admin"\nLDAP_PASSWORD = "a-second-copy-that-diverged"\n'
    assert _ldap_password_literals(divergent) == {"LDAP_PASSWORD": "a-second-copy-that-diverged"}

    duplicate = 'LDAP_ADMIN_PASSWORD = "a-verbatim-second-copy"\n'
    assert _ldap_password_literals(duplicate) == {"LDAP_ADMIN_PASSWORD": "a-verbatim-second-copy"}


def test_the_literal_detector_ignores_non_ldap_and_non_literal_assignments():
    """Must-stay-clean: the app admin password and re-exports of the shared table are fine."""
    clean = (
        'APP_ADMIN_PASSWORD = "password"\n'
        "LDAP_ADMIN_PASSWORD = LDAP_ADMIN.password\n"
        "LDAP_REGULAR_PASSWORD = LDAP_REGULAR.password\n"
    )
    assert _ldap_password_literals(clean) == {}
