#!/usr/bin/env python3
"""Repeatable end-to-end test runner for Watch Sources (issue #26).

Runs INSIDE the backend container against the live stack (Postgres, MinIO,
Redis/Celery, ffmpeg, and the GPU worker) — the host pytest harness can't do
this because it sets SKIP_CELERY/SKIP_REDIS/SKIP_S3. Each subtest sets up its
own data and cleans up after itself, so the script is safe to re-run.

Run it via the wrapper:  ./scripts/test-watch-e2e.sh
Or directly:            docker exec -w /app opentranscribe-backend \\
                            python scripts/e2e_watch_sources.py

Exit code 0 = all passed, 1 = a subtest failed.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import tempfile
import time
import uuid as uuid_pkg
from pathlib import Path

from app.core.config import settings
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.user import User
from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile
from app.services import minio_service
from app.tasks.watch_source_tasks import scan_all
from app.tasks.watch_source_tasks import scan_single

FOX = "the quick brown fox jumps over the lazy dog"
WATCH_ROOT = Path(settings.WATCH_FOLDER_PATH or "/watch")

_results: list[tuple[str, bool, str]] = []


def _settle(path: Path) -> None:
    """Backdate mtime past the file-stability window so the scan treats the file
    as fully written (otherwise a just-created file is skipped as 'still writing')."""
    import os

    past = time.time() - 600
    os.utime(path, (past, past))


def _admin_id(db) -> int:
    user = db.query(User).filter(User.email == "admin@example.com").first()
    if not user:
        user = db.query(User).order_by(User.id).first()
    return int(user.id)


def _gen_speech(dest: Path, text: str = FOX, seconds: int = 5) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 # nosec B603
        [
            "/usr/bin/ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"flite=text={text}:voice=slt",
            "-t",
            str(seconds),
            "-ar",
            "16000",
            str(dest),
        ],
        check=True,
    )
    _settle(dest)


def _gen_tone(dest: Path, freq: int = 440, seconds: int = 3) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 # nosec B603
        [
            "/usr/bin/ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}",
            "-c:a",
            "aac",
            str(dest),
        ],
        check=True,
    )
    _settle(dest)


def _delete_source(source_id: int) -> None:
    with session_scope() as db:
        for row in (
            db.query(WatchSourceFile).filter(WatchSourceFile.watch_source_id == source_id).all()
        ):
            if row.media_file_id:
                mf = db.get(MediaFile, row.media_file_id)
                if mf and mf.storage_path:
                    with contextlib.suppress(Exception):
                        minio_service.delete_file(str(mf.storage_path))
                if mf:
                    db.delete(mf)
        src = db.get(WatchSource, source_id)
        if src:
            db.delete(src)


def record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


# --------------------------------------------------------------------------- #
# Test 1 — Beat orchestrator due-logic (#3)
# --------------------------------------------------------------------------- #
def test_beat_due_logic() -> None:
    from datetime import datetime
    from datetime import timedelta
    from datetime import timezone

    sub = f"e2e_beat_{uuid_pkg.uuid4().hex[:8]}"
    (WATCH_ROOT / sub).mkdir(parents=True, exist_ok=True)
    due_id = not_due_id = None
    try:
        with session_scope() as db:
            uid = _admin_id(db)
            now = datetime.now(timezone.utc)
            due = WatchSource(
                uuid=uuid_pkg.uuid4(),
                name="e2e beat due",
                source_type="local",
                user_id=uid,
                created_by=uid,
                local_path=sub,
                is_enabled=True,
                polling_interval_minutes=15,
                last_scan_at=now - timedelta(hours=1),
                auto_transcribe=False,
            )
            not_due = WatchSource(
                uuid=uuid_pkg.uuid4(),
                name="e2e beat not-due",
                source_type="local",
                user_id=uid,
                created_by=uid,
                local_path=sub,
                is_enabled=True,
                polling_interval_minutes=60,
                last_scan_at=now,
                auto_transcribe=False,
            )
            db.add_all([due, not_due])
            db.flush()
            due_id, not_due_id = due.id, not_due.id

        # Capture which sources scan_all dispatches without running real scans.
        dispatched: list[int] = []
        orig = scan_single.delay
        scan_single.delay = lambda sid: dispatched.append(sid)  # type: ignore[assignment]
        try:
            scan_all.run()
        finally:
            scan_single.delay = orig  # type: ignore[assignment]

        ok = due_id in dispatched and not_due_id not in dispatched
        record(
            "beat due-logic dispatches only due sources",
            ok,
            f"(due dispatched={due_id in dispatched}, not-due skipped={not_due_id not in dispatched})",
        )
    except Exception as e:  # noqa: BLE001
        record("beat due-logic", False, f"error: {e}")
    finally:
        for sid in (due_id, not_due_id):
            if sid:
                _delete_source(sid)
        import shutil

        shutil.rmtree(WATCH_ROOT / sub, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Test 2 — Local import → REAL transcription to COMPLETED on the GPU (#2)
# --------------------------------------------------------------------------- #
def test_import_and_transcribe(timeout: int = 360) -> None:
    sub = f"e2e_tx_{uuid_pkg.uuid4().hex[:8]}"
    audio = WATCH_ROOT / sub / "fox.wav"
    source_id = None
    try:
        _gen_speech(audio)
        with session_scope() as db:
            uid = _admin_id(db)
            src = WatchSource(
                uuid=uuid_pkg.uuid4(),
                name="e2e transcribe",
                source_type="local",
                user_id=uid,
                created_by=uid,
                local_path=sub,
                is_enabled=True,
                auto_transcribe=True,
                skip_files_older_than_days=None,
            )
            db.add(src)
            db.flush()
            source_id = src.id

        scan_single.run(source_id)  # imports + dispatches transcription to GPU

        # Find the imported MediaFile id.
        with session_scope() as db:
            row = (
                db.query(WatchSourceFile)
                .filter(
                    WatchSourceFile.watch_source_id == source_id,
                    WatchSourceFile.status == "imported",
                )
                .first()
            )
            if not row or not row.media_file_id:
                record("import+transcribe", False, "file did not import")
                return
            media_id = row.media_file_id

        # Poll until terminal status.
        deadline = time.time() + timeout
        status = None
        while time.time() < deadline:
            with session_scope() as db:
                mf = db.get(MediaFile, media_id)
                status = mf.status if mf else None
            if status in (FileStatus.COMPLETED, FileStatus.ERROR, FileStatus.ORPHANED):
                break
            time.sleep(5)

        if status != FileStatus.COMPLETED:
            record("import+transcribe reaches COMPLETED", False, f"final status={status}")
            return
        record("import+transcribe reaches COMPLETED", True, "")

        # Verify a real transcript came out and matches the spoken words.
        with session_scope() as db:
            segs = (
                db.query(TranscriptSegment)
                .filter(TranscriptSegment.media_file_id == media_id)
                .all()
            )
            full = " ".join((s.text or "") for s in segs).lower()
        hits = [w for w in ("quick", "brown", "fox", "lazy", "dog", "jump") if w in full]
        record(
            "transcript contains spoken words",
            len(hits) >= 2,
            f"({len(segs)} segments, matched {hits})",
        )
    except Exception as e:  # noqa: BLE001
        record("import+transcribe", False, f"error: {e}")
    finally:
        if source_id:
            _delete_source(source_id)
        import shutil

        shutil.rmtree(WATCH_ROOT / sub, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Test 3 — Three-layer dedup
# --------------------------------------------------------------------------- #
def test_dedup() -> None:
    sub = f"e2e_dup_{uuid_pkg.uuid4().hex[:8]}"
    d = WATCH_ROOT / sub
    source_id = None
    try:
        _gen_tone(d / "a.mp4")
        import shutil as sh

        sh.copy2(d / "a.mp4", d / "b.mp4")  # identical content → dedup
        with session_scope() as db:
            uid = _admin_id(db)
            src = WatchSource(
                uuid=uuid_pkg.uuid4(),
                name="e2e dedup",
                source_type="local",
                user_id=uid,
                created_by=uid,
                local_path=sub,
                is_enabled=True,
                auto_transcribe=False,
                skip_files_older_than_days=None,
            )
            db.add(src)
            db.flush()
            source_id = src.id
        scan_single.run(source_id)
        with session_scope() as db:
            rows = (
                db.query(WatchSourceFile).filter(WatchSourceFile.watch_source_id == source_id).all()
            )
            imported = sum(1 for r in rows if r.status == "imported")
            dup = sum(1 for r in rows if r.status == "skipped_duplicate")
        record(
            "dedup: 1 imported + 1 duplicate-skipped",
            imported == 1 and dup == 1,
            f"(imported={imported}, dup={dup})",
        )
    except Exception as e:  # noqa: BLE001
        record("dedup", False, f"error: {e}")
    finally:
        if source_id:
            _delete_source(source_id)
        import shutil

        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Test 4 — Multi-part stitching (async on the CPU worker)
# --------------------------------------------------------------------------- #
def test_multipart(timeout: int = 180) -> None:
    sub = f"e2e_mp_{uuid_pkg.uuid4().hex[:8]}"
    d = WATCH_ROOT / sub
    source_id = None
    try:
        for n in (1, 2, 3):
            _gen_tone(d / f"meeting_P{n:03d}.mp4", freq=300 + n * 60)
        with session_scope() as db:
            uid = _admin_id(db)
            src = WatchSource(
                uuid=uuid_pkg.uuid4(),
                name="e2e multipart",
                source_type="local",
                user_id=uid,
                created_by=uid,
                local_path=sub,
                is_enabled=True,
                auto_transcribe=False,
                skip_files_older_than_days=None,
                multipart_enabled=True,
                file_extensions=".mp4",
            )
            db.add(src)
            db.flush()
            source_id = src.id
        scan_single.run(source_id)  # dispatches stitch_and_import to the CPU worker

        deadline = time.time() + timeout
        stitched = parts = 0
        while time.time() < deadline:
            with session_scope() as db:
                rows = (
                    db.query(WatchSourceFile)
                    .filter(WatchSourceFile.watch_source_id == source_id)
                    .all()
                )
                stitched = sum(1 for r in rows if r.status == "imported" and r.part_group)
                parts = sum(1 for r in rows if r.status == "stitched_part")
            if stitched >= 1 and parts == 3:
                break
            time.sleep(3)
        record(
            "multipart: 3 parts stitched into 1 file",
            stitched >= 1 and parts == 3,
            f"(stitched={stitched}, parts={parts})",
        )
    except Exception as e:  # noqa: BLE001
        record("multipart", False, f"error: {e}")
    finally:
        if source_id:
            _delete_source(source_id)
        import shutil

        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Test 5 — S3 import (self-seeded MinIO bucket via boto3)
# --------------------------------------------------------------------------- #
def test_s3_import() -> None:
    import os

    import boto3

    bucket = f"e2e-watch-{uuid_pkg.uuid4().hex[:8]}"
    tmp = Path(tempfile.gettempdir()) / f"{bucket}.mp4"
    source_id = None
    s3 = None
    try:
        _gen_tone(tmp)
        s3 = boto3.client(
            "s3",
            endpoint_url="http://minio:9000",
            aws_access_key_id=os.getenv("MINIO_ROOT_USER", "minioadmin"),
            aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD", "minioadmin"),
            use_ssl=False,
        )
        s3.create_bucket(Bucket=bucket)
        s3.upload_file(str(tmp), bucket, "clip.mp4")
        from app.utils.encryption import encrypt_api_key

        with session_scope() as db:
            uid = _admin_id(db)
            src = WatchSource(
                uuid=uuid_pkg.uuid4(),
                name="e2e s3",
                source_type="s3",
                user_id=uid,
                created_by=uid,
                is_enabled=True,
                auto_transcribe=False,
                s3_endpoint_url="http://minio:9000",
                s3_bucket_name=bucket,
                s3_access_key_id=os.getenv("MINIO_ROOT_USER", "minioadmin"),
                encrypted_s3_secret_key=encrypt_api_key(
                    os.getenv("MINIO_ROOT_PASSWORD", "minioadmin")
                ),
                s3_use_ssl=False,
                skip_files_older_than_days=None,
            )
            db.add(src)
            db.flush()
            source_id = src.id
        res = scan_single.run(source_id)
        record("s3 import (boto3 → MinIO)", res.get("imported", 0) == 1, f"({res})")
    except Exception as e:  # noqa: BLE001
        record("s3 import", False, f"error: {e}")
    finally:
        if source_id:
            _delete_source(source_id)
        if s3:
            with contextlib.suppress(Exception):
                for obj in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
                    s3.delete_object(Bucket=bucket, Key=obj["Key"])
                s3.delete_bucket(Bucket=bucket)
        tmp.unlink(missing_ok=True)


def main() -> int:
    print(f"Watch Sources E2E — watch root {WATCH_ROOT}\n")
    test_beat_due_logic()
    test_dedup()
    test_s3_import()
    test_multipart()
    test_import_and_transcribe()  # GPU — slowest, run last
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    for name, ok, detail in _results:
        print(f"  {'✓' if ok else '✗'} {name} {detail}")
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
