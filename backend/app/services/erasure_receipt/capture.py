"""Name what an erasure is about to destroy, before it is gone.

After the destroy Postgres can no longer say which files, speakers or objects the subject had, so
the post-check has nothing to look for. This reads them first, as plain data, in the caller's
short session (issue #440: no session across the storage and OpenSearch calls).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.models.media import SpeakerProfile
from app.models.user import User
from app.services.erasure_receipt.types import Captured
from app.services.erasure_receipt.types import ErasureScope

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _file_filter(scope: ErasureScope):
    if scope.kind == "user":
        return MediaFile.user_id == scope.user_id
    if scope.kind == "org_member":
        return (MediaFile.user_id == scope.user_id) & (
            MediaFile.organization_id == scope.organization_id
        )
    if scope.kind == "organization":
        return MediaFile.organization_id == scope.organization_id
    return MediaFile.id.in_(scope.file_ids)


def _owner_filter(model, scope: ErasureScope):
    """Profiles and clusters: owned by the user (and tenant) or by the organization."""
    if scope.kind == "user":
        return model.user_id == scope.user_id
    if scope.kind == "org_member":
        return (model.user_id == scope.user_id) & (model.organization_id == scope.organization_id)
    if scope.kind == "organization":
        return model.organization_id == scope.organization_id
    return None


def capture(db: Session, scope: ErasureScope) -> Captured:
    """Read the subject's identifiers. Opens no transaction of its own and writes nothing."""
    from app.services.hold_guard import hold_states

    captured = Captured()
    rows = db.execute(
        select(MediaFile.id, MediaFile.uuid, MediaFile.user_id).where(_file_filter(scope))
    ).all()
    all_ids = [int(r.id) for r in rows]
    if all_ids:
        captured.held_file_ids = sorted(int(i) for i in hold_states(db, all_ids))
    # A file a legal hold keeps is retained (Art. 17(3)(e)), so nothing about it is expected to
    # be gone: it is named in ``held_file_ids`` only, and every per-file selector below excludes it.
    held = set(captured.held_file_ids)
    kept = [r for r in rows if int(r.id) not in held]
    captured.file_ids = [int(r.id) for r in kept]
    captured.file_uuids = [str(r.uuid) for r in kept]
    captured.file_owners = [(int(r.user_id), int(r.id)) for r in kept]

    if captured.file_ids:
        speakers = db.execute(
            select(Speaker.id, Speaker.uuid).where(Speaker.media_file_id.in_(captured.file_ids))
        ).all()
        captured.speaker_ids = [int(r.id) for r in speakers]
        captured.speaker_uuids = [str(r.uuid) for r in speakers]

    for model, attr in ((SpeakerProfile, "profile_uuids"), (SpeakerCluster, "cluster_uuids")):
        condition = _owner_filter(model, scope)
        if condition is None:
            continue
        setattr(captured, attr, [str(u) for u in db.execute(select(model.uuid).where(condition))])

    condition = _owner_filter(SpeakerProfile, scope)
    if condition is not None:
        captured.avatar_paths = [
            str(p)
            for p in db.execute(
                select(SpeakerProfile.avatar_path).where(
                    condition, SpeakerProfile.avatar_path.is_not(None)
                )
            )
            .scalars()
            .all()
            if p
        ]

    if scope.kind == "organization":
        from app.models.content_hold import ContentHold

        captured.org_hold_active = bool(
            db.execute(
                select(ContentHold.id)
                .where(
                    ContentHold.organization_id == scope.organization_id,
                    ContentHold.released_at.is_(None),
                )
                .limit(1)
            ).first()
        )

    if scope.kind == "user" and scope.user_id is not None:
        email = db.execute(select(User.email).where(User.id == scope.user_id)).scalar_one_or_none()
        captured.email = str(email) if email else None
    return captured
