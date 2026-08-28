"""Privilege-gate behaviour that no test asserted (issue #446, mutation survivors).

Written from the 175 surviving mutants of ``app/api/endpoints/auth/dependencies.py``
(measured 2026-08-14, 94% coverage of the module by ``MODULE_TESTS[dependencies]``).
This module holds the privilege gates every authenticated route passes through
(``get_current_user``, ``get_current_active_user``, ``get_current_admin_user``,
``get_current_active_superuser``, ``get_optional_current_user`` and the lifecycle
gates they chain through), so the classification below is biased hard toward
``real`` — an inverted role/predicate check here is privilege escalation, not a
missing assertion.

Full triage of the 175, corrected by MEASURING (re-run three times: three mutants —
``_banner_requirement``'s ``or``->``and``, and the two ``accepted_algorithms(None)``
sibling mutants in ``get_current_user``/``get_optional_current_user`` — were first
classified "real" and given tests that did not kill them on re-measurement, each
investigated to a genuine equivalence proof below rather than left as a silent pass;
the exact trap the lockout pass's own docstring warns about. A fourth pair,
``algorithms=None`` hardcoded/dropped, stayed real but needed its ORIGINAL test
rewritten — see ``TestGetCurrentUserAlgorithmAllowlistIsEnforced`` — after tracing the
actual joserfc exception showed the first version's premise about which direction the
gap runs was wrong):

**+4 real, added 2026-08-28.** A from-scratch re-run (the prior log predated the
completion+source-binding evidence ``scripts/mutation-baselines.tsv``'s header now
requires, so it counted as NOT MEASURED) found 83 survivors against this row's 79 —
commit 1b536070 had added ``"trigger": "fixed_date"`` to ``_enforce_account_expiry``'s
audit ``details`` with no test alongside it. See
``TestEnforceAccountExpiry::test_the_audit_details_are_tagged_with_the_fixed_date_trigger``.
The other 78 were confirmed byte-identical to this triage (the intervening diff touches
only that one ``details=`` literal), bringing the total real count to 100.

* **100 real** — this file targets all 100. Two families dominate:

  - **Response-body text.** Every ``HTTPException`` this module raises carries a
    machine-readable ``detail`` dict (``code``/``message``, sometimes ``reason``)
    that the SPA renders or branches on, plus a ``headers`` dict for the 401s. The
    existing suites (``test_account_lifecycle.py``, ``test_account_approval.py``,
    ``test_banner_acknowledgment.py``, ``test_external_token_auth.py``, …) checked
    ``detail["code"]`` or a truthy ``detail["message"]``, never the exact string,
    dict key, or headers — so a reworded/miscapitalized message, a renamed key
    (``"message"`` -> ``"XXmessageXX"``), or a dropped/mistyped ``WWW-Authenticate``
    header all survived. This file asserts full dict/headers equality at every
    raise site, which kills the whole family in one assertion each.
  - **Predicates, boundaries and dropped/renamed arguments** that change real,
    observable behaviour: the banner-acknowledgment tzinfo-normalization guard and
    its ``>=``/``>`` expiry boundary, the account-expiry ``<``/``<=`` boundary, the
    JWT ``algorithms=`` allowlist actually reaching ``jwt.decode`` (see
    ``TestGetCurrentUserAlgorithmAllowlistIsEnforced`` for why the first version of
    this test was wrong about WHICH direction the gap runs), the httpOnly-cookie
    fallback in ``get_current_user``, ``request.state.user_id``/``org_id`` being
    stamped from the resolved user (not dropped or misspelled), ``_enforce_approval``'s
    ``db`` argument reaching ``approval_required`` (not silently replaced with
    ``None``, which trades a fresh per-request DB read for a process-level cache),
    ``verify_external_token`` actually receiving the real ``request`` (trusted-proxy
    checks inside a verifier need it), the TESTING-only mock-user's fields, and
    ``_route_path``'s exact stripped-character-set and length boundary (it is the
    fail-safe input to every exempt-path check).

* **52 noise** — a log string (``logger.debug``/``warning``/``info``/``error``/
  ``exception``) no caller observes, or a condition/argument that feeds *only* a
  log call:

  - ``_authenticate_external_token``'s ``logger.exception`` text (4),
    ``_banner_requirement``'s config-read-failure ``logger.debug`` (8),
    ``_enforce_banner_acknowledgment``'s pre-403 ``logger.debug`` (9),
    ``_lifecycle_client_info``'s failure ``logger.debug`` (7), and
    ``get_current_user``'s revoked-token/role-mismatch/DB-error/mock-user
    ``logger.*`` calls (14) — all pure log text.
  - ``get_current_user``'s ``user_role = payload.get("role")`` and its
    ``if user_role and user.role != user_role:`` guard (6 mutants: dropping/renaming
    the claim read, and flipping ``and``/``or``/``==``) gate **only** the
    ``logger.warning`` two lines below; the role used everywhere else is always
    ``user.role`` from the DB (the comment above it says so: "Database is the
    source of truth for roles"). No branch of this predicate changes what user or
    role is returned.
  - ``_enforce_proxy_identity_consistency``'s ``revoke_all_sessions(..., reason=...)``
    (3): ``account_security_service.revoke_all_sessions`` only ever reads ``reason``
    inside its own ``logger.error``/``logger.info`` calls — it is never persisted,
    never returned, never part of the audit ``details`` dict this module builds
    separately. Confirmed by reading ``revoke_all_sessions``'s body.

* **27 equivalent**, six proofs (the first two below cover the mutants that
  survived their own first tests):

  - **``_banner_requirement``'s ``db is None or not hasattr(db, "query")`` -> ``and``**
    (the mutant that survived its first test): ``hasattr(None, "query")`` is
    already ``False``, so ``db is None`` is a strict subset of the second disjunct
    and never adds a reachable case. See
    ``TestBannerRequirementFallsBackOnAnUnusableSession`` for the full three-case
    proof (the enclosing ``try/except Exception`` is what closes the gap for the
    "db present, no ``.query``" case).
  - **``accepted_algorithms(TOKEN_TYPE_ACCESS)`` -> ``accepted_algorithms(None)``**,
    once each in ``get_current_user`` and ``get_optional_current_user`` (the pair
    that survived their first tests, alongside ``_banner_requirement`` above):
    ``core.security.signing_algorithm`` does ``del token_type`` and returns
    ``settings.JWT_ALGORITHM`` unconditionally, so the *argument* to
    ``accepted_algorithms`` never reaches anything that branches on it — confirmed
    by the ``security`` module's own baseline note (measured: identical
    ``["HS256", "HS512"]``-shaped list either way). This is a different mutation
    from the ``algorithms=None`` ones above (those bypass the call to
    ``accepted_algorithms`` entirely; these still call it, just with the wrong
    argument, which happens not to matter).

  - **18 are ``getattr(<user-or-external_user>, "<mapped column>", DEFAULT)`` where
    only ``DEFAULT`` is mutated** (dropped, ``None``, or a sentinel string) on an
    attribute that is a real SQLAlchemy-mapped column of ``User``. Every call site
    in this module passes the FastAPI-resolved dependency chain rooted at
    ``get_current_user`` — always a genuine ``User`` ORM instance, including the
    TESTING-only fabricated one (``User(uuid=..., email=..., is_active=True,
    is_superuser=False)``), never a bare namespace. Verified empirically:
    ``getattr(User(), "<any mapped column>", "SENTINEL")`` returns ``None``, not
    ``"SENTINEL"`` — SQLAlchemy's instrumented attribute descriptor returns ``None``
    for an unset mapped column rather than raising, so the attribute is *never*
    absent and the ``default`` argument is dead code regardless of its value. Also
    covers ``_enforce_approval``'s own ``str(getattr(user, "approval_status", ""))``
    in its audit ``details`` dict: reaching that line already required
    ``is_pending(user)``/``is_rejected(user)`` to read ``approval_status`` as
    ``"pending"``/``"rejected"`` (their *own* getattr, same attribute, same object),
    so by the time this second getattr runs the attribute is provably present.
  - **``_route_path``'s inner default is neutralised by its own outer ``or ""``**:
    ``getattr(route, "path", None) or getattr(getattr(request, "url", None), "path",
    "") or ""`` — changing the middle getattr's default from ``""`` to ``None``
    cannot matter, because *both* are falsy and the trailing ``or ""`` catches
    either one identically.
  - **3 are case-only variants of the ``TESTING`` env fallback**, neutralised by
    ``.lower()``: ``os.environ.get("TESTING", "False")`` vs ``"false"``/``"FALSE"``
    all lower to ``"false"`` and compare unequal to ``"true"`` the same way. (The
    ``None``-default sibling is NOT equivalent — ``None.lower()`` raises, which is
    real and tested below.)
  - **2 are ``TokenPayload(sub=..., jti=token_jti)`` -> ``jti=None``/dropped**:
    ``token_data.jti`` is constructed and never read anywhere in this module (only
    ``token_data.sub`` is), and the schema field is already ``str | None = None``,
    so neither mutation changes any observable output.

No production bug was found in this module: every real gap above is existing,
correct behaviour that lacked an assertion, not a defect. Contrast with
``lockout``/``tasks``, where the same process found real bugs.
"""

