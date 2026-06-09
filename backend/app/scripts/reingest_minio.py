"""In-place MinIO re-ingestion CLI (Feature B).

Re-registers surviving MinIO media objects (``media/<user_id>/<uuid>.<ext>``)
as ``MediaFile`` rows WITHOUT copying or moving a single byte, then dispatches
the standard processing pipeline per file. Safe to re-run — objects already
referenced by a row are skipped.

Usage:
    python -m app.scripts.reingest_minio [--dry-run] [--limit N] \
        [--user-email admin@example.com] [--no-dispatch] [--throttle N]

Flags:
    --dry-run        Count what would be registered; create no rows, dispatch nothing.
    --limit N        Register at most N new files.
    --user-email     Owner for recovered files (default: admin@example.com).
    --no-dispatch    Register + fingerprint only; do not fire the pipeline.
    --throttle N     Sleep N seconds between dispatches. Default 0 (dispatch
                     all immediately — the single GPU worker serializes the
                     transcription queue anyway, so a throttle is only useful to
                     pace the CPU-side thumbnail/preprocess fan-out).

Run inside the backend container (``./opentr.sh shell backend``) so it reaches
the configured MinIO + Postgres.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from app.db.session_utils import session_scope
from app.services import storage_recovery_service as recovery

logger = logging.getLogger("reingest_minio")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="reingest_minio",
        description="Re-register surviving MinIO media objects as MediaFile rows in place.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be registered; create no rows and dispatch nothing.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Register at most N new files.",
    )
    parser.add_argument(
        "--user-email",
        default="admin@example.com",
        help="Owner email for recovered files (default: admin@example.com).",
    )
    parser.add_argument(
        "--no-dispatch",
        action="store_true",
        help="Register + fingerprint only; do not dispatch the processing pipeline.",
    )
    parser.add_argument(
        "--throttle",
        type=float,
        default=0.0,
        help="Seconds to sleep between dispatches (default 0 = dispatch all).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    args = _parse_args(argv)

    from app.services.minio_service import minio_client

    with session_scope() as db:
        try:
            user = recovery.resolve_user(db, args.user_email)
        except ValueError as exc:
            logger.error("%s", exc)
            return 2

        logger.info(
            "Re-ingesting media for owner=%s (id=%s) dry_run=%s limit=%s dispatch=%s throttle=%ss",
            user.email,
            user.id,
            args.dry_run,
            args.limit,
            not args.no_dispatch,
            args.throttle,
        )

        # The throttle is applied by sleeping between dispatches; the service
        # dispatches inline per file, so we thread the sleep via a wrapper when
        # a positive throttle is requested.
        if args.throttle and not args.dry_run and not args.no_dispatch:
            summary = _reingest_with_throttle(db, minio_client, user, args)
        else:
            summary = recovery.reingest_objects(
                db,
                minio_client=minio_client,
                user=user,
                dry_run=args.dry_run,
                limit=args.limit,
                dispatch=not args.no_dispatch,
            )

    payload = summary.as_dict()
    # Summary to stdout (machine-readable) in addition to the per-file logs above.
    print(json.dumps(payload, indent=2))  # noqa: T201 - intentional CLI output
    logger.info("Done: %s", payload)
    return 0


def _reingest_with_throttle(db, minio_client, user, args):  # type: ignore[no-untyped-def]
    """Register+dispatch one file at a time, sleeping ``--throttle`` between dispatches.

    Reuses the service's per-file primitives so dispatch pacing lives in the CLI
    without duplicating the discovery/registration logic.
    """
    summary = recovery.ReingestSummary()
    seen = recovery.existing_storage_paths(db)

    for object_name, size in recovery.iter_media_objects(minio_client):
        summary.discovered += 1
        if object_name in seen:
            summary.skipped_existing += 1
            continue
        if args.limit is not None and summary.registered >= args.limit:
            break
        try:
            media_file = recovery.register_object(db, object_name=object_name, size=size, user=user)
            recovery.fingerprint_object(media_file)
            db.commit()
            db.refresh(media_file)
            seen.add(object_name)
            summary.registered += 1
        except Exception as exc:
            db.rollback()
            summary.errors += 1
            logger.error("register failed for %s: %s", object_name, exc)
            continue

        try:
            from app.api.endpoints.files.upload import dispatch_upload_pipeline

            dispatch_upload_pipeline(
                media_file,
                user_id=user.id,
                whisper_model=None,
                min_speakers=None,
                max_speakers=None,
                num_speakers=None,
                task_id=None,
            )
            summary.dispatched += 1
        except Exception as exc:
            summary.errors += 1
            logger.error("dispatch failed for media_file id=%s: %s", media_file.id, exc)

        time.sleep(args.throttle)

    return summary


if __name__ == "__main__":
    raise SystemExit(main())
