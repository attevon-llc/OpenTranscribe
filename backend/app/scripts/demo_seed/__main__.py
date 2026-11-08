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

from app.scripts.demo_seed.bundle import BundleError
from app.scripts.demo_seed.bundle import SeedItem
from app.scripts.demo_seed.bundle import load_bundle

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


def _seed_item(item: SeedItem, bundle_version: str, user_id: int) -> None:
    """Rows, then the media upload with no session open, then a short write and the tasks."""
    from app.db.base import SessionLocal
    from app.models.media import MediaFile
    from app.models.speaker import Speaker
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
        for label, display in item.speaker_names().items():
            db.query(Speaker).filter(
                Speaker.media_file_id == record.media_file_id, Speaker.name == label
            ).update({"display_name": display, "verified": True})
        media_file = db.get(MediaFile, record.media_file_id)
        if media_file is None:
            raise RuntimeError(f"{item.id}: the injected media row vanished before upload")
        storage_path = media_file.storage_path
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
    for item in items:
        _seed_item(item, version, user_id)
        print(f"seeded {item.id} ({item.license})")
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
            print(f"bundle {version}: {len(items)} item(s), all licences allowed")
            return EXIT_OK
        return _import(args.bundle)
    except BundleError as exc:
        for problem in exc.problems:
            print(f"bundle: {problem}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