# mypy: disable-error-code="arg-type,comparison-overlap,index"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures declaring Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object -- the same shape test_lockout_survivor_mutants.py and
# test_banner_acknowledgment.py suppress for the same reason. Declared once here
# rather than as a cast at every call site.
from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.endpoints.auth import dependencies as deps_module
from app.api.endpoints.auth.dependencies import ERROR_CODE_ACCOUNT_EXPIRED
from app.api.endpoints.auth.dependencies import ERROR_CODE_ACCOUNT_PENDING_APPROVAL
from app.api.endpoints.auth.dependencies import ERROR_CODE_ACCOUNT_REJECTED
from app.api.endpoints.auth.dependencies import ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED
from app.api.endpoints.auth.dependencies import ERROR_CODE_PASSWORD_CHANGE_REQUIRED
from app.api.endpoints.auth.dependencies import ERROR_CODE_PROXY_IDENTITY_MISMATCH
from app.api.endpoints.auth.dependencies import get_current_active_user
from app.api.endpoints.auth.dependencies import get_current_admin_user
from app.api.endpoints.auth.dependencies import get_current_user
from app.api.endpoints.auth.dependencies import get_optional_current_user
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.direct_auth import create_access_token
from app.auth.provider_registry import ExternalIdentity
from app.auth.provider_registry import has_verifiers
from app.auth.provider_registry import register_verifier
from app.auth.provider_registry import unregister_verifier
from app.core.config import settings

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000ee"
CLIENT_IP = "198.51.100.7"
USER_AGENT = "pytest-survivor-suite"
ORDINARY_PATH = f"{settings.API_PREFIX}/files"
PROVIDER = "oidc"


