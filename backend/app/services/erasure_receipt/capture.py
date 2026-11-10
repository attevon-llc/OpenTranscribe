"""Name what an erasure is about to destroy, before it is gone.

After the destroy Postgres can no longer say which files, speakers or objects the subject had, so
the post-check has nothing to look for. This reads them first, as plain data, in the caller's
short session (issue #440: no session across the storage and OpenSearch calls).
"""

from __future__ import annotations

import re
import uuid as uuid_pkg
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement
from sqlalchemy import exists
from sqlalchemy import or_
from sqlalchemy import select

from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.models.speaker import SpeakerProfile
from app.models.user import User
from app.services.erasure_receipt.types import Captured
from app.services.erasure_receipt.types import ErasureScope
from app.services.erasure_receipt.types import HeldVoices

if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlalchemy.orm import Session


def _file_filter(scope: ErasureScope) -> ColumnElement[bool]:
    if scope.kind == "user":
        return MediaFile.user_id == scope.user_id
    if scope.kind == "org_member":
        return (MediaFile.user_id == scope.user_id) & (
            MediaFile.organization_id == scope.organization_id
        )
    if scope.kind == "organization":
        return MediaFile.organization_id == scope.organization_id
    return MediaFile.id.in_(scope.file_ids)


def _owner_filter(
    model: type[SpeakerProfile] | type[SpeakerCluster], scope: ErasureScope
) -> ColumnElement[bool] | None:
    """Profiles and clusters: owned by the user (and tenant) or by the organization."""
    if scope.kind == "user":
        return model.user_id == scope.user_id
    if scope.kind == "org_member":
        return (model.user_id == scope.user_id) & (model.organization_id == scope.organization_id)
    if scope.kind == "organization":
        return model.organization_id == scope.organization_id
    return None


def _held_files() -> Select[tuple[int]]:
    from app.services.hold_guard import held_pred

    return select(MediaFile.id).where(held_pred())


def profile_keeps_held_voice() -> ColumnElement[bool]:
    """EXISTS a speaker of a held file linked to the enclosing query's ``SpeakerProfile``.

    Deleting such a profile would unlink the speaker (``speaker.profile_id`` is ``ON DELETE SET
    NULL``), rewriting the identity of a file the hold freezes: it is kept until the hold lifts.
    The privacy purge's twin is ``purge_file_guard.profile_keeps_protected_voice``.
    """
    return exists().where(
        Speaker.profile_id == SpeakerProfile.id, Speaker.media_file_id.in_(_held_files())
    )


def _held_speaker_filter(scope: ErasureScope) -> ColumnElement[bool]:
    """Speakers of held files whose voiceprint document the scope's stamp would reach."""
    in_scope = Speaker.media_file_id.in_(_held_files().where(_file_filter(scope)))
    if scope.kind == "user":
        return or_(
            in_scope, (Speaker.user_id == scope.user_id) & Speaker.media_file_id.in_(_held_files())
        )
    if scope.kind == "org_member":
        org_held = _held_files().where(MediaFile.organization_id == scope.organization_id)
        return or_(
            in_scope, (Speaker.user_id == scope.user_id) & Speaker.media_file_id.in_(org_held)
        )
    return in_scope


def held_voices(db: Session, scope: ErasureScope) -> HeldVoices:
    """What a legal hold keeps of the scope's voiceprints (Art. 17(3)(e), the hold freeze).

    The speakers of held files; the scope's profiles linked to one of them; the scope's clusters
    with one of them as a member or promoted to a kept profile.
    """
    speakers = db.execute(select(Speaker.id, Speaker.uuid).where(_held_speaker_filter(scope))).all()
    if not speakers:
        return HeldVoices()
    speaker_ids = [int(r.id) for r in speakers]
    profiles: list[tuple[int, str]] = []
    clusters: list[str] = []
    profile_scope = _owner_filter(SpeakerProfile, scope)
    if profile_scope is not None:
        profiles = [
            (int(r.id), str(r.uuid))
            for r in db.execute(
                select(SpeakerProfile.id, SpeakerProfile.uuid).where(
                    profile_scope, profile_keeps_held_voice()
                )
            )
        ]
    cluster_scope = _owner_filter(SpeakerCluster, scope)
    if cluster_scope is not None:
        holds_voice = or_(
            exists().where(
                SpeakerClusterMember.cluster_id == SpeakerCluster.id,
                SpeakerClusterMember.speaker_id.in_(speaker_ids),
            ),
            exists().where(Speaker.cluster_id == SpeakerCluster.id, Speaker.id.in_(speaker_ids)),
            SpeakerCluster.promoted_to_profile_id.in_([pid for pid, _ in profiles]),
        )
        clusters = [
            str(u)
            for u in db.execute(
                select(SpeakerCluster.uuid).where(cluster_scope, holds_voice)
            ).scalars()
        ]
    return HeldVoices(
        speaker_uuids=tuple(str(r.uuid) for r in speakers),
        profile_uuids=tuple(u for _, u in profiles),
        cluster_uuids=tuple(clusters),
    )


_PATH_COLUMNS = (MediaFile.storage_path, MediaFile.thumbnail_path, MediaFile.playback_path)


