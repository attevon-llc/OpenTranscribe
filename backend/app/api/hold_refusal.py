"""The one way a route refuses a change to a held file (legal-hold freeze, plan §3.2).

Every guarded write path calls :func:`refuse_if_frozen` before it writes. The refusal is the
wire contract every hold refusal already has (HTTP 409, ``detail = refusal_detail(...)`` with
``error == FILE_UNDER_LEGAL_HOLD``) with a FIXED message, never caught exception text, and one
``content.hold.refused`` audit event per held file with an action from the closed vocabulary
``hold_guard.FREEZE_ACTIONS``.

The audit event is written by ``record_hold_refusal`` (best-effort, outside the request's
transaction), so it survives the rollback the refusal causes.

A ``HoldRefusedError`` that escapes a service unhandled is answered by
:func:`hold_refused_handler` (registered in ``app.main``) with the same 409 and
``FREEZE_REFUSAL``. That is a backstop, not a path: it has no actor, so it writes no audit
event. A route that can reach a service guard calls :func:`refuse_if_frozen` first.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING
from typing import NoReturn

from fastapi import HTTPException
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.services.hold_guard import FREEZE_ACTIONS
from app.services.hold_guard import FREEZE_REFUSAL
from app.services.hold_guard import REPROCESS_REFUSAL
from app.services.hold_guard import HoldRefusedError
from app.services.hold_guard import HoldState
from app.services.hold_guard import hold_states
from app.services.hold_guard import refusal_detail

if TYPE_CHECKING:
    from fastapi import Request

    from app.models.user import User

__all__ = [
    "audit_hold_refusals",
    "hold_refused_handler",
    "raise_hold_refusal",
    "refuse_if_frozen",
    "refuse_reprocess_if_held",
]


def _check_action(action: str) -> None:
    if action not in FREEZE_ACTIONS:
        # A programming error, not a request error: the vocabulary is closed and pinned.
        raise ValueError(f"unknown hold refusal action {action!r}; add it to FREEZE_ACTIONS")


def audit_hold_refusals(
    db: Session,
    states: dict[int, HoldState],
    *,
    actor: User | None,
    action: str,
    request: Request | None = None,
) -> None:
    """Write one ``content.hold.refused`` per held file in ``states``. Never raises."""
    _check_action(action)
    if actor is None or not states:
        return
    from app.services.content_hold_service import record_file_hold_refusals

    record_file_hold_refusals(db, request, actor, states, action=action)


def refuse_if_frozen(
    db: Session,
    file_ids: int | Iterable[int | None] | None,
    *,
    actor: User | None,
    action: str,
    request: Request | None = None,
    message: str = FREEZE_REFUSAL,
    reveal_uuids: bool = False,
    reveal_platform_uuids: bool = False,
) -> None:
    """409 ``FILE_UNDER_LEGAL_HOLD`` when any of ``file_ids`` is held; else return.

    Call it before anything is written. ``None`` ids (a speaker with no file) are ignored. ``reveal_uuids`` gives the organization's hold uuids
    (pass it only for that organization's admin), ``reveal_platform_uuids`` the platform's
    (an instance administrator); the hold's reason is never in the body.
    """
    _check_action(action)
    if file_ids is None:
        return
    raw = [file_ids] if isinstance(file_ids, int) else list(file_ids)
    ids = [int(i) for i in raw if i is not None]
    states = hold_states(db, ids)
    if not states:
        return
    audit_hold_refusals(db, states, actor=actor, action=action, request=request)
    merged = HoldRefusedError(states).merged()
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=refusal_detail(
            merged,
            reveal_uuids=reveal_uuids,
            reveal_platform_uuids=reveal_platform_uuids,
            message=message,
        ),
    )


def raise_hold_refusal(
    db: Session,
    exc: HoldRefusedError,
    *,
    actor: User | None,
    action: str,
    request: Request | None = None,
    message: str = FREEZE_REFUSAL,
) -> NoReturn:
    """Translate a service's ``HoldRefusedError`` into the audited 409 (always raises).

    Nothing the service staged before it reached the held file is flushed (the audit reads
    without autoflush) or committed: the request's session is closed without a commit.
    """
    _check_action(action)
    audit_hold_refusals(db, exc.states, actor=actor, action=action, request=request)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=refusal_detail(exc.merged(), reveal_uuids=False, message=message),
    ) from None


def refuse_reprocess_if_held(
    db: Session,
    file_id: int,
    *,
    actor: User | None = None,
    action: str = "reprocess",
    message: str | None = None,
) -> None:
    """:func:`refuse_if_frozen` with the reprocess wording (reprocess, retry, derived deletes).

    Reprocessing replaces the transcript, speakers and search copies, which a hold exists to
    keep. Raised before anything is cancelled or cleared. Names who holds the file but never
    the hold uuids: the caller may be any editor.
    """
    refuse_if_frozen(
        db, int(file_id), actor=actor, action=action, message=message or REPROCESS_REFUSAL
    )


async def hold_refused_handler(request: Request, exc: Exception) -> JSONResponse:
    """409 for a ``HoldRefusedError`` no route caught: the same body, the freeze wording."""
    if not isinstance(exc, HoldRefusedError):
        raise exc
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": refusal_detail(exc.merged(), reveal_uuids=False, message=FREEZE_REFUSAL)
        },
    )