def _user(**overrides: Any) -> Any:
    attrs: dict[str, Any] = {
        "id": 9001,
        "uuid": UUID(USER_UUID),
        "email": "survivor.person@example.com",
        "role": "user",
        "auth_type": "local",
        "is_active": True,
        "must_change_password": False,
        "account_expires_at": None,
        "banner_acknowledged_at": None,
        "approval_status": "approved",
        "external_org_id": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def _request(path: str = ORDINARY_PATH, *, matched_route: bool = True) -> Any:
    """A Request stand-in carrying a matched route, as Starlette would."""
    return SimpleNamespace(
        scope={"route": SimpleNamespace(path=path)} if matched_route else {},
        url=SimpleNamespace(path=path),
        client=SimpleNamespace(host=CLIENT_IP),
        headers={"User-Agent": USER_AGENT},
        state=SimpleNamespace(),
        cookies={},
    )


def _real_request(*, path: str = "/api/files", headers: list | None = None) -> Request:
    """A real Starlette ``Request``, for paths that read ``request.headers``/state."""
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
            "headers": headers or [(b"user-agent", b"pytest")],
            "client": ("10.0.0.9", 40000),
        }
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    """This module's community build registers no external verifiers."""
    unregister_verifier(PROVIDER)
    yield
    unregister_verifier(PROVIDER)


# ── _audit_lifecycle_denial: the shared audit-record builder ─────────────────────


class TestAuditLifecycleDenialUsernameFallback:
    def test_a_falsy_email_is_recorded_as_an_empty_string_not_a_sentinel(self, monkeypatch):
        """``str(getattr(user, "email", "") or "")`` — the trailing fallback must
        stay ``""``, not a sentinel like ``"XXXX"``. Unreachable for any real,
        persisted ``User`` (``email`` is a ``NOT NULL`` column set at registration),
        but the audit record must still degrade safely rather than fabricate an
        identity string for the rare/defensive case where it is falsy.
        """
        recorded: list[dict] = []
        monkeypatch.setattr(deps_module.audit_logger, "log", lambda **kw: recorded.append(kw))
        user = _user(email="")

        deps_module._audit_lifecycle_denial(
            deps_module.AuditEventType.AUTH_ACCOUNT_EXPIRED,
            user,
            _request(),
            error_code="ACCOUNT_EXPIRED",
            details={},
        )

        assert len(recorded) == 1
        assert recorded[0]["username"] == ""


# ── _route_path: the fail-safe input to every exempt-path check ──────────────────


class TestRoutePathStripsOnlyATrailingSlash:
    def test_a_path_ending_in_x_is_not_stripped(self):
        """``rstrip("/")``, not a wider character class.

        A mutant widening the strip set to ``"XX/XX"`` would also strip trailing
        ``X`` characters — invisible on any real route template, but this function's
        whole job is matching untrusted/edge-case paths against the exempt sets, and
        a widened strip set is a real, if narrow, matching bug.
        """
        request = _request("/api/userX")

        assert deps_module._route_path(request) == "/api/userX"

    def test_a_two_character_path_of_only_slashes_is_fully_stripped(self):
        """``len(path) > 1``, not ``> 2``: the boundary a two-character path sits on.

        ``"//"`` has length 2. Under the real guard that is already ``> 1``, so it
        is stripped to ``""``. A mutant guard of ``> 2`` would leave it as ``"//"``
        instead — the smallest input where the two guards disagree.
        """
        request = _request("//")

        assert deps_module._route_path(request) == ""


# ── _banner_requirement: the fallback that keeps a broken session non-fatal ──────


class TestBannerRequirementFallsBackOnAnUnusableSession:
    def test_a_non_none_session_without_query_still_falls_back(self, monkeypatch):
        """A caller can hand in something that is not ``None`` but still cannot
        query (a broken/half-initialised session stand-in); the function must still
        degrade to the ``.env`` value rather than raising.

        This does NOT kill ``x__banner_requirement__mutmut_1`` (``or`` -> ``and``
        in the guard) — confirmed EQUIVALENT by ``--verify`` after this test was
        written and still reported SURVIVED. Proof: ``hasattr(None, "query")`` is
        already ``False``, so ``db is None`` is a strict subset of
        ``not hasattr(db, "query")`` — the first disjunct never adds a case the
        second does not already cover. Three cases, all identical either way:
        ``db is None`` -> both forms are ``True`` -> fallback. ``db`` present but
        no ``.query`` -> the ``or`` form is ``True`` (immediate fallback); the
        ``and`` form is ``False``, but the enclosing ``try/except Exception`` then
        catches the ``AttributeError`` from calling the missing ``.query`` and
        returns the SAME fallback. ``db`` present with ``.query`` -> both forms are
        ``False`` -> both proceed to the real read. No object can separate the two
        forms, so this is a floor, not a gap; kept as a correctness test, not a
        mutant-killing one.
        """
        monkeypatch.setattr(settings, "LOGIN_BANNER_ENABLED", True)
        no_query = SimpleNamespace()  # deliberately no .query attribute

        enabled, changed_at = deps_module._banner_requirement(no_query)

        assert enabled is True
        assert changed_at is None


# ── _enforce_account_expiry: the boundary and the response body ──────────────────


