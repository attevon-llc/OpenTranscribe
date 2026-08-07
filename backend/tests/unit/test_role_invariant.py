"""Regression guards for the ``role`` / ``is_superuser`` invariant.

``User.role`` is the sole authorization truth and ``is_superuser`` is a *derived
mirror* of ``role == 'super_admin'``, enforced in the database by the CHECK
constraint ``ck_user_superuser_matches_role`` (migration ``v369``). Two rules follow,
and both were violated by ``_convert_local_user_to_ldap``:

1. **No impossible pair.** ``role='admin'`` with ``is_superuser=True`` fails the CHECK,
   so the ``db.commit()`` inside the conversion raised ``IntegrityError`` — a hard 500
   for any local user listed in ``LDAP_ADMIN_USERS`` / ``LDAP_ADMIN_GROUPS``, on every
   attempt, with no way to ever complete the conversion.
2. **External IdPs grant at most ``admin``.** ``super_admin`` is local-only, so an IdP
   sync must never *demote* one. The LDAP conversion assigned ``role = 'admin'``
   unconditionally, silently stripping a local platform owner.

``_FakeSession.commit`` re-implements the CHECK constraint, so test 1 fails loudly
against the old code instead of quietly passing on a fake that tolerates anything.

The AST guard pins the *cause* rather than the symptom: every write to
``is_superuser`` in these modules must be a call to
:func:`app.auth.roles.role_implies_superuser`, so no future edit can reintroduce an
independent derivation (a literal, or an inlined ``role == 'super_admin'``).
"""

# mypy: disable-error-code="arg-type"
# These tests pass structural stand-ins to signatures that declare
# Session/User/…Data. Suppressing arg-type for the file is the honest
# statement of that; the alternative is casts at every call site, or widening
# a production signature to suit a test.
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.auth.keycloak_auth as keycloak_auth
import app.auth.ldap_auth as ldap_auth
import app.auth.pki_auth as pki_auth
import app.initial_data as initial_data
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import role_implies_superuser

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class InvariantViolationError(AssertionError):
    """Raised by the fake session when a commit would fail the DB CHECK."""


class _FakeSession:
    """Minimal Session stub that enforces ``ck_user_superuser_matches_role``.

    The real constraint turns a bad pair into an ``IntegrityError`` at commit time;
    this raises at the same point so the tests exercise the same failure boundary.
    """

    def __init__(self, user: SimpleNamespace) -> None:
        self.user = user
        self.commits = 0

    def commit(self) -> None:
        if self.user.is_superuser != (self.user.role == ROLE_SUPER_ADMIN):
            raise InvariantViolationError(
                "ck_user_superuser_matches_role violated: "
                f"role={self.user.role!r}, is_superuser={self.user.is_superuser!r}"
            )
        self.commits += 1

    def add(self, obj: object) -> None:  # pragma: no cover - conversions never add
        raise AssertionError("conversion helpers must not create rows")

    # Since v376 the LDAP/OIDC conversions delegate privilege to
    # ``idp_group_mapping_service.reconcile_user``, which reads the account's group
    # memberships. There are none here — no ``group_mapping`` rows either — so the
    # legacy admin signal alone must still produce the same end state.
    def query(self, *args: object, **kwargs: object) -> _EmptyResult:
        return _EmptyResult()

    def delete(self, obj: object) -> None:  # pragma: no cover - nothing to remove
        raise AssertionError("conversion must not delete rows in these fixtures")

    def rollback(self) -> None:  # pragma: no cover - defensive
        pass


class _EmptyResult:
    def filter(self, *args: object, **kwargs: object) -> _EmptyResult:
        return self

    def all(self) -> list:
        return []


def _local_user(role: str) -> SimpleNamespace:
    """A local-auth user row whose mirror is consistent to begin with."""
    return SimpleNamespace(
        id=1,
        uuid="019ec90a-0000-7000-8000-000000000001",
        email="person@example.com",
        full_name="Test Person",
        role=role,
        is_superuser=role_implies_superuser(role),
        auth_type="local",
        hashed_password="hashed",
    )


