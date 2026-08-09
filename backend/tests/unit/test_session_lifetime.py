"""Session lifetime: ``refresh_token`` is the single owner of a session.

Two mechanisms used to exist and only one of them ran.
``app/auth/session.py:SessionManager`` implemented Redis-backed idle and absolute
timeouts in ~250 lines with **zero call sites**, so
``SESSION_IDLE_TIMEOUT_MINUTES`` and ``SESSION_ABSOLUTE_TIMEOUT_MINUTES`` were
configuration that changed nothing — and refresh rotation had no ceiling at all:
each rotation minted a fresh 7-day expiry, so a client that refreshed before every
expiry held a renewable session forever.

``RefreshToken`` was already the de-facto session record (concurrent-session
limits, rotation, revocation, the issue-#324 fail-closed fallback), so the
timeouts moved onto those rows and ``SessionManager`` was deleted rather than
wired up. See ``plans/session-ownership-decision.md``.

What this pins:

* the absolute ceiling is carried forward through rotation and never extended;
* an idle session is refused at the next refresh;
* a row that predates the columns (NULL in both) is **not** invalidated;
* ``SessionManager`` is gone, and the three things sharing its module are not.

Everything runs against fakes: no Postgres, no Redis, no HTTP client.
"""

# mypy: disable-error-code="arg-type,attr-defined"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.auth import token_service as token_service_module
from app.auth.token_service import TokenService
from app.core.config import settings
from app.models.refresh_token import RefreshToken

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000cc"


# ── fakes ────────────────────────────────────────────────────────────────────────


class _EmptyQuery:
    """No stored auth_config rows — the settings lookup falls through to .env."""

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return None

    def all(self):
        return []


class _FakeDB:
    """Minimal ``Session`` stand-in that records what was added."""

    def __init__(self):
        self.added: list = []
        self.commits = 0

    def query(self, model):
        return _EmptyQuery()

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        # The real Session populates server defaults here; nothing under test
        # reads one, and `id` is only used for log lines.
        obj.id = obj.id or len(self.added)


def _session_row(**overrides) -> RefreshToken:
    """A stored session row with the columns the lifetime check reads."""
    now = datetime.now(UTC)
    attrs = {
        "jti": "11111111-2222-3333-4444-555555555555",
        "expires_at": now + timedelta(days=7),
        "revoked_at": None,
        "last_activity_at": now,
        "absolute_expires_at": now + timedelta(hours=8),
    }
    attrs.update(overrides)
    return RefreshToken(**attrs)


@pytest.fixture
def service() -> TokenService:
    return TokenService()


@pytest.fixture
def timeouts(monkeypatch):
    """A deployment with a 15-minute idle cap and an 8-hour absolute cap.

    Set on ``settings`` rather than on a stored row: the fake DB holds no
    ``auth_config``, so ``DynamicAuthSettings`` falls through to the environment —
    which is the same precedence chain the real read uses.
    """
    monkeypatch.setattr(settings, "SESSION_IDLE_TIMEOUT_MINUTES", 15)
    monkeypatch.setattr(settings, "SESSION_ABSOLUTE_TIMEOUT_MINUTES", 480)


# ── the absolute ceiling is fixed at first issue ─────────────────────────────────


