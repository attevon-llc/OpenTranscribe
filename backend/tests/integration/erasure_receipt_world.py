"""A committed, fully-indexed subject for the erasure receipt integration tests (issue #1353).

Everything is created for real in Postgres, MinIO and OpenSearch, so the receipt's counts can be
checked against probes that share no code with the measurers. Users and files use the registered ``delres-e2e-`` and ``e2e-delres-`` shapes so a
killed run is findable, and ``World.cleanup`` removes whatever an erasure did not.
"""

from __future__ import annotations

import contextlib
import io
import uuid as uuid_pkg
from dataclasses import dataclass
from dataclasses import field

from sqlalchemy import text

SPEAKER_INDEX = "speakers_v4"


@dataclass
class World:
    user_id: int
    email: str
    org_id: int | None
    file_ids: list[int] = field(default_factory=list)
    file_uuids: list[str] = field(default_factory=list)
    speaker_uuids: list[str] = field(default_factory=list)
    profile_uuid: str = ""
    avatar_key: str = ""
    other_user_id: int = 0
    other_file_id: int = 0
    objects: list[tuple[str, str]] = field(default_factory=list)
    stray_indices: list[str] = field(default_factory=list)


def _db():
    from app.db.base import SessionLocal

    return SessionLocal()


def _put(bucket: str, key: str, world: World) -> None:
    from app.services.minio_service import minio_client

    if not minio_client.bucket_exists(bucket):
        minio_client.make_bucket(bucket)
    minio_client.put_object(bucket, key, io.BytesIO(b"erw"), 3)
    world.objects.append((bucket, key))


def _ensure_chunks_index() -> None:
    from app.core.config import settings
    from app.services.opensearch_service import opensearch_client

    if not opensearch_client.indices.exists(index=settings.OPENSEARCH_CHUNKS_INDEX):
        opensearch_client.indices.create(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            body={"mappings": {"properties": {"file_uuid": {"type": "keyword"}}}},
        )