def _ldap_data(is_admin: bool) -> dict:
    return {
        "username": "tperson",
        "email": "person@example.com",
        "full_name": "Test Person",
        "is_admin": is_admin,
        "groups": ["cn=admins,ou=groups,dc=example,dc=com"] if is_admin else [],
    }


def _keycloak_data(is_admin: bool) -> dict:
    return {
        "keycloak_id": "kc-uuid-1234",
        "email": "person@example.com",
        "full_name": "Test Person",
        "username": "tperson",
        "is_admin": is_admin,
        "roles": ["admin"] if is_admin else ["user"],
        "cert_dn": None,
        "cert_serial": None,
        "cert_issuer": None,
        "cert_org": None,
        "cert_ou": None,
        "cert_valid_from": None,
        "cert_valid_until": None,
        "cert_fingerprint": None,
    }


def _pki_data(is_admin: bool) -> dict:
    return {
        "subject_dn": "CN=Test Person,OU=People,O=Example,C=US",
        "common_name": "Test Person",
        "email": "person@example.com",
        "is_admin": is_admin,
        "serial_number": None,
        "issuer_dn": None,
        "organization": None,
        "organizational_unit": None,
        "not_before": None,
        "not_after": None,
        "fingerprint": None,
    }


def _convert_ldap(db, user, is_admin: bool):
    """Conversion + reconciliation, in the order ``sync_ldap_user_to_db`` runs them.

    Privilege used to be decided inside the conversion helper; since v376 it is
    decided once, in ``idp_group_mapping_service.reconcile_user``, for both the
    conversion and the update paths. The adapter follows the move so the tests keep
    pinning the end state of a real login rather than of one internal helper.
    """
    from app.models.group import MAPPING_SOURCE_LDAP
    from app.services.idp_group_mapping_service import reconcile_user

    data = _ldap_data(is_admin)
    ldap_auth._convert_local_user_to_ldap(db, user, data["username"], data["email"], data)
    reconcile_user(db, user, MAPPING_SOURCE_LDAP, data["groups"], legacy_admin=is_admin)
    return user


def _convert_keycloak(db, user, is_admin: bool):
    """Conversion + reconciliation — see :func:`_convert_ldap`."""
    from app.models.group import MAPPING_SOURCE_OIDC
    from app.services.idp_group_mapping_service import reconcile_user

    data = _keycloak_data(is_admin)
    keycloak_auth._convert_local_user_to_keycloak(db, user, data)
    reconcile_user(db, user, MAPPING_SOURCE_OIDC, data["roles"], legacy_admin=is_admin)
    return user


def _convert_pki(db, user, is_admin: bool):
    return pki_auth._convert_local_user_to_pki(db, user, _pki_data(is_admin))


#: ``(provider label, conversion adapter)`` for every local -> external IdP conversion.
CONVERSIONS = [
    pytest.param(_convert_ldap, id="ldap"),
    pytest.param(_convert_keycloak, id="keycloak"),
    pytest.param(_convert_pki, id="pki"),
]