class TestAbsoluteCeilingIsStampedAndCarried:
    def test_a_new_session_gets_a_ceiling(self, service, timeouts):
        db = _FakeDB()
        before = datetime.now(UTC)

        _token, row = service.create_refresh_token(db, 1, USER_UUID, "user")

        assert row.absolute_expires_at is not None
        assert row.absolute_expires_at >= before + timedelta(minutes=479)
        assert row.absolute_expires_at <= datetime.now(UTC) + timedelta(minutes=480)

    def test_a_new_session_records_activity(self, service, timeouts):
        _token, row = service.create_refresh_token(_FakeDB(), 1, USER_UUID, "user")

        assert row.last_activity_at is not None
        assert datetime.now(UTC) - row.last_activity_at < timedelta(seconds=5)

    def test_an_explicit_ceiling_is_used_verbatim(self, service, timeouts):
        """This is the parameter rotation passes; it must not be recomputed."""
        ceiling = datetime.now(UTC) + timedelta(minutes=3)

        _token, row = service.create_refresh_token(
            _FakeDB(), 1, USER_UUID, "user", absolute_expires_at=ceiling
        )

        assert row.absolute_expires_at == ceiling

    def test_rotation_carries_the_ceiling_forward(self, service, timeouts, monkeypatch):
        """The whole point: rotation must not renew the session's ceiling."""
        ceiling = datetime.now(UTC) + timedelta(minutes=17)
        old = _session_row(absolute_expires_at=ceiling)
        monkeypatch.setattr(service, "revoke_token", lambda *a, **k: True)

        _new_token, new_row = service.rotate_refresh_token(
            db=_FakeDB(),
            old_token="old",
            old_token_record=old,
            user_id=1,
            user_uuid=USER_UUID,
            role="user",
        )

        assert new_row.absolute_expires_at == ceiling

    def test_repeated_rotation_never_extends_the_ceiling(self, service, timeouts, monkeypatch):
        """A client refreshing forever used to hold an indefinitely renewable session."""
        monkeypatch.setattr(service, "revoke_token", lambda *a, **k: True)
        row = _session_row()
        original = row.absolute_expires_at

        for _ in range(5):
            _token, row = service.rotate_refresh_token(
                db=_FakeDB(),
                old_token="old",
                old_token_record=row,
                user_id=1,
                user_uuid=USER_UUID,
                role="user",
            )

        assert row.absolute_expires_at == original

    def test_rotation_restamps_activity(self, service, timeouts, monkeypatch):
        """``last_activity_at`` is the half that DOES move — that is the idle clock."""
        monkeypatch.setattr(service, "revoke_token", lambda *a, **k: True)
        stale = datetime.now(UTC) - timedelta(minutes=10)

        _token, new_row = service.rotate_refresh_token(
            db=_FakeDB(),
            old_token="old",
            old_token_record=_session_row(last_activity_at=stale),
            user_id=1,
            user_uuid=USER_UUID,
            role="user",
        )

        assert new_row.last_activity_at > stale


# ── enforcement in verify_refresh_token ──────────────────────────────────────────


class TestLifetimeIsEnforced:
    def test_a_live_session_passes(self, service, timeouts):
        assert service._session_within_lifetime(_FakeDB(), _session_row()) is True

    def test_the_absolute_cap_refuses(self, service, timeouts):
        row = _session_row(absolute_expires_at=datetime.now(UTC) - timedelta(seconds=1))

        assert service._session_within_lifetime(_FakeDB(), row) is False

    def test_a_stale_refresh_is_refused(self, service, timeouts):
        """Idle timeout: nothing has presented this session inside the window."""
        row = _session_row(last_activity_at=datetime.now(UTC) - timedelta(minutes=16))

        assert service._session_within_lifetime(_FakeDB(), row) is False

    def test_activity_just_inside_the_window_passes(self, service, timeouts):
        row = _session_row(last_activity_at=datetime.now(UTC) - timedelta(minutes=14))

        assert service._session_within_lifetime(_FakeDB(), row) is True

    def test_zero_disables_the_idle_cap(self, service, monkeypatch):
        """Reachable via .env, which is unbounded; without the guard, `> 0 minutes`
        idle would refuse every session on sight."""
        monkeypatch.setattr(settings, "SESSION_IDLE_TIMEOUT_MINUTES", 0)
        row = _session_row(last_activity_at=datetime.now(UTC) - timedelta(days=30))

        assert service._session_within_lifetime(_FakeDB(), row) is True

    def test_naive_timestamps_do_not_crash_the_comparison(self, service, timeouts):
        """A naive value must produce a decision, not a TypeError inside auth."""
        row = _session_row(
            last_activity_at=(datetime.now(UTC) - timedelta(minutes=16)).replace(tzinfo=None),
            absolute_expires_at=(datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None),
        )

        assert service._session_within_lifetime(_FakeDB(), row) is False

    def test_verify_refresh_token_applies_the_check(self, service, timeouts, monkeypatch):
        """The caps are enforced on the verification path, not only in the helper."""
        row = _session_row(absolute_expires_at=datetime.now(UTC) - timedelta(seconds=1))

        monkeypatch.setattr(service, "is_token_revoked", lambda *a, **k: False)
        monkeypatch.setattr(
            service, "verify_token_with_fallback", lambda token: {"type": "refresh", "jti": "j"}
        )

        class _RowQuery(_EmptyQuery):
            def first(self):
                return row

        db = _FakeDB()
        monkeypatch.setattr(db, "query", lambda model: _RowQuery())

        assert service.verify_refresh_token(db, "any-token") == (None, None)

    def test_a_live_session_still_verifies(self, service, timeouts, monkeypatch):
        """The counterpart: the new check must not refuse a healthy session."""
        row = _session_row()
        payload = {"type": "refresh", "jti": "j"}

        monkeypatch.setattr(service, "is_token_revoked", lambda *a, **k: False)
        monkeypatch.setattr(service, "verify_token_with_fallback", lambda token: payload)

        class _RowQuery(_EmptyQuery):
            def first(self):
                return row

        db = _FakeDB()
        monkeypatch.setattr(db, "query", lambda model: _RowQuery())

        assert service.verify_refresh_token(db, "any-token") == (payload, row)