class TestEnforceAccountExpiry:
    def test_the_exact_expiry_instant_is_already_treated_as_expired(self):
        """``datetime.now(UTC) < expires_at`` — not ``<=``.

        The early-return guard only lets the caller through while ``now`` is
        STRICTLY before ``expires_at``; at the exact instant they are equal, ``<``
        is ``False`` and the account is refused. The ``<=`` mutant would instead
        return (allow access) at that exact instant — one instant later than the
        real code permits.
        """
        frozen = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ARG003
                return frozen

        user = _user(account_expires_at=frozen)
        import unittest.mock as mock

        with mock.patch.object(deps_module, "datetime", _Frozen):
            with pytest.raises(HTTPException) as exc:
                deps_module._enforce_account_expiry(user, _request())

        assert exc.value.status_code == 403

    def test_the_response_body_is_exactly_this_shape(self):
        expires_at = datetime(2024, 6, 15, tzinfo=UTC)
        user = _user(account_expires_at=expires_at)

        with pytest.raises(HTTPException) as exc:
            deps_module._enforce_account_expiry(user, _request())

        assert exc.value.status_code == 403
        assert exc.value.detail == {
            "code": ERROR_CODE_ACCOUNT_EXPIRED,
            "message": (
                "This account expired on 2024-06-15. Contact an administrator to extend it."
            ),
        }

    def test_the_audit_details_are_tagged_with_the_fixed_date_trigger(self, monkeypatch):
        """``details={"trigger": "fixed_date", ...}`` on the ``AUTH_ACCOUNT_EXPIRED`` audit call.

        ``account_lifecycle_service.py``'s background inactivity sweep emits the SAME
        event type with ``details.trigger == "inactivity"`` (or
        ``"inactivity_skipped_super_admin"``). Per that module's own docstring, a
        FedRAMP AU-2 reader "must not have to string-match ``details.trigger`` to tell
        the two apart" — so a renamed key or reworded value here is not cosmetic, it
        breaks the one field that disambiguates a per-request refusal from a
        background sweep in the audit trail. Without this assertion, mutating
        ``"trigger"`` -> ``"XXtriggerXX"`` or ``"fixed_date"`` -> ``"FIXED_DATE"``
        survived (issue #446 follow-up: introduced by the trigger key landing in
        1b536070 without a test alongside it).
        """
        recorded: list[dict] = []
        monkeypatch.setattr(deps_module.audit_logger, "log", lambda **kw: recorded.append(kw))
        expires_at = datetime(2024, 6, 15, tzinfo=UTC)
        user = _user(account_expires_at=expires_at)

        with pytest.raises(HTTPException):
            deps_module._enforce_account_expiry(user, _request())

        assert len(recorded) == 1
        assert recorded[0]["details"] == {
            "trigger": "fixed_date",
            "expired_at": expires_at.isoformat(),
            "path": ORDINARY_PATH,
        }


# ── _enforce_approval: both refusal shapes, and the db argument ──────────────────


class TestEnforceApprovalResponseBodies:
    def test_a_rejected_account_gets_exactly_this_body(self, db_session):
        user = _user(approval_status="rejected")

        with pytest.raises(HTTPException) as exc:
            deps_module._enforce_approval(user, _request(), db_session)

        assert exc.value.status_code == 403
        assert exc.value.detail == {
            "code": ERROR_CODE_ACCOUNT_REJECTED,
            "message": "This account was not approved. Contact an administrator.",
        }

    def test_a_pending_account_gets_exactly_this_body(self, db_session, monkeypatch):
        from app.auth import approval as approval_module

        monkeypatch.setattr(approval_module, "approval_required", lambda db: True)
        user = _user(approval_status="pending")

        with pytest.raises(HTTPException) as exc:
            deps_module._enforce_approval(user, _request(), db_session)

        assert exc.value.status_code == 403
        assert exc.value.detail == {
            "code": ERROR_CODE_ACCOUNT_PENDING_APPROVAL,
            "message": "This account is awaiting administrator approval.",
        }


class TestEnforceApprovalPassesTheRealSession:
    def test_approval_required_receives_the_callers_db_not_none(self, db_session, monkeypatch):
        """A dropped ``db`` argument would make ``approval_required`` fall back to
        the process-level cache instead of a fresh per-request DB read — the two
        can disagree in the window right after an admin flips the setting.
        """
        from app.auth import approval as approval_module

        received: list[object] = []

        def _spy(db):
            received.append(db)
            return True

        monkeypatch.setattr(approval_module, "approval_required", _spy)
        user = _user(approval_status="pending")

        with pytest.raises(HTTPException):
            deps_module._enforce_approval(user, _request(), db_session)

        assert received == [db_session]

    def test_get_current_active_user_passes_its_own_db_to_enforce_approval(
        self, db_session, monkeypatch
    ):
        received: list[object] = []

        def _spy(user, request, db):
            received.append(db)

        monkeypatch.setattr(deps_module, "_enforce_approval", _spy)
        user = _user()

        get_current_active_user(request=_request(), current_user=user, db=db_session)

        assert received == [db_session]


# ── _enforce_banner_acknowledgment: normalization, boundary, response body ───────


class _ConfigRow(SimpleNamespace):
    pass


class _ConfigQuery:
    def __init__(self, rows: list):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


class _FakeBannerDB:
    def __init__(self, rows: list):
        self.rows = rows

    def query(self, model):
        return _ConfigQuery(self.rows)


