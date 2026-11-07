"""``get_optional_current_user`` applies every account-lifecycle gate.

An optional-auth route (the thumbnail fallback, ``GET /system/capabilities`` and the
capability-gate platform-staff bypass) used to resolve any active account with a
valid token, so an account the strict dependency next door refuses still received a
signed-in answer there. Such a caller is now served as anonymous: pending, rejected,
expired, and also owing a banner acknowledgment or a password change (the remedy
screens for those two use exempt strict routes, never an optional one). Each refusal
below has a control that differs in exactly the one attribute under test.
"""

# mypy: disable-error-code="arg-type"
# ``request``/``db`` are declared ``Request``/``Session`` for the production call
# sites; these are real Requests built from an ASGI scope and the real savepointed
# session, and the gate cases pass a structural stand-in user.
from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.endpoints.auth import lifecycle_gates as gates_module
from app.api.endpoints.auth.dependencies import get_optional_current_user
from app.api.endpoints.auth.lifecycle_gates import ERROR_CODE_ACCOUNT_REJECTED
from app.api.endpoints.auth.profile import session_status
from app.auth.approval import APPROVAL_PENDING
from app.auth.approval import APPROVAL_REJECTED
from app.auth.cookies import ACCESS_COOKIE
from app.auth.direct_auth import create_access_token
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.auth_config import AuthConfig
from app.models.user import User
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.xdist_group("account_approval")


