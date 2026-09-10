"""The LLDAP fixture accounts, in ONE place.

There were three spellings of ``ldap-admin``'s password and they disagreed:

* ``scripts/lib/dev-test-overlays.sh``'s ``seed_ldap_fixture_users`` set ``admin_password``
  before pytest started;
* ``tests/e2e/test_auth_buttons.py`` logged in with ``admin_password``;
* ``tests/e2e/test_ldap_oidc.py`` declared ``LdapAdmin123`` and — from a **session-autouse**
  fixture — ``ldappasswd``'d the account to that value.

Whichever ran first won. Under ``--dist loadfile`` that is a race between workers, and the
loser's bind fails, falls through to local auth, and increments ``ldap-admin``'s **lockout**
bucket: ``canonical_identifier`` collapses an ``ldap_uid`` onto the account's email, and lockout
is progressive, so a few gate runs lock the account for 15 minutes and poison every later test
that authenticates as it. Observed: both gated ``TestLDAPLogin`` tests failing at
``_wait_for_gallery`` in the 2026-09-04 ``--full`` log, and ``attempt N/5`` in the backend log.

So this module is the single source, imported by both e2e modules and read by
``seed_ldap_fixture_users`` (through ``VENV_PY``) so the pre-pytest seeding and the tests cannot
diverge again. ``tests/unit/test_ldap_fixture_credentials_single_source.py`` fails if a second
spelling reappears.

⚠️ These are fixture credentials for a throwaway IdP container that only ever runs on a
developer machine — never real secrets, and never reachable from a deployed stack.
"""

from __future__ import annotations

from typing import NamedTuple

LLDAP_BASE_DN = "dc=example,dc=com"

# LLDAP's own bootstrap administrator, set by LLDAP_LDAP_USER_PASS in
# docker-compose.ldap-test.yml. Everything below is created/modified by binding as this.
LLDAP_ADMIN_USER = "admin"
LLDAP_ADMIN_PASSWORD = "admin_password"  # noqa: S105 — fixture IdP bootstrap, see module docstring
LLDAP_BIND_DN = f"uid={LLDAP_ADMIN_USER},ou=people,{LLDAP_BASE_DN}"


class FixtureUser(NamedTuple):
    """One LLDAP account the e2e suite depends on."""

    uid: str
    email: str
    display_name: str
    password: str
    groups: tuple[str, ...]


LDAP_ADMIN = FixtureUser(
    uid="ldap-admin",
    email="ldap-admin@example.com",
    display_name="LDAP Admin",
    password="LdapAdmin123",  # noqa: S106 — fixture IdP account, see module docstring
    groups=("Admins", "Users"),
)
LDAP_REGULAR = FixtureUser(
    uid="ldap-user",
    email="ldap-user@example.com",
    display_name="LDAP Regular User",
    password="LdapUser123",  # noqa: S106 — fixture IdP account, see module docstring
    groups=("Users",),
)
# A real LLDAP account that is NEVER logged in successfully, reserved for the wrong-password
# test. Rejecting a bad bind for a user that exists is a different branch from rejecting an
# unknown user, so the coverage has to keep a real account — but pointing it at ldap-admin fed
# failures into that account's lockout bucket. Because this uid never authenticates
# successfully, the app never provisions a local User for it, so its bucket belongs to nothing.
#
# The value is spelled out rather than made to look like a credential: a realistic-looking
# string here is both a secret-scanner finding and a standing invitation for someone to "fix"
# it into something that binds, which would silently invert the test. What the negative test
# actually submits is a different literal again, so no assertion depends on this value.
LDAP_NEGATIVE = FixtureUser(
    uid="ldap-negative",
    email="ldap-negative@example.com",
    display_name="LDAP Negative Fixture",
    password="never-used-for-a-successful-bind",  # noqa: S106 — see the comment above
    groups=(),
)

#: Every account the seeding must create, in creation order.
FIXTURE_USERS: tuple[FixtureUser, ...] = (LDAP_ADMIN, LDAP_REGULAR, LDAP_NEGATIVE)


def seed_records() -> str:
    """Tab-separated records for ``seed_ldap_fixture_users`` in dev-test-overlays.sh.

    ``ADMIN<TAB>uid<TAB>password`` — the bind LLDAP's own bootstrap admin uses;
    ``USER<TAB>uid<TAB>password`` — one per fixture account to set.

    The shell script runs this module through ``$VENV_PY`` rather than carrying its own copy of
    the credentials, which is the entire point of this file: the pre-pytest seeding and the
    tests can no longer disagree about what ``ldap-admin``'s password is.
    """
    lines = [f"ADMIN\t{LLDAP_ADMIN_USER}\t{LLDAP_ADMIN_PASSWORD}"]
    lines += [f"USER\t{u.uid}\t{u.password}" for u in FIXTURE_USERS]
    return "\n".join(lines)


if __name__ == "__main__":
    print(seed_records())