def _banner_rows(text_updated_at: datetime) -> list:
    return [
        _ConfigRow(
            config_key="login_banner_enabled", config_value="true", updated_at=datetime.now(UTC)
        ),
        _ConfigRow(
            config_key="login_banner_text", config_value="ATTENTION", updated_at=text_updated_at
        ),
    ]


class TestBannerAcknowledgmentNaiveTextTimestamp:
    def test_a_naive_text_change_timestamp_does_not_crash_the_compare(self):
        """``changed_at.tzinfo is None`` -> normalise to UTC — not the inverse guard,
        and not dropped to ``None``/left naive.

        The stored ``login_banner_text`` row's ``updated_at`` can come back naive
        (a write that did not set tzinfo). The comparison two lines later is against
        an already-aware ``acknowledged_at``; a naive ``changed_at`` reaching it
        raises ``TypeError`` — a 500 on every authenticated request, not a 403.
        """
        naive_changed_at = datetime(2026, 1, 1, 12, 0)  # deliberately no tzinfo
        user = _user(banner_acknowledged_at=datetime(2025, 1, 1, tzinfo=UTC))
        db = _FakeBannerDB(_banner_rows(naive_changed_at))

        with pytest.raises(HTTPException) as exc:
            deps_module._enforce_banner_acknowledgment(
                user, _request(), db
            )  # must not raise TypeError

        assert exc.value.detail["reason"] == "banner_text_changed"

    def test_an_acknowledgment_after_a_naive_text_change_still_passes(self):
        """The other half: normalise correctly enough that a FRESH ack clears it."""
        naive_changed_at = datetime(2026, 1, 1, 12, 0)
        user = _user(banner_acknowledged_at=datetime(2026, 1, 2, tzinfo=UTC))
        db = _FakeBannerDB(_banner_rows(naive_changed_at))

        refused = False
        try:
            deps_module._enforce_banner_acknowledgment(user, _request(), db)
        except HTTPException:
            refused = True
        assert refused is False, "a fresh acknowledgment must not be refused"


class TestBannerAcknowledgmentExpiryBoundary:
    def test_an_acknowledgment_at_the_exact_text_change_instant_still_passes(self):
        """``acknowledged_at >= changed_at`` — not ``>``.

        Acknowledging at the exact instant the text changed must count: the ``>``
        mutant would treat that instant as stale and demand a second click.
        """
        instant = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        user = _user(banner_acknowledged_at=instant)
        db = _FakeBannerDB(_banner_rows(instant))

        refused = False
        try:
            deps_module._enforce_banner_acknowledgment(user, _request(), db)
        except HTTPException:
            refused = True
        assert refused is False, "the exact expiry instant must not be treated as stale"


class TestBannerAcknowledgmentResponseBody:
    def test_the_response_body_is_exactly_this_shape(self):
        user = _user(banner_acknowledged_at=None)
        db = _FakeBannerDB(_banner_rows(datetime.now(UTC) - timedelta(days=1)))

        with pytest.raises(HTTPException) as exc:
            deps_module._enforce_banner_acknowledgment(user, _request(), db)

        assert exc.value.status_code == 403
        assert exc.value.detail == {
            "code": ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED,
            "message": "You must acknowledge the login banner before continuing.",
            "reason": "never_acknowledged",
        }


# ── _enforce_password_change: the response body ───────────────────────────────────


class TestEnforcePasswordChangeResponseBody:
    def test_the_response_body_is_exactly_this_shape(self):
        user = _user(must_change_password=True)

        with pytest.raises(HTTPException) as exc:
            deps_module._enforce_password_change(user, _request())

        assert exc.value.status_code == 403
        assert exc.value.detail == {
            "code": ERROR_CODE_PASSWORD_CHANGE_REQUIRED,
            "message": "You must change your password before continuing.",
        }


# ── _enforce_proxy_identity_consistency: the response body ───────────────────────


class TestEnforceProxyIdentityMismatchResponseBody:
    def test_the_response_body_is_exactly_this_shape(self, db_session, monkeypatch):
        from app.auth.constants import AUTH_TYPE_PROXY
        from app.core.auth_settings import publish_process_auth_setting

        publish_process_auth_setting("proxy_enabled", True)
        publish_process_auth_setting("proxy_email_header", "X-Remote-User")
        publish_process_auth_setting("proxy_trusted_proxies", "10.0.0.0/8")
        monkeypatch.setattr(
            "app.services.account_security_service.revoke_all_sessions", lambda *a, **kw: 1
        )
        monkeypatch.setattr(db_session, "commit", lambda: None)
        user = _user(auth_type=AUTH_TYPE_PROXY, email="real@example.com")
        request = _real_request(headers=[(b"x-remote-user", b"someone-else@example.com")])
        request.scope["client"] = ("10.0.0.9", 40000)

        with pytest.raises(HTTPException) as exc:
            deps_module._enforce_proxy_identity_consistency(user, request, db_session)

        assert exc.value.status_code == 401
        assert exc.value.detail == {
            "code": ERROR_CODE_PROXY_IDENTITY_MISMATCH,
            "message": "Identity mismatch. Please sign in again.",
        }


# ── get_current_active_user: the 400 for a deactivated account ───────────────────


