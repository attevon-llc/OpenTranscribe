"""Seed the public demo from a bundle (issue #628): ``validate`` or ``import``.

    python -m app.scripts.demo_seed validate --bundle /seed
    python -m app.scripts.demo_seed import --bundle /seed

``import`` runs only inside ``scripts/demo/demo-reset.sh``'s seed phase: a one-off backend
container with ``DEMO_MODE=false``, the public port closed, and a database that ``down -v``
has just emptied. It refuses to run with ``DEMO_MODE`` on.

Rows come from the same builder the evaluation corpus uses
(``corpus_injection.injector.inject_meeting``: ``MediaFile`` / ``Speaker`` /
``TranscriptSegment`` with status COMPLETED, no speech recognition). This adds what a visitor
needs and the evaluation corpus does not: the real media object in storage (a player that
404s is worse than no demo), the speakers' display names, and the production indexing and
redaction tasks run in this process (``mode="eager"``), so the demo is settled when the
command exits. It exits non-zero, and the reset keeps the public port closed, if any file is
still pending a redaction scan afterwards.

The demo account is ``DEMO_USER_EMAIL`` / ``DEMO_USER_PASSWORD`` (both required), an
ordinary ``user``: never an administrator.
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path

from app.core.demo_info import DEMO_LICENSE_KEY
from app.scripts.demo_seed.bundle import BundleError
from app.scripts.demo_seed.bundle import SeedItem
from app.scripts.demo_seed.bundle import load_bundle
from app.scripts.demo_seed.conversations import SeedConversation
from app.scripts.demo_seed.conversations import load_conversations

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _ensure_demo_user(db, email: str, password: str) -> int:
    from app.auth.roles import ROLE_USER
    from app.auth.roles import role_implies_superuser
    from app.core.security import get_password_hash
    from app.models.user import User

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(
            email=email,
            full_name="Demo Visitor",
            hashed_password=get_password_hash(password),
            role=ROLE_USER,
            is_superuser=role_implies_superuser(ROLE_USER),
            email_verified=True,
            password_changed_at=datetime.now(UTC),
            approval_status="approved",
            approved_at=datetime.now(UTC),
        )
        db.add(user)
        db.flush()
    return int(user.id)


def _to_doc(item: SeedItem, bundle_version: str):
    from app.scripts.corpus_injection.model import TIMING_REAL
    from app.scripts.corpus_injection.model import MeetingDoc
    from app.scripts.corpus_injection.model import TimingInfo
    from app.scripts.corpus_injection.model import Turn

    segments = item.segments()
    turns = [
        Turn(turn_index=n, speaker=s.speaker, text=s.text, start=s.start, end=s.end)
        for n, s in enumerate(segments)
    ]
    timing = TimingInfo(
        source=TIMING_REAL,
        reference=f"demo-seed:{bundle_version}:{item.id}",
        aligned_turns=len(turns),
        total_turns=len(turns),
    )
    return MeetingDoc(
        corpus="demo",
        meeting_id=item.id,
        title=item.title,
        turns=turns,
        language=item.language,
        timing=timing,
        extra={
            "license": item.license,
            "attribution": item.attribution,
            "source_url": item.source_url,
        },
    )


def _name_speakers(db, media_file_id: int, names: dict[str, str]) -> None:
    """Give the injected speakers the bundle's names, recorded as a person's.

    The bundle's curator typed these names, so they are ``human`` (a voiceprint purge or
    switching voiceprints off must never hide them); no account in this database is the
    curator's, so no user is recorded.
    """
    from app.models.speaker import Speaker
    from app.utils.speaker_identity_detail import ORIGIN_TYPED
    from app.utils.speaker_identity_source import record_human_identity

    for speaker in db.query(Speaker).filter(
        Speaker.media_file_id == media_file_id, Speaker.name.in_(names)
    ):
        speaker.display_name = names[speaker.name]
        speaker.verified = True
        record_human_identity(speaker, user_id=None, origin=ORIGIN_TYPED)


def _seed_item(item: SeedItem, bundle_version: str, user_id: int) -> str:
    """Rows, then the media upload with no session open, then a short write and the tasks.

    Returns the seeded file's UUID, which conversations cite.
    """
    from app.db.base import SessionLocal
    from app.models.media import MediaFile
    from app.scripts.corpus_injection import injector
    from app.services import minio_service

    with SessionLocal() as db:
        record, _ = injector.inject_meeting(
            db,
            _to_doc(item, bundle_version),
            user_id,
            seed=bundle_version,
            tool_version="demo_seed",
            force=True,
        )
        _name_speakers(db, record.media_file_id, item.speaker_names())
        media_file = db.get(MediaFile, record.media_file_id)
        if media_file is None:
            raise RuntimeError(f"{item.id}: the injected media row vanished before upload")
        storage_path = media_file.storage_path
        file_uuid = str(media_file.uuid)
        # The licence the item was published under, in a key the product may read (the
        # injector's own ``rag_eval`` block is the evaluation harness's, not the product's);
        # `GET /auth/demo/attributions` serves it so CC BY credit shows wherever the file is.
        media_file.metadata_important = {
            **(media_file.metadata_important or {}),
            DEMO_LICENSE_KEY: {
                "license": item.license,
                "license_url": item.license_url,
                "attribution": item.attribution,
                "source_url": item.source_url,
            },
        }
        db.commit()

    content_type = mimetypes.guess_type(item.media.name)[0] or "application/octet-stream"
    size = item.media.stat().st_size
    with item.media.open("rb") as fh:
        minio_service.upload_file(fh, size, storage_path, content_type)

    with SessionLocal() as db:
        db.query(MediaFile).filter(MediaFile.id == record.media_file_id).update(
            {"filename": item.media.name, "content_type": content_type, "file_size": size}
        )
        db.commit()
    injector.dispatch_indexing(record, user_id, mode="eager")
    with SessionLocal() as db:
        injector.dispatch_redaction(db, record, user_id, mode="eager")
    return file_uuid


def _seed_conversations(
    db, user_id: int, conversations: list[SeedConversation], uuid_by_item: dict[str, str]
) -> None:
    """Store each conversation as finished, with the citations pointing at the seeded files."""
    from datetime import timedelta

    from app.models.chat import ChatConversation
    from app.models.chat import ChatMessage

    now = datetime.now(UTC)
    for n, conv in enumerate(conversations):
        stamp = now - timedelta(minutes=n)
        row = ChatConversation(
            user_id=user_id,
            title=conv.title,
            context={"file_uuids": [uuid_by_item[i] for i in conv.item_ids]},
            last_message_at=stamp,
        )
        db.add(row)
        db.flush()
        for msg in conv.messages:
            citations = [
                {
                    "id": k + 1,
                    "kind": c.get("kind", "chunk"),
                    "file_uuid": uuid_by_item[c["item_id"]],
                    "title": c.get("title", ""),
                    "chunk_index": c.get("chunk_index", 0),
                    "start_time": c.get("start_time", 0.0),
                    "end_time": c.get("end_time"),
                    "speaker": c.get("speaker"),
                    "snippet": c.get("snippet", ""),
                }
                for k, c in enumerate(msg.get("citations") or [])
            ]
            db.add(
                ChatMessage(
                    conversation_id=row.id,
                    role=msg["role"],
                    content=msg["content"],
                    reasoning_content=msg.get("reasoning_content"),
                    citations=citations or None,
                    provider=msg.get("provider"),
                    model=msg.get("model"),
                    status="complete",
                )
            )


def _file_into_collections(
    db, user_id: int, items: list[SeedItem], uuid_by_item: dict[str, str]
) -> None:
    """One personal collection per named bundle collection, owned by the demo user."""
    from app.models.collection import Collection
    from app.models.collection import CollectionMember
    from app.models.media import MediaFile
    from app.scripts.demo_seed.bundle import COLLECTIONS

    rows: dict[str, Collection] = {}
    for item in items:
        if item.collection not in rows:
            row = Collection(
                name=COLLECTIONS[item.collection],
                user_id=user_id,
                is_public=False,
                source="manual",
            )
            db.add(row)
            db.flush()
            rows[item.collection] = row
        media_id = db.query(MediaFile.id).filter(MediaFile.uuid == uuid_by_item[item.id]).scalar()
        db.add(
            CollectionMember(
                collection_id=rows[item.collection].id, media_file_id=media_id, source="manual"
            )
        )
    db.flush()


def _pending_redactions(db) -> int:
    from app.core.redaction_defaults import REDACTION_STATUS_PENDING
    from app.models.media import MediaFile

    return int(
        db.query(MediaFile).filter(MediaFile.redaction_status == REDACTION_STATUS_PENDING).count()
    )


def _import(bundle: Path) -> int:
    from app.core.config import settings
    from app.db.base import SessionLocal

    if settings.DEMO_MODE:
        print("refusing to seed with DEMO_MODE on: run it from demo-reset.sh", file=sys.stderr)
        return EXIT_USAGE
    email, password = settings.DEMO_USER_EMAIL, settings.DEMO_USER_PASSWORD
    if not email or not password:
        print("DEMO_USER_EMAIL and DEMO_USER_PASSWORD are required", file=sys.stderr)
        return EXIT_USAGE
    version, items = load_bundle(bundle)
    with SessionLocal() as db:
        user_id = _ensure_demo_user(db, email, password)
        db.commit()
    conversations = load_conversations(bundle, {i.id for i in items})
    uuid_by_item: dict[str, str] = {}
    for item in items:
        uuid_by_item[item.id] = _seed_item(item, version, user_id)
        print(f"seeded {item.id} ({item.license})")
    with SessionLocal() as db:
        _file_into_collections(db, user_id, items, uuid_by_item)
        _seed_conversations(db, user_id, conversations, uuid_by_item)
        db.commit()
    print(f"seeded {len(conversations)} conversation(s)")
    with SessionLocal() as db:
        pending = _pending_redactions(db)
    if pending:
        print(f"{pending} file(s) still pending a redaction scan", file=sys.stderr)
        return EXIT_FAILED
    print(f"demo seed {version}: {len(items)} item(s)")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the exit code."""
    parser = argparse.ArgumentParser(prog="python -m app.scripts.demo_seed")
    parser.add_argument("command", choices=("validate", "import"))
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            version, items = load_bundle(args.bundle)
            conversations = load_conversations(args.bundle, {i.id for i in items})
            print(
                f"bundle {version}: {len(items)} item(s), all licences allowed, "
                f"{len(conversations)} conversation(s)"
            )
            return EXIT_OK
        return _import(args.bundle)
    except BundleError as exc:
        for problem in exc.problems:
            print(f"bundle: {problem}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
