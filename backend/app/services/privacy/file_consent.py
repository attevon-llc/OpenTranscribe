"""Per-file voiceprint consent: the write side (issue #1356).

The READ side is ``policy._file_consent_allows`` (the file layer of the privacy decision).

``granted`` records what the UPLOADER claimed about the people in the recording and who made the
claim. The product does not verify it, and it never enables anything an instance, organization or
user switch turned off. ``declined`` denies voiceprints for the file; a later opt-out also purges
the file's existing voiceprints (``PurgeScope(kind="files")``, ``purge_reason="opt_out"``).

Three entry points, three rules:

* ``/files/prepare`` writes the state with the row, before any processing exists
  (:func:`state_for_new_file`).
* ``/files/complete`` may only RESTRICT (:func:`restrict_at_complete`): a clone of the request
  must not be able to widen what ``/prepare`` recorded, and clients retry ``/complete``.
* The owner's PATCH changes it any time (:func:`change_state`), and a move to ``declined``
  starts the purge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from app.models.media import VOICEPRINT_CONSENT_STATES

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.media import MediaFile

logger = logging.getLogger(__name__)

#: States a client may SET (``unspecified`` is only ever the starting point).
SETTABLE_STATES = ("granted", "declined")


@dataclass(frozen=True)
class ConsentChange:
    """What :func:`change_state` did."""

    previous: str
    current: str

    @property
    def changed(self) -> bool:
        return self.previous != self.current


def _stamp(row: MediaFile, state: str, actor_user_id: int | None) -> None:
    row.voiceprint_consent = state  # type: ignore[assignment]
    row.voiceprint_consent_at = datetime.now(UTC)  # type: ignore[assignment]
    row.voiceprint_consent_by_user_id = actor_user_id  # type: ignore[assignment]


def stamp_new_file(row: MediaFile, requested: str | None, actor_user_id: int) -> None:
    """``/files/prepare``: record the uploader's stated state on a file that has no processing yet."""
    if requested is None or requested == "unspecified":
        return
    if requested not in SETTABLE_STATES:
        raise ValueError(f"unknown voiceprint consent state {requested!r}")
    _stamp(row, requested, actor_user_id)


def restrict_at_complete(row: MediaFile, requested: str | None, actor_user_id: int) -> bool:
    """``/files/complete``: apply ``requested`` only if it RESTRICTS. Returns True if it changed.

    ``declined`` is applied from any state. Anything that would widen (``granted`` over
    ``unspecified``/``declined``, or ``unspecified``) is ignored with a warning, never an error:
    a client retries ``/complete``, and the stored state stays what ``/prepare`` recorded.
    """
    if requested is None or requested == row.voiceprint_consent:
        return False
    if requested == "declined":
        _stamp(row, "declined", actor_user_id)
        return True
    logger.warning(
        "file %s: /complete asked for voiceprint consent %r over %r; ignored (restrict-only)",
        row.id,
        requested,
        row.voiceprint_consent,
    )
    return False


def change_state(row: MediaFile, new_state: str, actor_user_id: int) -> ConsentChange:
    """The owner's later change. Stamps the row; the caller commits and starts any purge."""
    if new_state not in SETTABLE_STATES:
        raise ValueError(f"unknown voiceprint consent state {new_state!r}")
    previous = str(row.voiceprint_consent)
    if previous != new_state:
        _stamp(row, new_state, actor_user_id)
    return ConsentChange(previous=previous, current=new_state)


def start_opt_out_purge(
    db: Session,
    file_id: int,
    *,
    actor_user_id: int,
    actor_username: str | None,
    source_ip: str | None = None,
    user_agent: str | None = None,
) -> str:
    """Queue the two-pass purge of one file's voiceprints. Returns the task id.

    The consent column MUST already be committed as ``declined``: that is what stops new
    voiceprints while the purge runs, and the purge's second pass catches a write that raced it.
    """
    from app.services.privacy import policy_admin
    from app.services.privacy.purge import OPT_OUT
    from app.services.privacy.purge import VOICEPRINTS
    from app.services.privacy.purge import PurgeScope

    return policy_admin.start_purge(
        db,
        PurgeScope(kind="files", file_ids=(int(file_id),)),
        [VOICEPRINTS],
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        source_ip=source_ip,
        user_agent=user_agent,
        purge_reason=OPT_OUT,
    )


__all__ = [
    "SETTABLE_STATES",
    "VOICEPRINT_CONSENT_STATES",
    "ConsentChange",
    "change_state",
    "restrict_at_complete",
    "stamp_new_file",
    "start_opt_out_purge",
]
