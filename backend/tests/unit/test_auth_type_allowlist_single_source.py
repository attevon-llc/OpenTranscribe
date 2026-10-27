"""``user.auth_type`` allowlist: one source, no silent drift from migrations or the registry (#866).

Three owners used to hold the same fact independently: the hardcoded literal in the
model CHECKs, the literal in each widening migration, and nothing at all on the
registry side -- ``external_sync`` writes an arbitrary ``identity.provider`` into the
constrained column, so a provider registered under a string the CHECK does not admit
failed its first JIT login with a bare ``CheckViolation``.

These tests are in-process (no database): migrations are loaded by path and the model
metadata is inspected, so a drift fails here rather than in a login.
"""

from __future__ import annotations

import importlib.util
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import CheckConstraint

from app.auth import auth_type_admission
from app.auth import constants
from app.auth import provider_registry
from app.auth.auth_type_admission import assert_auth_type_admitted
from app.auth.auth_type_admission import core_admits
from app.auth.constants import DB_ADMITTED_AUTH_TYPES
from app.auth.constants import VALID_AUTH_TYPES
from app.auth.constants import auth_type_check_sql
from app.auth.external_sync import sync_external_user_to_db
from app.auth.provider_registry import ExternalIdentity
from app.core.exceptions import ExternalProviderNotAdmittedError
from app.models.invitation import UserInvitation
from app.models.user import User

pytestmark = pytest.mark.unit

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
_LITERAL = re.compile(r"'([^']+)'")


def _check_body(model: Any, name: str) -> str:
    matches = [
        c for c in model.__table__.constraints if isinstance(c, CheckConstraint) and c.name == name
    ]
    assert len(matches) == 1, f"{model.__name__} must declare exactly one {name}"
    return str(matches[0].sqltext)


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _revision_number(path: Path) -> int:
    match = re.match(r"v(\d+)_", path.name)
    assert match, path.name
    return int(match.group(1))


# ---------------------------------------------------------------- the single source


def test_the_model_checks_are_rendered_from_the_single_source() -> None:
    expected = auth_type_check_sql()
    assert _check_body(User, "ck_user_auth_type_valid") == expected
    assert _check_body(UserInvitation, "ck_user_invitation_auth_type_valid") == expected


def test_every_auth_type_constant_is_supported_and_admitted_by_the_database() -> None:
    """A new ``AUTH_TYPE_X`` added without widening the CHECK fails here, not at login."""
    declared = {
        value
        for name, value in vars(constants).items()
        if name.startswith("AUTH_TYPE_") and isinstance(value, str)
    }
    assert declared, "the AUTH_TYPE_* constants must be discoverable or this proves nothing"
    assert declared <= set(VALID_AUTH_TYPES)
    assert declared <= set(DB_ADMITTED_AUTH_TYPES)


def test_the_database_set_contains_the_supported_set() -> None:
    assert set(VALID_AUTH_TYPES) <= set(DB_ADMITTED_AUTH_TYPES)
    assert len(set(DB_ADMITTED_AUTH_TYPES)) == len(DB_ADMITTED_AUTH_TYPES)


def test_the_latest_migration_that_sets_the_check_matches_the_single_source() -> None:
    """The migration allowlist is frozen history, so pin its NEWEST state to the constant."""
    setters = [
        path
        for path in sorted(_VERSIONS.glob("v*.py"), key=_revision_number)
        if "ADD CONSTRAINT ck_user_auth_type_valid" in path.read_text()
    ]
    assert setters, "no migration sets ck_user_auth_type_valid -- the scan is looking wrong"
    latest = setters[-1]
    module = _load(latest)
    assert hasattr(module, "VALID_AUTH_TYPES_SQL"), (
        f"{latest.name} now sets the CHECK without a module-level VALID_AUTH_TYPES_SQL; "
        "update this test to read the new revision's value set"
    )
    assert _LITERAL.findall(module.VALID_AUTH_TYPES_SQL) == list(DB_ADMITTED_AUTH_TYPES), (
        f"{latest.name} admits a different set than app.auth.constants.DB_ADMITTED_AUTH_TYPES"
    )