class TestGetCurrentActiveUserInactiveResponseBody:
    def test_the_response_body_is_exactly_this_shape(self):
        user = _user(is_active=False)

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(request=_request(), current_user=user, db=None)

        assert exc.value.status_code == 400
        assert exc.value.detail == "Inactive user"


# ── get_current_admin_user: the 403 response body ─────────────────────────────────


class TestGetCurrentAdminUserResponseBody:
    def test_the_response_body_is_exactly_this_string(self):
        user = _user(role="user")

        with pytest.raises(HTTPException) as exc:
            get_current_admin_user(current_user=user)

        assert exc.value.status_code == 403
        assert exc.value.detail == "Not enough permissions"


# ── get_current_user ──────────────────────────────────────────────────────────────


def _access_token(**claim_overrides: Any) -> str:
    claims = {"sub": USER_UUID, "role": "user"}
    claims.update(claim_overrides)
    return create_access_token(claims)


class _NoDBAccess:
    """A ``Session`` stand-in that must never be reached (no-token/bad-token paths)."""

    def query(self, *a, **kw):  # pragma: no cover - defensive
        raise AssertionError("must not reach the database before a token is present")


class TestGetCurrentUserNoTokenResponseBody:
    def test_the_response_body_and_headers_are_exactly_this_shape(self):
        request = _request()
        request.cookies = {}

        with pytest.raises(HTTPException) as exc:
            get_current_user(request=request, token=None, db=_NoDBAccess())

        assert exc.value.status_code == 401
        assert exc.value.detail == "Could not validate credentials"
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


def _persist_user(db_session, **overrides: Any):
    """A real, committed-shape ``User`` row -- so a false ACCEPT is observable as
    a returned user object, not just "still 401s" (which a broken path can also
    produce, masking the very gap the test exists to catch)."""
    from app.models.user import User

    attrs: dict[str, Any] = {
        "uuid": uuid4(),
        "email": f"survivor-{uuid4().hex[:8]}@example.com",
        "hashed_password": "x",
        "role": "user",
        "is_active": True,
    }
    attrs.update(overrides)
    user = User(**attrs)
    db_session.add(user)
    db_session.flush()
    return user


class TestGetCurrentUserCookieFallback:
    def test_the_httponly_cookie_is_used_when_no_bearer_token_is_given(self, db_session):
        """``token = get_access_token_from_cookie(request)`` — not dropped to ``None``.

        The browser SPA authenticates entirely via the httpOnly cookie; dropping
        this fallback would break every cookie-only request while leaving
        Bearer-header clients (Swagger, API scripts) unaffected. A real, persisted
        user is used so success is unambiguous: both the real code (cookie read,
        user found) and a dropped fallback (no token at all) end up at a 401 with
        the SAME "Could not validate credentials" text on THIS UUID's failure
        mode, so asserting only the exception would pass either way. Asserting a
        successful return is the only signal that actually distinguishes them.
        """
        user = _persist_user(db_session)
        token = create_access_token({"sub": str(user.uuid), "role": "user"})
        request = _request()
        request.cookies = {"access_token": token}

        result = get_current_user(request=request, token=None, db=db_session)

        assert result.id == user.id


class TestGetCurrentUserCredentialsExceptionResponseBody:
    def test_a_malformed_token_gets_exactly_this_body_and_headers(self, db_session):
        with pytest.raises(HTTPException) as exc:
            get_current_user(request=_request(), token="not-a-real-jwt", db=db_session)

        assert exc.value.status_code == 401
        assert exc.value.detail == "Could not validate credentials"
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


