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
from app.db.base import get_db
from app.models.user import User
from app.services import system_settings_service

router = APIRouter()
logger = logging.getLogger(__name__)

#: SystemSettings key. Value is an ISO timestamp string once completed, absent
#: (None) otherwise — presence, not a bool, so "when" is free for an audit trail
#: without a second key.
_COMPLETED_AT_KEY = "first_run_wizard.completed_at"


@router.get("/status")
def get_first_run_wizard_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict[str, str | None]:
    """Whether the wizard has been completed (or explicitly skipped — both count:
    HANDOFF's spec is 'mark it complete... and never show it again', re-runnable
    from Settings for someone who skipped)."""
    completed_at = system_settings_service.get_setting(db, _COMPLETED_AT_KEY)
    return {"completed_at": completed_at}


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