# ── legacy rows are grandfathered, not evicted ───────────────────────────────────


class TestLegacyRowsSurviveTheUpgrade:
    def test_null_columns_do_not_invalidate_a_session(self, service, timeouts):
        """Users are already signed out once this release; twice is gratuitous."""
        row = _session_row(last_activity_at=None, absolute_expires_at=None)

        assert service._session_within_lifetime(_FakeDB(), row) is True

    def test_a_null_ceiling_alone_does_not_invalidate(self, service, timeouts):
        row = _session_row(absolute_expires_at=None)

        assert service._session_within_lifetime(_FakeDB(), row) is True

    def test_a_null_activity_time_alone_does_not_invalidate(self, service, timeouts):
        """NULL means "no cap recorded", never "idle since the beginning of time"."""
        row = _session_row(last_activity_at=None)

        assert service._session_within_lifetime(_FakeDB(), row) is True

    def test_first_rotation_stamps_a_legacy_row(self, service, timeouts, monkeypatch):
        """The grandfathering is bounded: the successor row carries real caps."""
        monkeypatch.setattr(service, "revoke_token", lambda *a, **k: True)
        legacy = _session_row(last_activity_at=None, absolute_expires_at=None)

        _token, new_row = service.rotate_refresh_token(
            db=_FakeDB(),
            old_token="old",
            old_token_record=legacy,
            user_id=1,
            user_uuid=USER_UUID,
            role="user",
        )

        assert new_row.absolute_expires_at is not None
        assert new_row.last_activity_at is not None


# ── the deleted implementation stays deleted ─────────────────────────────────────


class TestSessionManagerIsGone:
    def test_the_class_cannot_be_imported(self):
        """Two owners would enforce against different session sets — see the plan."""
        with pytest.raises(ImportError):
            from app.auth.session import SessionManager  # noqa: F401

    def test_the_singleton_cannot_be_imported(self):
        with pytest.raises(ImportError):
            from app.auth.session import session_manager  # noqa: F401

    def test_the_module_exports_neither_name(self):
        from app.auth import session

        assert not hasattr(session, "SessionManager")
        assert not hasattr(session, "session_manager")

    def test_the_live_callers_in_that_module_survive(self):
        """OIDC state, the in-memory fallback and the Redis factory all have callers."""
        from app.auth.session import InMemoryStore
        from app.auth.session import OIDCStateStore
        from app.auth.session import get_redis_client
        from app.auth.session import oidc_state_store

        assert callable(get_redis_client)
        assert isinstance(oidc_state_store, OIDCStateStore)
        assert InMemoryStore().get("nothing") is None


# ── the settings are read from the database, not only from .env ──────────────────


class TestTimeoutsAreDatabaseBacked:
    def test_a_stored_value_wins_over_the_environment(self, monkeypatch):
        """The admin Session tab was inert; these two keys now drive the check."""
        monkeypatch.setattr(settings, "SESSION_IDLE_TIMEOUT_MINUTES", 15)
        monkeypatch.setattr(settings, "SESSION_ABSOLUTE_TIMEOUT_MINUTES", 480)

        stored = {"session_idle_timeout_minutes": 5, "session_absolute_timeout_minutes": 60}
        monkeypatch.setattr(
            "app.services.auth_config_service.AuthConfigService.get_effective_config",
            staticmethod(lambda db, key: stored.get(key)),
        )

        assert token_service_module._session_lifetime_minutes(_FakeDB()) == (5, 60)

    def test_an_unreadable_configuration_degrades_to_the_environment(self, monkeypatch):
        """A config read must not be able to break token issue or verification."""
        monkeypatch.setattr(settings, "SESSION_IDLE_TIMEOUT_MINUTES", 15)
        monkeypatch.setattr(settings, "SESSION_ABSOLUTE_TIMEOUT_MINUTES", 480)

        broken = SimpleNamespace()  # no .query at all

        assert token_service_module._session_lifetime_minutes(broken) == (15, 480)
