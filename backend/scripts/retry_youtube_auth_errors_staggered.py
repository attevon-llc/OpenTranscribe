#!/usr/bin/env python3
"""Retry YouTube auth errors with staggered delays to avoid rate limiting.

Resets false-positive auth error files in small batches with delays between
batches to prevent hitting YouTube rate limits again.

Usage:
    docker exec opentranscribe-backend python3 /app/scripts/retry_youtube_auth_errors_staggered.py
    docker exec opentranscribe-backend python3 /app/scripts/retry_youtube_auth_errors_staggered.py --dry-run
    docker exec opentranscribe-backend python3 /app/scripts/retry_youtube_auth_errors_staggered.py --batch-size 10 --delay 600
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.media import FileStatus  # noqa: E402
from app.models.media import MediaFile  # noqa: E402
from app.utils.file_retry import reset_file_for_retry  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Retry YouTube auth errors with staggered delays")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--batch-size", type=int, default=20, help="Number of files per batch (default: 20)"
    )
    parser.add_argument(
        "--delay", type=int, default=300, help="Seconds between batches (default: 300 = 5 minutes)"
    )
    args = parser.parse_args()

    engine = create_engine(settings.DATABASE_URL)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()

    try:
        # Get all auth error YouTube files
        auth_errors = (
            db.query(MediaFile)
            .filter(
                MediaFile.status == FileStatus.ERROR,
                MediaFile.error_category == "auth_or_rate_limit",
                MediaFile.source_url.ilike("%youtube.com%"),
            )
            .order_by(MediaFile.id)
            .all()
        )

        total_files = len(auth_errors)
        if total_files == 0:
            print("No YouTube auth error files found to retry.")
            return

        num_batches = (total_files + args.batch_size - 1) // args.batch_size

        print(f"Found {total_files} YouTube auth errors to retry")
        print(f"Batch size: {args.batch_size}, Delay: {args.delay}s ({args.delay / 60:.1f} min)")
        print(f"Total batches: {num_batches}")
        print(f"Estimated total time: {num_batches * args.delay / 3600:.1f} hours")

        if args.dry_run:
            print("\n*** DRY RUN MODE - No changes will be made ***\n")

        reset_count = 0
        for batch_num in range(num_batches):
            start_idx = batch_num * args.batch_size
            end_idx = min(start_idx + args.batch_size, total_files)
            batch = auth_errors[start_idx:end_idx]

            print(f"\n=== Batch {batch_num + 1}/{num_batches} ({len(batch)} files) ===")

            for file in batch:
                try:
                    filename = str(file.filename)[:60] if file.filename else "unknown"

                    if args.dry_run:
                        print(f"  [DRY RUN] Would queue: {filename}")
                        continue

                    # Reclassify error category as network (retriable)
                    file.error_category = "network"
                    file.last_error_message = "YouTube rate limit during bulk import (retriable)"
                    db.commit()

                    # Reset file for retry (clears error state, sets PENDING)
                    if reset_file_for_retry(db, int(file.id), reset_retry_count=True):
                        # Dispatch YouTube download task
                        from app.tasks.youtube_processing import process_youtube_url_task

                        process_youtube_url_task.delay(
                            url=str(file.source_url),
                            user_id=int(file.user_id),
                            file_uuid=str(file.uuid),
                        )
                        reset_count += 1
                        print(f"  Queued: {filename}")
                    else:
                        print(f"  Failed to reset: {filename}")

                except Exception as e:
                    print(f"  ERROR: {file.id} - {e}")
                    db.rollback()

            # Delay before next batch (except after last batch)
            if batch_num < num_batches - 1 and not args.dry_run:
                print(f"\nWaiting {args.delay}s before next batch...")
                time.sleep(args.delay)

        print("\n=== Summary ===")
        if args.dry_run:
            print(f"Would queue {total_files} files for retry")
        else:
            print(f"Queued {reset_count}/{total_files} files for retry")

    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    main()
