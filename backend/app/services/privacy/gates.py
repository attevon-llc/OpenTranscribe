"""Entry gates of the voiceprint and gender stages (issue #1280, tier 2).

The pipeline plan (tier 1) decides at dispatch whether a voiceprint step is scheduled at all.
A run that was planned before an owner, an organization or the operator flipped the switch is
still in flight, so every stage that produces or consumes a voiceprint asks again, here, with
a FRESH read of ``services/privacy/policy`` (no cache: the 10 s cache belongs to the OpenSearch
write backstop, tier 3). Every error reads as "not allowed".

The answers are keyed by the FILE, so the owner and the organization come from its row, never
from the user who triggered the stage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.db.session_utils import session_scope

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def voiceprints_allowed(file_id: int, db: Session | None = None) -> bool:
    """May a voiceprint be computed, matched or stored for this file right now?

    Args:
        file_id: The media file the stage works on.
        db: A session the caller already holds. Otherwise a short one is opened and closed.
    """
    from app.services.privacy.policy import voiceprints_allowed_for_file

    try:
        if db is not None:
            return voiceprints_allowed_for_file(db, file_id)
        with session_scope() as short:
            return voiceprints_allowed_for_file(short, file_id)
    except Exception:
        logger.exception("privacy: voiceprint gate failed for file %s; denying", file_id)
        return False


def gender_allowed(file_id: int, db: Session | None = None) -> bool:
    """May gender be inferred for this file right now? Same contract as above."""
    from app.services.privacy.policy import gender_allowed_for_file

    try:
        if db is not None:
            return gender_allowed_for_file(db, file_id)
        with session_scope() as short:
            return gender_allowed_for_file(short, file_id)
    except Exception:
        logger.exception("privacy: gender gate failed for file %s; denying", file_id)
        return False


def job_privacy_overrides(file_id: int) -> dict[str, bool]:
    """``TranscriptionConfig`` overrides for one job: only ever restrictions.

    A key is present only when the policy forbids the feature, so an allowed job keeps the
    deployment's own defaults (``USE_NATIVE_SPEAKER_EMBEDDINGS``, ``DIAR_NATIVE_GENDER``) and
    this can never switch on something the deployment turned off.
    """
    overrides: dict[str, bool] = {}
    if not voiceprints_allowed(file_id):
        overrides["enable_native_embeddings"] = False
    if not gender_allowed(file_id):
        overrides["request_gender"] = False
    if overrides:
        logger.info("privacy: restricting job for file %s: %s", file_id, sorted(overrides))
    return overrides
