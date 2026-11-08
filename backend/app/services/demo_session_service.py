"""Database reads behind the public demo's sign-in and licence credit (issue #628)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.demo_info import DEMO_LICENSE_KEY
from app.models.media import MediaFile
from app.models.user import User


def find_demo_user(db: Session, email: str) -> User | None:
    """The configured demo account, or ``None`` when none is configured or it does not exist."""
    if not email:
        return None
    return db.query(User).filter(User.email == email).first()


def list_demo_attributions(db: Session, user_id: int) -> list[dict[str, str]]:
    """Licence and credit the seeder recorded for each file this user owns."""
    rows = (
        db.query(MediaFile.uuid, MediaFile.metadata_important)
        .filter(
            MediaFile.user_id == user_id,
            MediaFile.metadata_important[DEMO_LICENSE_KEY].isnot(None),
        )
        .all()
    )
    items = []
    for file_uuid, meta in rows:
        lic = (meta or {}).get(DEMO_LICENSE_KEY) or {}
        items.append(
            {
                "file_uuid": str(file_uuid),
                "license": str(lic.get("license", "")),
                "license_url": str(lic.get("license_url", "")),
                "attribution": str(lic.get("attribution", "")),
                "source_url": str(lic.get("source_url", "")),
            }
        )
    return items