# ---------------------------------------------------------------------------
# Conversion behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("convert", CONVERSIONS)
class TestLocalUserConversionKeepsInvariant:
    def test_idp_admin_gets_admin_without_superuser(self, convert):
        """An IdP admin becomes role='admin' — never the CHECK-violating pair."""
        user = _local_user(ROLE_USER)
        db = _FakeSession(user)

        convert(db, user, True)

        assert user.role == ROLE_ADMIN
        assert user.is_superuser is False
        # >= 1: a role change is a second commit since v376 (reconcile_user commits
        # after revoking sessions), and the fake re-checks the invariant on each.
        assert db.commits >= 1

    def test_existing_super_admin_is_not_demoted(self, convert):
        """``super_admin`` is local-only: an IdP sync must not strip it."""
        user = _local_user(ROLE_SUPER_ADMIN)
        db = _FakeSession(user)

        convert(db, user, True)

        assert user.role == ROLE_SUPER_ADMIN
        assert user.is_superuser is True
        # >= 1: a role change is a second commit since v376 (reconcile_user commits
        # after revoking sessions), and the fake re-checks the invariant on each.
        assert db.commits >= 1

    def test_non_idp_admin_is_demoted_to_user(self, convert):
        """A local 'admin' not granted admin by the IdP drops to 'user'."""
        user = _local_user(ROLE_ADMIN)
        db = _FakeSession(user)

        convert(db, user, False)

        assert user.role == ROLE_USER
        assert user.is_superuser is False

    def test_plain_user_stays_plain(self, convert):
        user = _local_user(ROLE_USER)
        db = _FakeSession(user)

        convert(db, user, False)

        assert user.role == ROLE_USER
        assert user.is_superuser is False

    def test_super_admin_survives_a_non_admin_idp(self, convert):
        """The demotion branch is keyed on 'admin' only, so super_admin is untouched."""
        user = _local_user(ROLE_SUPER_ADMIN)
        db = _FakeSession(user)

        convert(db, user, False)

        assert user.role == ROLE_SUPER_ADMIN
        assert user.is_superuser is True


# ---------------------------------------------------------------------------
# Single-derivation guard
# ---------------------------------------------------------------------------

#: Modules that write ``User.is_superuser`` and must use the shared derivation.
DERIVATION_MODULES = [ldap_auth, keycloak_auth, pki_auth, initial_data]

DERIVATION_HELPER = "role_implies_superuser"


def _is_helper_call(node: ast.AST) -> bool:
    """Whether *node* is a ``role_implies_superuser(...)`` call."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == DERIVATION_HELPER
    if isinstance(func, ast.Attribute):
        return func.attr == DERIVATION_HELPER
    return False


def _superuser_writes(tree: ast.AST) -> list[tuple[int, ast.AST]]:
    """Collect every ``is_superuser`` write as ``(lineno, value node)``.

    Covers both assignment (``user.is_superuser = ...``) and construction
    (``User(..., is_superuser=...)``). Reads such as ``User.is_superuser.is_(True)``
    are not writes and are ignored.
    """
    writes: list[tuple[int, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = getattr(target, "attr", None) or getattr(target, "id", None)
                if name == "is_superuser" and node.value is not None:
                    writes.append((node.lineno, node.value))
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "is_superuser":
                    writes.append((kw.value.lineno, kw.value))
    return writes


@pytest.mark.parametrize("module", DERIVATION_MODULES, ids=lambda m: m.__name__)
def test_is_superuser_only_written_via_role_implies_superuser(module):
    """No literal and no inlined ``role == 'super_admin'`` — one derivation only."""
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
    writes = _superuser_writes(ast.parse(source))

    assert writes, f"{module.__name__} is expected to write is_superuser"

    offenders = [
        f"{module.__name__}:{lineno} -> {ast.dump(value)[:80]}"
        for lineno, value in writes
        if not _is_helper_call(value)
    ]
    assert not offenders, (
        "is_superuser must be derived via role_implies_superuser(); found "
        f"independent derivations at: {offenders}"
    )


@pytest.mark.parametrize("module", DERIVATION_MODULES, ids=lambda m: m.__name__)
def test_derivation_helper_is_imported(module):
    """The helper must be a real import, not a same-named local shadow."""
    assert getattr(module, DERIVATION_HELPER, None) is role_implies_superuser


def test_role_implies_superuser_is_exact():
    assert role_implies_superuser(ROLE_SUPER_ADMIN) is True
    assert role_implies_superuser(ROLE_ADMIN) is False
    assert role_implies_superuser(ROLE_USER) is False
    assert role_implies_superuser(None) is False