def build_world(label: str = "user", *, org_id: int | None = None, files: int = 2) -> World:
    """A user with ``files`` indexed recordings, a speaker per file, a profile with an avatar,
    a comment on ANOTHER user's file, a chat conversation, and a lockout record."""
    from app.core.config import settings
    from app.core.security import get_password_hash
    from app.models.chat import ChatConversation
    from app.models.media import Comment
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.models.media import SpeakerProfile
    from app.models.user import User
    from app.services.opensearch_service import opensearch_client

    db = _db()
    try:
        tag = uuid_pkg.uuid4().hex[:10]
        users = []
        for who in ("subject", "other"):
            u = User(
                email=f"delres-e2e-erw{label}{who}-{tag}@example.com",
                full_name=f"erw {who}",
                hashed_password=get_password_hash("password123"),
                is_active=True,
                is_superuser=False,
                role="user",
            )
            db.add(u)
            users.append(u)
        db.commit()
        subject, other = users
        world = World(
            user_id=int(subject.id),
            email=str(subject.email),
            org_id=org_id,
            other_user_id=int(other.id),
        )

        def media(owner: User, org: int | None = None) -> MediaFile:
            fid = uuid_pkg.uuid4()
            row = MediaFile(
                uuid=fid,
                filename=f"e2e-delres-{fid.hex[:8]}.wav",
                storage_path=f"media/e2e-delres-{fid.hex}.wav",
                content_type="video/mp4",
                file_size=3,
                user_id=owner.id,
                organization_id=org,
                status="completed",
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

        other_file = media(other, org_id)
        world.other_file_id = int(other_file.id)
        db.add(Comment(media_file_id=other_file.id, user_id=subject.id, text="erw comment"))
        db.add(ChatConversation(user_id=subject.id, organization_id=org_id, title="erw chat"))
        profile = SpeakerProfile(user_id=subject.id, organization_id=org_id, name="erw profile")
        profile.avatar_path = f"avatars/{subject.id}/{uuid_pkg.uuid4()}.png"
        db.add(profile)
        db.commit()
        db.refresh(profile)
        world.profile_uuid, world.avatar_key = str(profile.uuid), str(profile.avatar_path)
        _put(settings.MEDIA_BUCKET_NAME, world.avatar_key, world)

        _ensure_chunks_index()
        for _ in range(files):
            row = media(subject, org_id)
            fid, fuuid = int(row.id), str(row.uuid)
            world.file_ids.append(fid)
            world.file_uuids.append(fuuid)
            prefix = f"user_{subject.id}/file_{fid}"
            _put(settings.MEDIA_BUCKET_NAME, f"{prefix}/original.mp4", world)
            _put(settings.MEDIA_BUCKET_NAME, f"{prefix}/thumbnail.jpg", world)
            # Not named by any file column: only a prefix sweep reaches it.
            _put(settings.MEDIA_BUCKET_NAME, f"{prefix}/stray.bin", world)
            _put(settings.MEDIA_BUCKET_NAME, f"temp/preprocess/{fuuid}/audio.wav", world)
            _put(settings.CACHE_BUCKET_NAME, f"derived/{fid}_erw", world)
            spk = Speaker(user_id=subject.id, media_file_id=row.id, name="SPEAKER_00")
            db.add(spk)
            db.commit()
            db.refresh(spk)
            world.speaker_uuids.append(str(spk.uuid))
            opensearch_client.index(
                index=settings.OPENSEARCH_TRANSCRIPT_INDEX,
                id=fuuid,
                body={"file_id": fid, "user_id": int(subject.id)},
            )
            opensearch_client.index(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body={"file_uuid": fuuid, "doc_type": "chunk"},
            )
            opensearch_client.index(
                index=SPEAKER_INDEX,
                id=str(spk.uuid),
                body={"user_id": int(subject.id)},
            )
        opensearch_client.indices.refresh(index="_all")

        from app.auth import lockout

        lockout._get_store().set(lockout._lockout_key(world.email.lower()), "{}")
        return world
    finally:
        db.close()


def cleanup(world: World) -> None:
    """Remove whatever the erasure left (best effort, every store). Never raises."""
    from app.core.config import settings
    from app.services.minio_service import minio_client
    from app.services.opensearch_service import opensearch_client

    for bucket, key in world.objects:
        with contextlib.suppress(Exception):
            minio_client.remove_object(bucket, key)
    for name in world.stray_indices:
        with contextlib.suppress(Exception):
            opensearch_client.indices.delete(index=name, ignore_unavailable=True)
    with contextlib.suppress(Exception):
        for index in (settings.OPENSEARCH_TRANSCRIPT_INDEX, settings.OPENSEARCH_CHUNKS_INDEX):
            opensearch_client.delete_by_query(
                index=index,
                body={
                    "query": {
                        "bool": {
                            "should": [
                                {"ids": {"values": world.file_uuids}},
                                {"terms": {"file_uuid": world.file_uuids}},
                            ]
                        }
                    }
                },
                conflicts="proceed",
            )
    ids = [world.user_id, world.other_user_id]
    db = _db()
    try:
        # The audit tables refuse DELETE; this cleanup owns the rows it made (owner-only switch).
        db.execute(text("SELECT set_config('opentranscribe.audit_maintenance', 'on', true)"))
        for stmt in (
            "DELETE FROM erasure_receipt WHERE ledger_entry_id IN "
            "(SELECT id FROM erasure_ledger WHERE subject_user_id = ANY(:ids))",
            "DELETE FROM erasure_ledger WHERE subject_user_id = ANY(:ids)",
            "DELETE FROM comment WHERE user_id = ANY(:ids)",
            "DELETE FROM media_file WHERE user_id = ANY(:ids)",
            'DELETE FROM "user" WHERE id = ANY(:ids)',
        ):
            db.execute(text(stmt), {"ids": ids})
        db.commit()
    except Exception:  # noqa: BLE001 - cleanup must not mask the test's own failure
        db.rollback()
    finally:
        db.close()
