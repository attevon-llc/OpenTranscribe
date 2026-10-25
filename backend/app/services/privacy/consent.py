"""Voiceprint consent seam (issue #1280), implemented by whatever records consent.

Core ships no consent record. This module is the one place a consent implementation (a derived
deployment now, core's own built-in hook with #1132 later) plugs in, so there is a single gate
and never a second one beside it.

Contract (covered by ``CLOUD_SEAM_VERSION`` in ``app.auth.constants``):

* ``register_voiceprint_consent_hook(hook)``: every registered hook must return the literal
  ``True`` for an action to proceed. No hooks registered means allow, which is the behaviour
  before the seam existed.
* A hook that raises, or returns anything but ``True``, DENIES and is logged. A buggy hook must
  not silently allow biometric processing.
* The session is borrowed read-only (same contract as ``core.tenant_limits.RedactionFloorResolver``):
  a hook must not write, commit or roll back on it.
* Hooks run inside Celery tasks (no request) and inside the shared-profile candidate query, so
  they must be fast and must answer per subject, not per request.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ConsentPurpose = Literal["create", "share"]


@dataclass(frozen=True)
class VoiceprintConsentRequest:
    """One question put to the consent hooks.

    Attributes:
        purpose: ``create`` to compute or store a voiceprint of the subject, ``share`` to let
            someone other than the subject match against the subject's profiles.
        subject_user_id: The person whose voice it is (the owner of the file or profile).
        organization_id: The tenant of the record, taken from the row, never from request state.
        media_file_id: The file being processed, when the question is about one.
        profile_ids: The profiles in question for ``share``, grouped per owner so a hook is
            asked once per owner and not once per profile.
        requester_user_id: Who is asking, for ``share``.
    """

    purpose: ConsentPurpose
    subject_user_id: int
    organization_id: int | None
    media_file_id: int | None = None
    profile_ids: tuple[int, ...] = ()
    requester_user_id: int | None = None


VoiceprintConsentHook = Callable[["Session", VoiceprintConsentRequest], bool]

_hooks: list[VoiceprintConsentHook] = []


def register_voiceprint_consent_hook(hook: VoiceprintConsentHook) -> None:
    """Add a consent hook. Registering the same callable twice is a no-op."""
    if hook not in _hooks:
        _hooks.append(hook)
        logger.info("Voiceprint consent hook registered: %s", getattr(hook, "__name__", hook))


def clear_voiceprint_consent_hooks() -> None:
    """Remove every hook (restores the no-hooks, allow-all state; primarily for tests)."""
    _hooks.clear()


def has_voiceprint_consent_hooks() -> bool:
    """True when at least one hook is registered."""
    return bool(_hooks)


def voiceprint_consent_granted(db: Session, request: VoiceprintConsentRequest) -> bool:
    """Ask every registered hook; True only if all of them return ``True``.

    Fails closed: a hook that raises, or returns a non-``True`` value, denies.
    """
    for hook in list(_hooks):
        name = getattr(hook, "__name__", repr(hook))
        # A savepoint per hook: a hook whose query fails must not leave the borrowed
        # session's transaction aborted for the caller.
        savepoint = db.begin_nested()
        try:
            answer = hook(db, request)
        except Exception:
            savepoint.rollback()
            logger.exception("Voiceprint consent hook %s raised; denying %s", name, request.purpose)
            return False
        savepoint.commit()
        if answer is not True:
            if answer is not False:
                logger.error(
                    "Voiceprint consent hook %s returned %r, not a bool; denying", name, answer
                )
            return False
    return True