def file_dirs_of(file_id: int, paths: tuple[str | None, ...]) -> list[str]:
    """The ``user_<uploader>/file_<file_id>/`` directories this file's object keys sit in.

    Read from the keys themselves (``utils/filename.get_safe_storage_filename``), because the
    uploader in a key is not the file's current owner after a transfer. A key of any other shape
    names no directory: its object is removed by name by the canonical per-file destroy, and a
    prefix guessed from it could reach another file's objects.
    """
    pattern = re.compile(rf"user_\d+/file_{int(file_id)}/")
    dirs = []
    for path in paths:
        match = pattern.match(path or "")
        if match:
            dirs.append(match.group(0))
    return list(dict.fromkeys(dirs))


def _surviving_file_keys(db: Session, prefix: str, erased_file_ids: list[int]) -> list[str]:
    """What files the erasure does NOT destroy (held, or another account's) keep under ``prefix``.

    Each such file's own ``user_<uid>/file_<fid>/`` directory, and any of its keys of another shape
    by name (a prefix guessed from one could reach other objects). Empty when nothing survives.
    """
    under = or_(*(col.startswith(prefix, autoescape=True) for col in _PATH_COLUMNS))
    query = select(MediaFile.id, *_PATH_COLUMNS).where(under)
    if erased_file_ids:
        query = query.where(MediaFile.id.not_in(erased_file_ids))
    keep: list[str] = []
    for row in db.execute(query):
        paths = (row.storage_path, row.thumbnail_path, row.playback_path)
        dirs = [d for d in file_dirs_of(int(row.id), paths) if d.startswith(prefix)]
        keep.extend(dirs)
        keep.extend(
            p for p in paths if p and p.startswith(prefix) and not p.startswith(tuple(dirs))
        )
    return sorted(set(keep))


def _surviving_avatar_keys(db: Session, prefix: str, erased_profile_uuids: list[str]) -> list[str]:
    """What profiles the erasure does NOT delete keep under ``prefix``.

    A profile's ``avatars/<uid>/<profile uuid>/`` directory when its avatar sits in it
    (``speaker_avatar_service``), otherwise the avatar key itself.
    """
    query = select(SpeakerProfile.uuid, SpeakerProfile.avatar_path).where(
        SpeakerProfile.avatar_path.startswith(prefix, autoescape=True)
    )
    if erased_profile_uuids:
        query = query.where(
            SpeakerProfile.uuid.not_in([uuid_pkg.UUID(u) for u in erased_profile_uuids])
        )
    keep: set[str] = set()
    for row in db.execute(query):
        own_dir = f"{prefix}{row.uuid}/"
        keep.add(own_dir if str(row.avatar_path).startswith(own_dir) else str(row.avatar_path))
    return sorted(keep)


def capture(db: Session, scope: ErasureScope) -> Captured:
    """Read the subject's identifiers. Opens no transaction of its own and writes nothing."""
    from app.services.hold_guard import hold_states

    captured = Captured()
    rows = db.execute(
        select(MediaFile.id, MediaFile.uuid, *_PATH_COLUMNS).where(_file_filter(scope))
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
    captured.file_dirs = [
        d
        for r in kept
        for d in file_dirs_of(int(r.id), (r.storage_path, r.thumbnail_path, r.playback_path))
    ]

    if captured.file_ids:
        speakers = db.execute(
            select(Speaker.id, Speaker.uuid).where(Speaker.media_file_id.in_(captured.file_ids))
        ).all()
        captured.speaker_ids = [int(r.id) for r in speakers]
        captured.speaker_uuids = [str(r.uuid) for r in speakers]

    held_voice = held_voices(db, scope)
    captured.kept_voiceprint_ids = held_voice.doc_ids()
    for model, attr, keep in (
        (SpeakerProfile, "profile_uuids", set(held_voice.profile_uuids)),
        (SpeakerCluster, "cluster_uuids", set(held_voice.cluster_uuids)),
    ):
        condition = _owner_filter(model, scope)
        if condition is None:
            continue
        uuids = db.execute(select(model.uuid).where(condition)).scalars()
        setattr(captured, attr, [str(u) for u in uuids if str(u) not in keep])

    condition = _owner_filter(SpeakerProfile, scope)
    if condition is not None:
        captured.avatar_paths = [
            str(p)
            for p in db.execute(
                select(SpeakerProfile.avatar_path).where(
                    condition,
                    SpeakerProfile.avatar_path.is_not(None),
                    ~profile_keeps_held_voice(),
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
        # The whole prefix only when nothing under it outlives the erasure: a held file's objects
        # are retained, and a file the subject uploaded and an organization transfer handed to
        # someone else is that owner's, with its objects still under the subject's prefix.
        # When it is not taken whole, everything else under it still goes (``types.user_residue``).
        captured.user_prefix_keep = _surviving_file_keys(
            db, f"user_{scope.user_id}/", captured.file_ids
        )
        captured.whole_user_prefix = not captured.user_prefix_keep
        captured.avatar_prefix_keep = _surviving_avatar_keys(
            db, f"avatars/{scope.user_id}/", captured.profile_uuids
        )
        captured.whole_avatar_prefix = not captured.avatar_prefix_keep
        email = db.execute(select(User.email).where(User.id == scope.user_id)).scalar_one_or_none()
        captured.email = str(email) if email else None
    return captured
