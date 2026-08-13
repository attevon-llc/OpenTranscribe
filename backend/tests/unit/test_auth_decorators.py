"""Behavioural tests for the authorization decorators in ``app/utils/auth_decorators.py``.

⚠️ **This module is currently unreachable from any live route.** Its only importer in
``app/`` is ``app/services/transcription_service.py`` (and only for ``AuthorizationHelper``,
never for the four decorators), and ``TranscriptionService`` is itself imported by nothing —
no router, no task, no test, no dynamic-import registry. The four decorators
(``require_file_ownership``, ``require_admin_or_ownership``, ``require_admin``,
``require_verified_user``) have **zero call sites anywhere in the repository**.

These tests exist anyway, for one reason: ``app/utils`` is the namespace the next developer
reaches for, and ``@require_admin`` is a name that gets applied without reading the body. If
someone revives these, the gates should already be pinned. Everything asserted here is
therefore a *contract*, not a description — each test fails if the gate it covers is removed.

What is pinned, and why each property matters:

* **DENY before ALLOW.** A gate that refuses nobody is the failure these tests exist to catch,
  so every decorator has a refusal test asserting the exact status code and detail string.
  Never ``!= 200``: a 500 satisfies that, and this repo has a detector for the shape.
* **The kwargs-only contract, in both directions.** The decorators read ``db`` /
  ``current_user`` / ``file_id`` out of ``kwargs`` and never look at ``args``. A caller who
  passes positionally gets a ``ValueError``. That is fail-closed *today*; the danger is a
  future refactor turning the ``ValueError`` into a silent skip of the check, which is why the
  positional path is asserted rather than left implicit.
* **Which decorator has an admin bypass and which does not.** ``require_admin_or_ownership``
  short-circuits for admins; ``require_file_ownership`` does not. Both directions are pinned
  so neither can be "harmonised" into the other by accident.
* **Two documented mismatches** (see ``test_verified_user_gate_admits_an_unverified_account``
  and ``test_falsy_file_id_is_rejected_as_a_missing_parameter``) are pinned *as they behave*,
  with the discrepancy called out in the test body. Reported, not fixed — ``app/`` is not this
  suite's to change.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException

from app.models.media import MediaFile
from app.models.user import User
from app.utils.auth_decorators import AuthorizationHelper
from app.utils.auth_decorators import require_admin
from app.utils.auth_decorators import require_admin_or_ownership
from app.utils.auth_decorators import require_file_ownership
from app.utils.auth_decorators import require_verified_user

#: Sentinel returned by every probe. A test asserting this value proves the wrapped
#: function actually executed, which "no exception was raised" alone does not.
PROBE_RESULT = "probe-body-executed"


def _probe() -> tuple[Any, list[dict[str, Any]]]:
    """Build an undecorated probe function plus the list recording its invocations.

    Not a mock: a plain function whose body appends to a list and returns a sentinel.
    Assertions are on the sentinel and on the recorded call arguments, so the tests
    constrain behaviour rather than mock wiring.
    """
    calls: list[dict[str, Any]] = []

    def body(**kwargs: Any) -> str:
        calls.append(dict(kwargs))
        return PROBE_RESULT

    return body, calls


def _make_file(db_session, owner: User) -> MediaFile:
    """Insert a MediaFile owned by ``owner``."""
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="auth_decorators_test.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _make_user(db_session, *, role: str = "user", is_active: bool = True, **columns) -> User:
    """Insert a User with the role/flags a specific gate test needs."""
    unique_id = uuid.uuid4().hex[:8]
    user = User(
        email=f"authdec_{unique_id}@example.com",
        full_name="Auth Decorator Probe",
        hashed_password="not-a-real-hash",
        is_active=is_active,
        is_superuser=(role == "super_admin"),
        role=role,
        **columns,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _unused_file_id(db_session) -> int:
    """An id no MediaFile row holds, derived from the live table rather than guessed."""
    highest = db_session.query(MediaFile.id).order_by(MediaFile.id.desc()).first()
    return (highest[0] if highest is not None else 0) + 10_000


# ---------------------------------------------------------------------------
# require_file_ownership
# ---------------------------------------------------------------------------


def test_file_owner_is_allowed_through(db_session, normal_user):
    """The ALLOW path. Without this, a decorator that refuses everyone would pass the
    DENY tests below and look correct."""
    media_file = _make_file(db_session, normal_user)
    body, calls = _probe()
    guarded = require_file_ownership(body)

    result = guarded(db=db_session, current_user=normal_user, file_id=media_file.id)

    assert result == PROBE_RESULT
    assert len(calls) == 1
    assert calls[0]["file_id"] == media_file.id


def test_non_owner_is_refused_with_404(db_session, normal_user, other_user):
    """The gate under test. A second account must not reach the body of a
    file-scoped handler, and the refusal is a 404 (not a 403) so the gate does not
    confirm the file exists."""
    media_file = _make_file(db_session, normal_user)
    body, calls = _probe()
    guarded = require_file_ownership(body)

    with pytest.raises(HTTPException) as excinfo:
        guarded(db=db_session, current_user=other_user, file_id=media_file.id)

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "File not found or access denied"
    assert calls == []


def test_unknown_file_id_is_refused_with_the_same_404(db_session, normal_user):
    """A missing file and someone else's file are indistinguishable to the caller —
    that identical response is the property that keeps the gate from being an
    existence oracle."""
    body, calls = _probe()
    guarded = require_file_ownership(body)

    with pytest.raises(HTTPException) as excinfo:
        guarded(db=db_session, current_user=normal_user, file_id=_unused_file_id(db_session))

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "File not found or access denied"
    assert calls == []


def test_admin_does_not_bypass_file_ownership(db_session, normal_user, admin_user):
    """``require_file_ownership`` has NO admin bypass, unlike its sibling
    ``require_admin_or_ownership``. Pinned in both directions so the two cannot be
    quietly harmonised: an admin reading another user's file through this decorator
    would be a privilege change, not a refactor."""
    media_file = _make_file(db_session, normal_user)
    body, calls = _probe()
    guarded = require_file_ownership(body)

    with pytest.raises(HTTPException) as excinfo:
        guarded(db=db_session, current_user=admin_user, file_id=media_file.id)

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "File not found or access denied"
    assert calls == []


def test_positional_call_raises_value_error_instead_of_checking_ownership(db_session, normal_user):
    """The kwargs-only contract, negative direction.

    The wrapper never inspects ``args``. A caller who passes positionally gets a
    ``ValueError`` — confusing, but fail-CLOSED. The reason this is asserted rather
    than left implicit: the dangerous refactor is one that makes the missing-kwargs
    branch fall through to ``func(*args, **kwargs)``, which would turn every
    positional caller into an unguarded one.
    """
    media_file = _make_file(db_session, normal_user)
    body, calls = _probe()
    guarded = require_file_ownership(body)

    with pytest.raises(ValueError, match="must have 'db', 'current_user', and 'file_id'"):
        guarded(db_session, normal_user, media_file.id)

    assert calls == []


def test_falsy_file_id_is_rejected_as_a_missing_parameter(db_session, normal_user):
    """DISCREPANCY, pinned as-is: the guard is ``if not all([db, current_user, file_id])``,
    so ``file_id=0`` reads as *absent* and raises ``ValueError`` rather than performing
    an ownership lookup that would 404.

    Harmless today — ``media_file.id`` is a 1-based Postgres serial, so 0 never occurs —
    and fail-closed either way. It is pinned because the same ``all(...)`` shape is what
    a copy-paste into a gate keyed on a *nullable* or 0-valid identifier would inherit,
    where "falsy means missing" stops being safe.
    """
    body, calls = _probe()
    guarded = require_file_ownership(body)

    with pytest.raises(ValueError, match="must have 'db', 'current_user', and 'file_id'"):
        guarded(db=db_session, current_user=normal_user, file_id=0)

    assert calls == []


# ---------------------------------------------------------------------------
# require_admin_or_ownership
# ---------------------------------------------------------------------------


def test_resource_owner_is_allowed_through(db_session, normal_user):
    """ALLOW path for the owner branch."""
    media_file = _make_file(db_session, normal_user)
    body, calls = _probe()
    guarded = require_admin_or_ownership(MediaFile)(body)

    result = guarded(db=db_session, current_user=normal_user, resource_id=media_file.id)

    assert result == PROBE_RESULT
    assert len(calls) == 1
    assert calls[0]["resource_id"] == media_file.id


def test_non_owner_non_admin_is_refused_with_404(db_session, normal_user, other_user):
    """The gate. A plain second account gets the resource-flavoured 404."""
    media_file = _make_file(db_session, normal_user)
    body, calls = _probe()
    guarded = require_admin_or_ownership(MediaFile)(body)

    with pytest.raises(HTTPException) as excinfo:
        guarded(db=db_session, current_user=other_user, resource_id=media_file.id)

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Resource not found or access denied"
    assert calls == []


def test_unknown_resource_id_is_refused_for_a_non_admin(db_session, normal_user):
    body, calls = _probe()
    guarded = require_admin_or_ownership(MediaFile)(body)

    with pytest.raises(HTTPException) as excinfo:
        guarded(
            db=db_session,
            current_user=normal_user,
            resource_id=_unused_file_id(db_session),
        )

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Resource not found or access denied"
    assert calls == []


def test_admin_bypasses_ownership_on_another_users_resource(db_session, normal_user, admin_user):
    """The admin bypass IS the documented behaviour here — asserted so that removing it
    (or removing the ownership branch it sits in front of) fails."""
    media_file = _make_file(db_session, normal_user)
    body, calls = _probe()
    guarded = require_admin_or_ownership(MediaFile)(body)

    result = guarded(db=db_session, current_user=admin_user, resource_id=media_file.id)

    assert result == PROBE_RESULT
    assert len(calls) == 1
    assert calls[0]["resource_id"] == media_file.id


def test_super_admin_also_bypasses_ownership(db_session, normal_user, super_admin_user):
    """``User.is_admin`` is ``role in ("admin", "super_admin")``. Pinned so a narrowing
    of that property to ``role == "admin"`` cannot silently lock super admins out."""
    media_file = _make_file(db_session, normal_user)
    body, calls = _probe()
    guarded = require_admin_or_ownership(MediaFile)(body)

    result = guarded(db=db_session, current_user=super_admin_user, resource_id=media_file.id)

    assert result == PROBE_RESULT
    assert len(calls) == 1


def test_admin_bypass_runs_before_the_resource_is_looked_up(db_session, admin_user):
    """Ordering matters, and it is observable: the admin branch returns *before* the
    query, so an admin passing an id no row holds still reaches the handler body.

    That means this decorator gives an admin no existence guarantee — the wrapped
    handler must do its own lookup. Pinned because reordering the two branches would
    change an admin 200 into a 404 across every hypothetical call site at once.
    """
    body, calls = _probe()
    guarded = require_admin_or_ownership(MediaFile)(body)
    absent_id = _unused_file_id(db_session)

    result = guarded(db=db_session, current_user=admin_user, resource_id=absent_id)

    assert result == PROBE_RESULT
    assert len(calls) == 1
    assert calls[0]["resource_id"] == absent_id


def test_custom_id_param_is_read_and_named_in_the_error(db_session, normal_user):
    """The factory's ``id_param`` must drive both the kwarg it reads and the
    ``ValueError`` message, or a caller mis-wiring the parameter name gets no clue."""
    media_file = _make_file(db_session, normal_user)
    body, calls = _probe()
    guarded = require_admin_or_ownership(MediaFile, id_param="media_file_id")(body)

    result = guarded(db=db_session, current_user=normal_user, media_file_id=media_file.id)
    assert result == PROBE_RESULT
    assert len(calls) == 1

    with pytest.raises(ValueError, match="'media_file_id' parameters"):
        guarded(db=db_session, current_user=normal_user, resource_id=media_file.id)

    assert len(calls) == 1


def test_custom_user_id_field_is_the_field_actually_compared(db_session, normal_user, other_user):
    """``user_id_field`` must select the column compared against ``current_user.id``.

    Pointed at ``id`` — the MediaFile primary key — ownership becomes "your user id
    equals this file's id", which is true for exactly one contrived pairing and false
    for the real owner. Both outcomes are asserted, so a decorator that ignored
    ``user_id_field`` and always compared ``user_id`` would fail the first assertion.
    """
    media_file = _make_file(db_session, normal_user)
    body, calls = _probe()
    guarded = require_admin_or_ownership(MediaFile, user_id_field="id")(body)

    with pytest.raises(HTTPException) as excinfo:
        guarded(db=db_session, current_user=normal_user, resource_id=media_file.id)
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Resource not found or access denied"
    assert calls == []

    # ...and it really is reading `id`: a user whose own id equals the file's id passes.
    contrived = _make_user(db_session)
    contrived.id = media_file.id
    result = guarded(db=db_session, current_user=contrived, resource_id=media_file.id)
    assert result == PROBE_RESULT
    assert len(calls) == 1

    db_session.expunge(contrived)


def test_admin_or_ownership_positional_call_raises_value_error(db_session, normal_user):
    """kwargs-only contract for the factory-built decorator."""
    media_file = _make_file(db_session, normal_user)
    body, calls = _probe()
    guarded = require_admin_or_ownership(MediaFile)(body)

    with pytest.raises(ValueError, match="must have 'db', 'current_user', and 'resource_id'"):
        guarded(db_session, normal_user, media_file.id)

    assert calls == []


# ---------------------------------------------------------------------------
# require_admin
# ---------------------------------------------------------------------------


def test_normal_user_is_refused_by_require_admin(db_session, normal_user):
    """THE gate for this decorator. 403 with the exact detail — a bare ``!= 200``
    would also accept the 500 that a broken ``is_admin`` property produces."""
    body, calls = _probe()
    guarded = require_admin(body)

    with pytest.raises(HTTPException) as excinfo:
        guarded(db=db_session, current_user=normal_user)

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Admin privileges required"
    assert calls == []


def test_admin_is_allowed_by_require_admin(db_session, admin_user):
    body, calls = _probe()
    guarded = require_admin(body)

    result = guarded(db=db_session, current_user=admin_user)

    assert result == PROBE_RESULT
    assert len(calls) == 1


def test_super_admin_is_allowed_by_require_admin(db_session, super_admin_user):
    """``role == "super_admin"`` satisfies ``is_admin``; asserted so the gate cannot be
    narrowed to the literal ``"admin"`` string without a failure."""
    body, calls = _probe()
    guarded = require_admin(body)

    result = guarded(db=db_session, current_user=super_admin_user)

    assert result == PROBE_RESULT
    assert len(calls) == 1


def test_inactive_admin_still_passes_require_admin(db_session):
    """``require_admin`` checks the role and nothing else — a *disabled* admin account
    still gets through it.

    Pinned rather than treated as a bug: the decorator's contract is "is an admin", and
    liveness is the job of the authentication dependency that produced ``current_user``.
    It matters because ``require_admin`` alone is not a sufficient gate; a call site
    would need ``require_verified_user`` stacked with it.
    """
    disabled_admin = _make_user(db_session, role="admin", is_active=False)
    body, calls = _probe()
    guarded = require_admin(body)

    result = guarded(db=db_session, current_user=disabled_admin)

    assert result == PROBE_RESULT
    assert len(calls) == 1


def test_require_admin_raises_value_error_without_a_current_user_kwarg(db_session):
    body, calls = _probe()
    guarded = require_admin(body)

    with pytest.raises(ValueError, match="must have 'current_user' parameter"):
        guarded(db=db_session)

    assert calls == []


def test_require_admin_positional_call_raises_value_error(db_session, admin_user):
    """kwargs-only, negative direction: even a genuine admin passed positionally is
    rejected rather than silently admitted."""
    body, calls = _probe()
    guarded = require_admin(body)

    with pytest.raises(ValueError, match="must have 'current_user' parameter"):
        guarded(admin_user)

    assert calls == []


# ---------------------------------------------------------------------------
# require_verified_user
# ---------------------------------------------------------------------------


def test_inactive_user_is_refused_by_require_verified_user(db_session):
    """THE gate. A deactivated account must not reach the handler body."""
    inactive = _make_user(db_session, is_active=False)
    body, calls = _probe()
    guarded = require_verified_user(body)

    with pytest.raises(HTTPException) as excinfo:
        guarded(db=db_session, current_user=inactive)

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Account verification required"
    assert calls == []


def test_active_user_is_allowed_by_require_verified_user(db_session, normal_user):
    body, calls = _probe()
    guarded = require_verified_user(body)

    result = guarded(db=db_session, current_user=normal_user)

    assert result == PROBE_RESULT
    assert len(calls) == 1


def test_verified_user_gate_admits_an_unverified_account(db_session):
    """DISCREPANCY, pinned as-is: ``require_verified_user`` inspects ``is_active`` only.

    ``User`` carries two *separate* columns that mean verification —
    ``email_verified`` and ``approval_status`` (see ``app/models/user.py`` and
    ``app/auth/approval.py``, whose comment spells out that deactivation and approval
    are different states). An account that has never verified its email and is still
    ``approval_status="pending"`` passes this gate, because it is ``is_active=True``.

    The decorator's name and its own 403 detail ("Account verification required") both
    promise a check the body does not perform. Unreachable today, which is the only
    reason this is a pinned observation and not a live vulnerability. Reported, not
    fixed — ``backend/app/`` is out of this suite's scope.
    """
    unverified = _make_user(
        db_session,
        is_active=True,
        email_verified=False,
        approval_status="pending",
    )
    body, calls = _probe()
    guarded = require_verified_user(body)

    result = guarded(db=db_session, current_user=unverified)

    assert result == PROBE_RESULT
    assert len(calls) == 1
    assert unverified.email_verified is False
    assert unverified.approval_status == "pending"


def test_require_verified_user_raises_value_error_without_a_current_user_kwarg(db_session):
    body, calls = _probe()
    guarded = require_verified_user(body)

    with pytest.raises(ValueError, match="must have 'current_user' parameter"):
        guarded(db=db_session)

    assert calls == []


def test_require_verified_user_positional_call_raises_value_error(db_session, normal_user):
    body, calls = _probe()
    guarded = require_verified_user(body)

    with pytest.raises(ValueError, match="must have 'current_user' parameter"):
        guarded(normal_user)

    assert calls == []


# ---------------------------------------------------------------------------
# Stacking + identity
# ---------------------------------------------------------------------------


def test_stacked_gates_both_apply_and_the_outer_one_refuses_first(db_session):
    """A disabled non-admin must be refused by whichever gate sits outermost.

    With ``require_admin`` outermost the answer is 403 "Admin privileges required" —
    the role check runs before liveness. Pinned because the two decorators return the
    same status code with different details, so only the detail distinguishes them, and
    a reordering would change which reason a caller is told.
    """
    disabled_user = _make_user(db_session, role="user", is_active=False)
    body, calls = _probe()
    guarded = require_admin(require_verified_user(body))

    with pytest.raises(HTTPException) as excinfo:
        guarded(db=db_session, current_user=disabled_user)

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Admin privileges required"
    assert calls == []


def test_decorators_preserve_the_wrapped_function_identity(db_session):
    """All four use ``functools.wraps``. FastAPI derives an operation id from
    ``__name__``, so losing it would silently rename every route these guarded."""

    def handler_under_test(**kwargs: Any) -> str:
        return PROBE_RESULT

    assert require_file_ownership(handler_under_test).__name__ == "handler_under_test"
    assert require_admin(handler_under_test).__name__ == "handler_under_test"
    assert require_verified_user(handler_under_test).__name__ == "handler_under_test"
    assert (
        require_admin_or_ownership(MediaFile)(handler_under_test).__name__ == "handler_under_test"
    )


# ---------------------------------------------------------------------------
# AuthorizationHelper — the one part of this module with a (dead) caller
# ---------------------------------------------------------------------------


def test_check_file_access_returns_the_owned_file(db_session, normal_user):
    media_file = _make_file(db_session, normal_user)

    found = AuthorizationHelper.check_file_access(db_session, media_file.id, normal_user)

    assert found.id == media_file.id
    assert found.user_id == normal_user.id


def test_check_file_access_refuses_a_non_owner(db_session, normal_user, other_user):
    """``TranscriptionService`` calls this on eight paths; it is the only live-ish
    authorization surface in the module."""
    media_file = _make_file(db_session, normal_user)

    with pytest.raises(HTTPException) as excinfo:
        AuthorizationHelper.check_file_access(db_session, media_file.id, other_user)

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "File not found or access denied"


def test_check_file_access_gives_an_admin_no_bypass(db_session, normal_user, admin_user):
    """Consistent with ``require_file_ownership`` and inconsistent with
    ``require_admin_or_ownership`` — pinned so the divergence is deliberate."""
    media_file = _make_file(db_session, normal_user)

    with pytest.raises(HTTPException) as excinfo:
        AuthorizationHelper.check_file_access(db_session, media_file.id, admin_user)

    assert excinfo.value.status_code == 404


def test_check_admin_or_owner_distinguishes_all_three_roles(db_session, normal_user, other_user):
    media_file = _make_file(db_session, normal_user)
    admin = _make_user(db_session, role="admin")

    assert AuthorizationHelper.check_admin_or_owner(media_file, normal_user) is True
    assert AuthorizationHelper.check_admin_or_owner(media_file, other_user) is False
    assert AuthorizationHelper.check_admin_or_owner(media_file, admin) is True


def test_check_admin_or_owner_returns_false_for_an_absent_owner_field(db_session, other_user):
    """``getattr(resource, owner_field, None)`` defaults to ``None``, so a resource with
    no such attribute denies rather than raising. Fail-closed, and asserted as such."""
    media_file = _make_file(db_session, other_user)

    assert (
        AuthorizationHelper.check_admin_or_owner(media_file, other_user, owner_field="no_such_col")
        is False
    )


def test_require_resource_access_splits_404_from_403(db_session, normal_user, other_user):
    """This helper answers a *different* pair of statuses than the decorator that shares
    its logic: 404 for missing, 403 for present-but-not-yours.

    ``require_admin_or_ownership`` collapses both into 404. The 403 here confirms the
    resource exists, so the two are not interchangeable — swapping one for the other
    would turn a non-enumerable surface into an enumerable one.
    """
    media_file = _make_file(db_session, normal_user)

    with pytest.raises(HTTPException) as missing:
        AuthorizationHelper.require_resource_access(
            db_session, MediaFile, _unused_file_id(db_session), normal_user
        )
    assert missing.value.status_code == 404
    assert missing.value.detail == "Resource not found"

    with pytest.raises(HTTPException) as forbidden:
        AuthorizationHelper.require_resource_access(
            db_session, MediaFile, media_file.id, other_user
        )
    assert forbidden.value.status_code == 403
    assert forbidden.value.detail == "Access denied"


def test_require_resource_access_returns_the_resource_for_owner_and_admin(
    db_session, normal_user, admin_user
):
    media_file = _make_file(db_session, normal_user)

    as_owner = AuthorizationHelper.require_resource_access(
        db_session, MediaFile, media_file.id, normal_user
    )
    as_admin = AuthorizationHelper.require_resource_access(
        db_session, MediaFile, media_file.id, admin_user
    )

    assert as_owner.id == media_file.id
    assert as_admin.id == media_file.id