class TestGetCurrentUserAlgorithmAllowlistIsEnforced:
    """``algorithms=accepted_algorithms(...)`` must reach ``jwt.decode`` unaltered.

    Corrected mid-triage by MEASURING rather than trusting the diff (the trap the
    handoff warns about): the first version of this test assumed ``algorithms=None``
    means joserfc accepts *any* registered algorithm, and used an HS512 token as the
    "disallowed" one. That token was rejected under BOTH the real code and the
    simulated mutation — but for an unrelated reason, discovered by tracing the
    actual exception: joserfc's ``JWSRegistry.get_alg`` is ``if self.allowed: check
    membership; else: check membership in ``self.recommended`` (``["HS256",
    "RS256", "ES256"]`` — hardcoded in the library, not this app). So
    ``algorithms=None`` does not open the door, it narrows it to that fixed
    three-algorithm set — which happens to already reject HS512 on its own,
    independent of ``accepted_algorithms()``. Confirmed by reading
    ``JWSRegistry.get_alg``'s source and reproducing the exact
    ``UnsupportedAlgorithmError: ... is not recommended`` in isolation.

    The REAL, observable difference is therefore the opposite direction: a
    deployment that has migrated to a NON-recommended signing algorithm (this
    app's own documented FIPS-migration route is exactly ``JWT_ALGORITHM=HS512``,
    per ``core/security.accepted_algorithms``'s docstring) would have its
    legitimately-signed HS512 tokens WRONGLY REJECTED once ``algorithms=None``
    replaces the real allowlist — an availability regression on the very
    deployments FIPS migration is for, not a downgrade-to-weaker-algorithm hole.
    """

    def test_a_non_recommended_but_configured_algorithm_is_still_accepted(
        self, db_session, monkeypatch
    ):
        """HS512 is not in joserfc's own hardcoded ``recommended`` set, so it is
        the algorithm that distinguishes "the real allowlist reached jwt.decode"
        from "jwt.decode fell back to joserfc's own three recommended algorithms".
        A real, persisted user is required: with an unknown ``sub`` both the real
        code (accepts, then finds no user) and the mutant (falls back to
        ``recommended``, rejects at decode) land on the same 401.
        """
        from tests.jwt_compat import jwt as compat_jwt

        user = _persist_user(db_session)
        monkeypatch.setattr(deps_module, "accepted_algorithms", lambda token_type: ["HS512"])
        now = datetime.now(UTC)
        token = compat_jwt.encode(
            {
                "sub": str(user.uuid),
                "role": "user",
                "type": TOKEN_TYPE_ACCESS,
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            settings.JWT_SECRET_KEY,
            algorithm="HS512",
        )

        result = get_current_user(request=_request(), token=token, db=db_session)

        assert result.id == user.id

    def test_an_algorithm_outside_the_configured_allowlist_is_rejected(
        self, db_session, monkeypatch
    ):
        """The control, using two algorithms that ARE both in joserfc's
        ``recommended`` set (so this direction cannot be satisfied by a mutant
        that silently widens/narrows to that fixed set): the allowlist is
        ``["ES256"]`` only, and the token is signed with the recommended-but-not-
        configured ``HS256``.
        """
        from tests.jwt_compat import jwt as compat_jwt

        user = _persist_user(db_session)
        monkeypatch.setattr(deps_module, "accepted_algorithms", lambda token_type: ["ES256"])
        now = datetime.now(UTC)
        token = compat_jwt.encode(
            {
                "sub": str(user.uuid),
                "role": "user",
                "type": TOKEN_TYPE_ACCESS,
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            settings.JWT_SECRET_KEY,
            algorithm="HS256",
        )

        with pytest.raises(HTTPException) as exc:
            get_current_user(request=_request(), token=token, db=db_session)

        assert exc.value.status_code == 401


class TestGetCurrentUserInactiveResponseBody:
    def test_the_response_body_is_exactly_this_string(self, db_session):
        from app.models.user import User

        user = User(
            uuid=uuid4(),
            email=f"inactive-{uuid4().hex[:8]}@example.com",
            hashed_password="x",
            role="user",
            is_active=False,
        )
        db_session.add(user)
        db_session.flush()
        token = create_access_token({"sub": str(user.uuid), "role": "user"})

        with pytest.raises(HTTPException) as exc:
            get_current_user(request=_request(), token=token, db=db_session)

        assert exc.value.status_code == 400
        assert exc.value.detail == "Inactive user"


class TestGetCurrentUserRequestStateIsStamped:
    def test_user_id_and_org_id_are_stamped_from_the_resolved_user(self, db_session):
        """``request.state.user_id``/``org_id`` feed the access log and org-scoping
        (``deps_context``). Dropping either to ``None``, or misreading the wrong
        attribute name, breaks both silently -- there is no HTTP-visible symptom.
        """
        from app.models.user import User

        user = User(
            uuid=uuid4(),
            email=f"stamped-{uuid4().hex[:8]}@example.com",
            hashed_password="x",
            role="user",
            is_active=True,
            external_org_id="org_abc123",
        )
        db_session.add(user)
        db_session.flush()
        token = create_access_token({"sub": str(user.uuid), "role": "user"})
        request = _request()

        result = get_current_user(request=request, token=token, db=db_session)

        assert result.id == user.id
        assert request.state.user_id == user.id
        assert request.state.org_id == "org_abc123"


class TestGetCurrentUserTestingFallback:
    """The DB-error / TESTING-shortcut branch: a fabricated user, gated on both
    ``TESTING`` and ``not settings.is_hardened`` (issue #284 A0.8)."""

    def _boom_db(self):
        class _BoomQuery:
            def filter(self, *a, **kw):
                return self

            def first(self):
                raise RuntimeError("db unavailable")

        class _BoomDB:
            def query(self, *a, **kw):
                return _BoomQuery()

        return _BoomDB()

    def test_the_fabricated_user_carries_the_tokens_uuid(self, monkeypatch):
        # is_hardened is already False under the test suite's ENVIRONMENT=testing
        # (a RELAXED_ENVIRONMENTS member); it is a derived, read-only property.
        assert settings.is_hardened is False
        # Revocation checking has its own DB fallback and would fail-closed on
        # this test's deliberately-broken db before ever reaching the user lookup.
        monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", False)
        subject = str(uuid4())
        token = create_access_token({"sub": subject, "role": "user"})

        user = get_current_user(request=_request(), token=token, db=self._boom_db())

        assert str(user.uuid) == subject
        assert user.email == "test@example.com"
        assert user.is_active is True
        assert user.is_superuser is False

    def test_without_testing_set_the_original_db_error_propagates_not_an_attributeerror(
        self, monkeypatch
    ):
        """``os.environ.get("TESTING", "False")`` -- not a dropped/``None`` default.

        With ``TESTING`` genuinely unset (simulating production), the default must
        be the STRING ``"False"`` so ``.lower()`` succeeds and evaluates to
        ``False``, falling through to re-raise the original DB error. A ``None``
        default makes ``None.lower()`` raise ``AttributeError`` instead -- masking
        the real failure with an unrelated crash.
        """
        monkeypatch.delenv("TESTING", raising=False)
        assert settings.is_hardened is False
        monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", False)
        token = create_access_token({"sub": str(uuid4()), "role": "user"})

        with pytest.raises(RuntimeError, match="db unavailable"):
            get_current_user(request=_request(), token=token, db=self._boom_db())


# ── get_optional_current_user ─────────────────────────────────────────────────────


class TestGetOptionalCurrentUserAlgorithmAllowlistIsEnforced:
    """See ``TestGetCurrentUserAlgorithmAllowlistIsEnforced``'s class docstring for
    why HS512 (not "recommended" by joserfc itself) is the algorithm that proves
    the real allowlist reached ``jwt.decode``, and why the earlier "disallowed
    algorithm" version of this test could not distinguish the mutation."""

    def test_a_non_recommended_but_configured_algorithm_is_still_accepted(
        self, db_session, monkeypatch
    ):
        from tests.jwt_compat import jwt as compat_jwt

        user = _persist_user(db_session)
        monkeypatch.setattr(deps_module, "accepted_algorithms", lambda token_type: ["HS512"])
        now = datetime.now(UTC)
        token = compat_jwt.encode(
            {
                "sub": str(user.uuid),
                "role": "user",
                "type": TOKEN_TYPE_ACCESS,
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            settings.JWT_SECRET_KEY,
            algorithm="HS512",
        )
        request = _real_request(headers=[(b"authorization", f"Bearer {token}".encode())])

        result = get_optional_current_user(request=request, db=db_session)

        assert result is not None
        assert result.id == user.id

    def test_an_algorithm_outside_the_configured_allowlist_yields_anonymous(
        self, db_session, monkeypatch
    ):
        """The control: ``ES256``-only allowlist, a recommended-but-not-configured
        ``HS256`` token. A mutant falling back to joserfc's own ``recommended``
        set (which includes HS256) would wrongly accept it here.
        """
        from tests.jwt_compat import jwt as compat_jwt

        user = _persist_user(db_session)
        monkeypatch.setattr(deps_module, "accepted_algorithms", lambda token_type: ["ES256"])
        now = datetime.now(UTC)
        token = compat_jwt.encode(
            {
                "sub": str(user.uuid),
                "role": "user",
                "type": TOKEN_TYPE_ACCESS,
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            settings.JWT_SECRET_KEY,
            algorithm="HS256",
        )
        request = _real_request(headers=[(b"authorization", f"Bearer {token}".encode())])

        result = get_optional_current_user(request=request, db=db_session)

        assert result is None


class TestGetOptionalCurrentUserExternalRequestState:
    def test_user_id_is_stamped_from_the_synced_external_user(self, db_session):
        from app.auth import external_sync

        identity = ExternalIdentity(
            provider=PROVIDER,
            external_id=f"ext_{uuid4().hex[:10]}",
            email=f"opt-external-{uuid4().hex[:8]}@example.com",
            full_name="Optional External",
            org_id=None,
            email_verified=True,
        )

        class _Verifier:
            def verify(self, token, request):
                return identity

        register_verifier(PROVIDER, _Verifier())
        assert has_verifiers()
        request = _real_request(headers=[(b"authorization", b"Bearer external-token")])

        user = get_optional_current_user(request=request, db=db_session)

        assert user is not None
        assert request.state.user_id == user.id
        assert external_sync  # imported for clarity of what performed the sync


class TestGetOptionalCurrentUserPassesTheRealRequestToTheVerifier:
    def test_the_verifier_receives_the_exact_request_object(self, db_session):
        """``verify_external_token(token, request)`` -- a dropped ``request`` (``None``)
        would break any verifier that needs it (e.g. a trusted-proxy check)."""
        received: list[object] = []

        class _Verifier:
            def verify(self, token, request):
                received.append(request)
                return None

        register_verifier(PROVIDER, _Verifier())
        request = _real_request(headers=[(b"authorization", b"Bearer some-token")])

        get_optional_current_user(request=request, db=db_session)

        assert received == [request]


# ── _authenticate_external_token: the sync-failure response headers ──────────────


class TestAuthenticateExternalTokenSyncFailureHeaders:
    def test_the_401_carries_the_bearer_challenge_header(self, db_session, monkeypatch):
        """``test_external_token_auth.py`` pins the ``detail`` string; this pins the
        ``headers`` dict the same raise site also sets, which nothing checked."""
        from app.auth import external_sync

        def _explode(_db, _identity):
            raise RuntimeError("sync exploded")

        monkeypatch.setattr(external_sync, "sync_external_user_to_db", _explode)
        identity = ExternalIdentity(
            provider=PROVIDER,
            external_id=f"ext_{uuid4().hex[:10]}",
            email=f"sync-fail-{uuid4().hex[:8]}@example.com",
            full_name="Sync Fail",
            org_id=None,
            email_verified=True,
        )

        class _Verifier:
            def verify(self, token, request):
                return identity

        register_verifier(PROVIDER, _Verifier())
        request = _real_request()

        with pytest.raises(HTTPException) as exc:
            deps_module._authenticate_external_token(request, "some-token", db_session)

        assert exc.value.status_code == 401
        assert exc.value.detail == "Could not provision external identity"
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


class TestAuthenticateExternalTokenPassesTheRealRequest:
    def test_the_verifier_receives_the_exact_request_object(self, db_session):
        received: list[object] = []

        class _Verifier:
            def verify(self, token, request):
                received.append(request)
                return None

        register_verifier(PROVIDER, _Verifier())
        request = _real_request()

        result = deps_module._authenticate_external_token(request, "some-token", db_session)

        assert result is None
        assert received == [request]
