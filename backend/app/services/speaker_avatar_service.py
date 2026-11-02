"""Swap a speaker profile's avatar without two requests ever sharing an object key.

Each upload writes its own key (``avatars/<uid>/<profile_uuid>/<random>.<ext>``) BEFORE any
lock is taken, so no lock is held across object-store I/O. The profile row is locked only for
the swap itself: read the key being replaced, point the row at the new one, commit. After the
commit the request deletes exactly the key it replaced. A concurrent request therefore either
replaced a different key or sees this request's key as the one to replace, and in no
interleaving does a delete name the key a profile currently points at.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg

from sqlalchemy.orm import Session

from app.models.media import SpeakerProfile
from app.services.speaker_profile_activity import touch_profile_interaction

logger = logging.getLogger(__name__)


def new_avatar_key(user_id: int, profile_uuid: object, ext: str) -> str:
    """A key no other upload will ever use."""
    return f"avatars/{user_id}/{profile_uuid}/{uuid_pkg.uuid4().hex}.{ext}"


def swap_avatar_path(db: Session, profile_id: int, new_path: str | None) -> str | None:
    """Point the profile at ``new_path`` (None clears it) and commit; return the key replaced.

    The row lock lives only inside this call: ``SELECT ... FOR UPDATE``, one assignment,
    commit. Raises whatever the database raises; the caller owns the rollback.
    """
    locked = (
        db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).with_for_update().one()
    )
    previous = str(locked.avatar_path) if locked.avatar_path else None
    locked.avatar_path = new_path  # type: ignore[assignment]
    touch_profile_interaction(db, profile_id, "human_edit")
    db.commit()
    return previous


def delete_avatar_object(key: str | None, *, current: str | None = None) -> None:
    """Best-effort delete of an avatar object this request owned (a leftover is a leak only).

    Args:
        key: The object to remove; None is a no-op.
        current: The key the profile now points at, if any; never deleted.
    """
    if not key or key == current:
        return
    from app.services.minio_service import delete_file

    try:
        delete_file(key)
    except Exception:
        # A leftover avatar object is a leak, not data loss; the request already succeeded.
        logger.warning("Failed to delete superseded avatar object %s", key, exc_info=True)