def _request(token: str, path: str = "/api/files/some-uuid/thumbnail") -> Request:
    """A real ``Request`` carrying *token* in the SPA's httpOnly cookie."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "server": ("backend", 8080),
            "root_path": "",
            "path": path,
            "query_string": b"",
            "headers": [
                (b"user-agent", b"pytest"),
                (b"cookie", f"{ACCESS_COOKIE}={token}".encode()),
            ],
            "client": ("10.0.0.7", 40000),
        }
    )


def _account(db, **attrs) -> User:
    unique = uuid_pkg.uuid4().hex[:8]
    user = User(
        email=f"optional-gate-{unique}@example.com",
        full_name="Optional Gate",
        hashed_password=get_password_hash("irrelevant-Passphrase99!"),
        role="user",
        auth_type="local",
        is_active=True,
        is_superuser=False,
        **attrs,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _resolve(db, user: User) -> User | None:
    token = create_access_token({"sub": str(user.uuid), "role": str(user.role)})
    return get_optional_current_user(_request(token), db)  # type: ignore[no-any-return]


def _require_approval(db, actor, enabled: bool) -> None:
    """Flip the setting through the exact path the admin endpoint takes."""
    AuthConfigService.bulk_update_category(
        db=db,
        category="local",
        config_dict={"require_account_approval": enabled},
        user_id=actor.id,
    )


def _store_banner_enabled(db) -> None:
    row = db.query(AuthConfig).filter(AuthConfig.config_key == "login_banner_enabled").first()
    if row is None:
        row = AuthConfig(config_key="login_banner_enabled", category="local")
        db.add(row)
    row.config_value = "true"
    db.flush()


class TestApproval:
    def test_a_pending_account_is_anonymous_while_approval_is_required(
        self, db_session, super_admin_user
    ):
        _require_approval(db_session, super_admin_user, True)
        pending = _account(db_session, approval_status=APPROVAL_PENDING)

        assert _resolve(db_session, pending) is None

    def test_a_pending_account_resolves_once_the_setting_is_off(self, db_session, super_admin_user):
        """Control: turning the setting off releases pending accounts here exactly as
        it does at the strict gate."""
        _require_approval(db_session, super_admin_user, False)
        pending = _account(db_session, approval_status=APPROVAL_PENDING)

        resolved = _resolve(db_session, pending)

        assert resolved is not None and resolved.id == pending.id

    def test_a_rejected_account_is_anonymous_even_with_the_setting_off(
        self, db_session, super_admin_user
    ):
        _require_approval(db_session, super_admin_user, False)
        rejected = _account(db_session, approval_status=APPROVAL_REJECTED)

        assert _resolve(db_session, rejected) is None

    def test_an_approved_account_resolves(self, db_session, super_admin_user):
        _require_approval(db_session, super_admin_user, True)
        approved = _account(db_session)

        resolved = _resolve(db_session, approved)

        assert resolved is not None and resolved.id == approved.id

    def test_an_unreadable_setting_makes_a_pending_account_anonymous(self, db_session, monkeypatch):
        """The strict gate answers 503 here; the optional path must not read the
        outage as "approval off"."""
        pending = _account(db_session, approval_status=APPROVAL_PENDING)
        monkeypatch.setattr("app.auth.approval.approval_required", lambda db: None)

        assert _resolve(db_session, pending) is None


class TestExpiry:
    def test_an_expired_account_is_anonymous(self, db_session):
        expired = _account(db_session, account_expires_at=datetime.now(UTC) - timedelta(hours=1))

        assert _resolve(db_session, expired) is None

    def test_an_account_expiring_later_resolves(self, db_session):
        current = _account(db_session, account_expires_at=datetime.now(UTC) + timedelta(days=1))

        resolved = _resolve(db_session, current)

        assert resolved is not None and resolved.id == current.id


class TestSelfServiceStatesAreAnonymous:
    def test_an_account_owing_a_password_change_is_anonymous(self, db_session):
        owing = _account(db_session, must_change_password=True)

        assert _resolve(db_session, owing) is None

    def test_an_account_not_owing_a_password_change_resolves(self, db_session):
        """Control: the same account without the flag."""
        current = _account(db_session, must_change_password=False)

        resolved = _resolve(db_session, current)

        assert resolved is not None and resolved.id == current.id

    def test_an_account_owing_the_banner_is_anonymous(self, db_session):
        _store_banner_enabled(db_session)
        owing = _account(db_session)
        assert owing.banner_acknowledged_at is None

        assert _resolve(db_session, owing) is None

    def test_an_account_that_acknowledged_the_banner_resolves(self, db_session):
        """Control: the banner is still enabled; only the acknowledgment differs."""
        _store_banner_enabled(db_session)
        acknowledged = _account(db_session, banner_acknowledged_at=datetime.now(UTC))

        resolved = _resolve(db_session, acknowledged)

        assert resolved is not None and resolved.id == acknowledged.id

    def test_an_exempt_route_still_resolves_an_account_owing_a_password_change(self, db_session):
        """The gates are asked about the matched route, exactly as the strict path asks:
        the self-service change endpoint stays reachable while the flag is set."""
        owing = _account(db_session, must_change_password=True)
        token = create_access_token({"sub": str(owing.uuid), "role": "user"})
        request = _request(token, path=f"{settings.API_PREFIX}/users/me")

        resolved = get_optional_current_user(request, db_session)

        assert resolved is not None and resolved.id == owing.id


class TestTheSessionProbeIsUnchanged:
    def test_the_probe_still_reports_a_pending_account_as_signed_in(
        self, db_session, super_admin_user
    ):
        """The SPA reads ``approval_status`` off the probe to render the waiting
        screen; it resolves through ``get_current_user``, not the optional path."""
        _require_approval(db_session, super_admin_user, True)
        pending = _account(db_session, approval_status=APPROVAL_PENDING)
        token = create_access_token({"sub": str(pending.uuid), "role": "user"})

        body = session_status(_request(token), token=None, db=db_session)

        assert body["authenticated"] is True
        assert body["user"]["approval_status"] == APPROVAL_PENDING


class TestUnrecognisedApprovalStatus:
    """The CHECK constraint keeps such a value out of the table; the gate must not
    read one as approved if a row or object ever carries it."""

    def test_an_unrecognised_value_is_refused_like_a_rejection(self, db_session):
        user = SimpleNamespace(id=1, email="odd@example.com", approval_status="suspended")

        with pytest.raises(HTTPException) as caught:
            gates_module._enforce_approval(user, None, db_session)

        assert caught.value.status_code == 403
        assert cast(dict, caught.value.detail)["code"] == ERROR_CODE_ACCOUNT_REJECTED

    def test_a_missing_value_still_passes(self, db_session):
        """Control: an object without the column (the TESTING stand-in user) is not
        refused, matching ``is_pending``'s documented fail-safe read."""
        user = SimpleNamespace(id=1, email="bare@example.com", approval_status=None)

        assert gates_module._approval_refusal(user, db_session) is None


class TestNoGateIsLeftOut:
    """A gate added to the strict dependency must be asked on the optional path too."""

    #: Each gate ``get_current_active_user`` enforces, and the call through which
    #: ``admitted_on_optional_auth`` asks the same question without raising.
    GATE_PREDICATES = {
        "_enforce_proxy_identity_consistency": "_enforce_proxy_identity_consistency",
        "_enforce_approval": "_approval_refusal",
        "_enforce_account_expiry": "_expired_at",
        "_enforce_banner_acknowledgment": "_banner_refusal_reason",
        "_enforce_password_change": "_password_change_owed",
    }

    @staticmethod
    def _called_names(function) -> set[str]:
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        return {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

    def test_every_strict_gate_is_mapped(self):
        from app.api.endpoints.auth.dependencies import get_current_active_user

        enforced = {
            name
            for name in self._called_names(get_current_active_user)
            if name.startswith("_enforce_")
        }

        assert enforced == set(self.GATE_PREDICATES)

    def test_the_optional_path_asks_every_mapped_gate(self):
        asked = self._called_names(gates_module.admitted_on_optional_auth)

        missing = {gate for gate, call in self.GATE_PREDICATES.items() if call not in asked}

        assert missing == set()
