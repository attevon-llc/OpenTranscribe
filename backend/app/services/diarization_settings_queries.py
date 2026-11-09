"""Lookup of a user's stored diarization-provider credential row.

Takes the request's ``Session`` and plain arguments; does not commit.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.user_diarization_settings import UserDiarizationSettings


def find_user_diarization_settings(
    db: Session, user_id: int, provider: str
) -> UserDiarizationSettings | None:
    """The user's stored row for diarization ``provider``, or ``None``."""
    return (
        db.query(UserDiarizationSettings)
        .filter(
            UserDiarizationSettings.user_id == user_id,
            UserDiarizationSettings.provider == provider,
        )
        .first()
    )
