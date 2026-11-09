"""Speaker-profile listing for the admin maintenance routes. Does not commit."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.speaker import SpeakerProfile


def list_all_speaker_profiles(db: Session) -> list[SpeakerProfile]:
    """Every speaker profile, across all owners (deployment-scope maintenance only)."""
    return db.query(SpeakerProfile).all()
