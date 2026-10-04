#!/usr/bin/env python3
"""Fix files incorrectly marked as ERROR due to system issues.

This script analyzes all ERROR files and:
1. Marks files that have transcripts but are ERROR as COMPLETED
2. Resets retriable system errors (duplicate key, worker lost, OOM) to PENDING
3. Tags permanent errors (private/removed) with error_category for tracking

Usage:
    docker exec opentranscribe-backend python3 /app/scripts/fix_error_status.py
    docker exec opentranscribe-backend python3 /app/scripts/fix_error_status.py --dry-run
"""

import argparse
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.media import FileStatus  # noqa: E402
from app.models.media import MediaFile  # noqa: E402
from app.models.media import TranscriptSegment  # noqa: E402
from app.utils.error_classification import ErrorCategory  # noqa: E402
from app.utils.error_classification import categorize_error  # noqa: E402
from app.utils.error_classification import should_retry  # noqa: E402
from app.utils.task_utils import reset_file_for_retry  # noqa: E402
from app.utils.task_utils import update_media_file_status  # noqa: E402

RETRIABLE_CATEGORIES = (
    ErrorCategory.DUPLICATE_KEY,
    ErrorCategory.WORKER_LOST,
    ErrorCategory.OOM_ERROR,
    ErrorCategory.GPU_OOM,
    ErrorCategory.SYSTEM_ERROR,
    ErrorCategory.UNKNOWN,
)


def _categorize_files(db, error_files):
    """Categorize error files into completed, retriable, and permanent."""
    completed = []
    retriable = []
    permanent = []

    for file in error_files:
        segment_count = (
            db.query(TranscriptSegment).filter(TranscriptSegment.media_file_id == file.id).count()
        )
        error_category = categorize_error(file.last_error_message or "")

        if segment_count > 0:
            completed.append((file, error_category, segment_count))
        elif error_category in RETRIABLE_CATEGORIES and should_retry(
            error_category, int(file.retry_count)
        ):
            retriable.append((file, error_category))
        else:
            permanent.append((file, error_category))

    return completed, retriable, permanent


def _fix_completed_files(db, files, dry_run):
    """Mark files with transcripts as COMPLETED."""
    print("=== Fixing Actually-Completed Files ===")
    count = 0
    for file, category, segments in files:
        try:
            filename = str(file.filename)[:50] if file.filename else "unknown"
            if dry_run:
                print(
                    f"  [DRY RUN] Would mark COMPLETED: {file.uuid} - "
                    f"{filename} ({segments} segments, {category.value})"
                )
            else:
                file.error_category = category.value
                update_media_file_status(db, int(file.id), FileStatus.COMPLETED)
                print(
                    f"  Marked COMPLETED: {file.uuid} - "
                    f"{filename} ({segments} segments, {category.value})"
                )
            count += 1
        except Exception as e:
            print(f"  ERROR fixing {file.uuid}: {e}")
            db.rollback()

    print(f"\nFixed {count}/{len(files)} actually-completed files\n")
    return count


def _reset_retriable_files(db, files, dry_run):
    """Reset retriable errors for retry."""
    print("=== Resetting Retriable Errors ===")
    count = 0
    for file, category in files:
        try:
            filename = str(file.filename)[:50] if file.filename else "unknown"
            if dry_run:
                print(f"  [DRY RUN] Would reset: {file.uuid} - {filename} ({category.value})")
                count += 1
            else:
                file.error_category = category.value
                db.commit()

                if reset_file_for_retry(db, int(file.id), reset_retry_count=True):
                    from app.tasks.transcription import transcribe_audio_task

                    transcribe_audio_task.delay(str(file.uuid))
                    count += 1
                    print(f"  Reset: {file.uuid} - {filename} ({category.value})")
                else:
                    print(f"  Failed to reset {file.uuid}")
        except Exception as e:
            print(f"  ERROR resetting {file.uuid}: {e}")
            db.rollback()

    print(f"\nReset {count}/{len(files)} retriable files\n")
    return count


def _tag_permanent_errors(db, files, dry_run):
    """Tag permanent errors with error_category for tracking."""
    print("=== Tagging Permanent Errors ===")
    category_counts: dict[str, int] = {}
    for file, category in files:
        category_counts[category.value] = category_counts.get(category.value, 0) + 1
        if not dry_run:
            file.error_category = category.value

    if not dry_run:
        try:
            db.commit()
        except Exception as e:
            print(f"  ERROR committing tags: {e}")
            db.rollback()

    print(f"Tagged {len(files)} permanent failures:")
    for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")

    return len(files)


def main():
    parser = argparse.ArgumentParser(description="Fix files incorrectly marked as ERROR")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without making changes"
    )
    args = parser.parse_args()

    engine = create_engine(settings.DATABASE_URL)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()

    try:
        error_files = db.query(MediaFile).filter(MediaFile.status == FileStatus.ERROR).all()
        print(f"Found {len(error_files)} files in ERROR status\n")

        completed, retriable, permanent = _categorize_files(db, error_files)

        print("=== ERROR Files Analysis ===")
        print(f"Actually COMPLETED (has transcript): {len(completed)}")
        print(f"Retriable system errors: {len(retriable)}")
        print(f"Permanent failures: {len(permanent)}")
        print()

        if args.dry_run:
            print("=== DRY RUN MODE - No changes will be made ===\n")

        completed_count = _fix_completed_files(db, completed, args.dry_run)
        retry_count = _reset_retriable_files(db, retriable, args.dry_run)
        tagged_count = _tag_permanent_errors(db, permanent, args.dry_run)

        print("\n=== Summary ===")
        print(f"Files marked COMPLETED: {completed_count}")
        print(f"Files reset for retry: {retry_count}")
        print(f"Files tagged as permanent error: {tagged_count}")
        if args.dry_run:
            print("\n(DRY RUN - no changes were made)")

    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    main()
