"""First-run setup wizard completion state (HANDOFF #28 / auth-parity R8).

Not an auth method and not itself a config category — this endpoint only tracks
whether the guided first-run flow has been shown, so the SPA knows whether to
offer it. The wizard's actual work (changing the bootstrap password, picking an
identity-source posture, flipping security defaults) goes through the existing
``PUT /users/{uuid}``, ``PUT /admin/auth-config/{category}`` and OIDC/LDAP Test
Connection endpoints — this module deliberately does not duplicate any of them.

DB-backed via ``SystemSettings`` (``first_run_wizard.completed_at``), the same
pattern as ``directory_sync.*`` — no ``.env`` var, because there is nothing here
an operator would want to preconfigure before ever logging in. Never gates
anything: a deployment that never completes the wizard functions identically to
one that did. That is the "never block the zero-config local path" invariant
from HANDOFF's task list, applied literally — this endpoint can 500 and no
other auth flow notices.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.auth.password_policy import password_policy
from app.core.security import verify_password
from app.db.base import get_db
from app.initial_data import _DEV_ADMIN_PASSWORD
from app.legal import acceptance as legal_acceptance
from app.models.user import User
from app.services import system_settings_service

router = APIRouter()
logger = logging.getLogger(__name__)

#: SystemSettings key. Value is an ISO timestamp string once completed, absent
#: (None) otherwise — presence, not a bool, so "when" is free for an audit trail
#: without a second key.
_COMPLETED_AT_KEY = "first_run_wizard.completed_at"


def _credential_kind(user: User) -> str:
    """Classify the caller's current credential without exposing it.

    ``default_dev`` means the stored hash still verifies against the well-known
    development bootstrap password (``initial_data``), i.e. anyone who has read the
    docs can sign in. ``generated`` means the password was randomly generated at
    first boot and ``must_change_password`` still forces its replacement. Anything
    else is ``operator_set``. Only the caller's own hash is checked, and neither the
    candidate nor the result of the comparison is logged or returned.
    """
    if user.hashed_password and verify_password(_DEV_ADMIN_PASSWORD, str(user.hashed_password)):
        return "default_dev"
    if user.must_change_password:
        return "generated"
    return "operator_set"


@router.get("/status")
def get_first_run_wizard_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict[str, str | int | bool | None]:
    """Wizard state plus what step 1 needs to explain the password choice.

    ``completed_at``: set once completed or explicitly skipped (both count: HANDOFF's
    spec is 'mark it complete... and never show it again', re-runnable from Settings).
    ``credential_kind``: ``default_dev`` | ``operator_set`` | ``generated``.
    ``password_min_age_hours``: the configured minimum time between password changes
    (0 means no limit).
    ``password_changed_by_you``: the account holder has chosen their own password at
    least once (``password_last_self_changed_at`` is set), so the SPA can resume the
    wizard after the Settings round trip.
    ``legal_required``: this edition needs the operator acknowledgment (false when an
    extension installed its own document provider). ``legal_acknowledged``: the caller has
    acknowledged the CURRENT text, or none is required.
    """
    legal_required, legal_acknowledged = legal_acceptance.operator_acknowledgment_status(
        db, current_user.id
    )
    completed_at = system_settings_service.get_setting(db, _COMPLETED_AT_KEY)
    return {
        "completed_at": completed_at,
        "credential_kind": _credential_kind(current_user),
        "password_min_age_hours": password_policy.min_age_hours,
        "password_changed_by_you": current_user.password_last_self_changed_at is not None,
        "legal_required": legal_required,
        "legal_acknowledged": legal_acknowledged,
    }


@router.post("/complete")
def complete_first_run_wizard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict[str, str | None]:
    """Mark the wizard complete. Idempotent — re-running it (from Settings, for
    someone who skipped the first time) and completing it again just re-stamps
    the timestamp rather than erroring."""
    from datetime import UTC
    from datetime import datetime

    completed_at = datetime.now(UTC).isoformat()
    system_settings_service.set_setting(
        db,
        _COMPLETED_AT_KEY,
        completed_at,
        description="First-run setup wizard completion timestamp (HANDOFF #28)",
    )
    logger.info(f"First-run setup wizard marked complete by {current_user.email}")
    return {"completed_at": completed_at}