def test_no_migration_after_v380_rewrites_unlisted_auth_types_to_local() -> None:
    """The v380 repair (``UPDATE ... WHERE auth_type NOT IN (...)`` -> 'local') is closed.

    Replayed after an edition widened the CHECK with its own value, it would demote every
    delegated-auth account to a password login that has no password.
    """
    rewrite = re.compile(r"auth_type\s+NOT\s+IN", re.IGNORECASE)
    newer = [p for p in _VERSIONS.glob("v*.py") if _revision_number(p) > 380]
    assert len(newer) > 10, "the scan must actually cover the post-v380 migrations"
    offenders = [p.name for p in newer if rewrite.search(p.read_text())]
    assert offenders == [], f"destructive auth_type rewrite in {offenders}; widen additively"


def test_the_rewrite_pattern_does_fire_on_the_known_historical_offender() -> None:
    """Control for the test above: the regex matches v380, so an empty result means clean."""
    text = (_VERSIONS / "v380_oidc_identity_columns.py").read_text()
    assert re.search(r"auth_type\s+NOT\s+IN", text, re.IGNORECASE)


# ---------------------------------------------------------------- the registry side


class _Verifier:
    def verify(self, token: str, request: Any) -> None:
        return None


@pytest.fixture
def registered():
    names: list[str] = []

    def _register(auth_type: str) -> None:
        provider_registry.register_verifier(auth_type, _Verifier())
        names.append(auth_type)

    yield _register
    for name in names:
        provider_registry.unregister_verifier(name)


def test_registering_a_provider_the_core_check_does_not_admit_warns(
    registered, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=provider_registry.logger.name):
        registered("managed_idp")
    assert any(
        "managed_idp" in r.getMessage() and "ck_user_auth_type_valid" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )
    assert "managed_idp" in provider_registry.get_registered_providers()
    assert not core_admits("managed_idp")


def test_registering_a_core_auth_type_does_not_warn(
    registered, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=provider_registry.logger.name):
        registered("saml")
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def _db_with_live_check(body: str | None) -> MagicMock:
    db = MagicMock()
    db.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    db.execute.return_value.scalar.return_value = body
    return db


@pytest.fixture(autouse=True)
def _reset_confirmed():
    before = set(auth_type_admission._confirmed)
    yield
    auth_type_admission._confirmed.clear()
    auth_type_admission._confirmed.update(before)


def test_a_core_type_is_admitted_without_touching_the_database() -> None:
    db = MagicMock()
    db.get_bind.side_effect = AssertionError("a core auth type must not query the database")
    assert_auth_type_admitted(db, "oidc")


def test_a_provider_missing_from_the_live_check_is_refused_with_the_fix_named() -> None:
    db = _db_with_live_check("CHECK ((auth_type)::text = ANY (ARRAY['local'::text, 'saml'::text]))")
    with pytest.raises(ExternalProviderNotAdmittedError, match="additive migration"):
        assert_auth_type_admitted(db, "managed_idp")


def test_a_provider_the_edition_widened_the_check_for_is_admitted_and_cached() -> None:
    db = _db_with_live_check("CHECK (auth_type IN ('local', 'managed_idp'))")
    assert_auth_type_admitted(db, "managed_idp")
    db.execute.reset_mock()
    assert_auth_type_admitted(db, "managed_idp")
    db.execute.assert_not_called()


def test_a_refusal_is_not_cached_so_a_later_migration_takes_effect() -> None:
    db = _db_with_live_check("CHECK (auth_type IN ('local'))")
    with pytest.raises(ExternalProviderNotAdmittedError):
        assert_auth_type_admitted(db, "managed_idp")
    db.execute.return_value.scalar.return_value = "CHECK (auth_type IN ('local', 'managed_idp'))"
    assert_auth_type_admitted(db, "managed_idp")


def test_an_unreadable_constraint_does_not_block_a_login() -> None:
    assert_auth_type_admitted(_db_with_live_check(None), "managed_idp")


def test_external_sync_refuses_an_unadmitted_provider_before_any_write() -> None:
    db = _db_with_live_check("CHECK (auth_type IN ('local'))")
    identity = ExternalIdentity(provider="managed_idp", external_id="ext-1", email="a@example.com")
    with pytest.raises(ExternalProviderNotAdmittedError):
        sync_external_user_to_db(db, identity)
    db.add.assert_not_called()
    db.commit.assert_not_called()
