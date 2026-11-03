"""Give every media_file row that shares an original's object key its own copy.

Used when the v456 revision skipped ``uq_media_file_storage_path`` because several rows named one
``storage_path``. For each shared key the lowest-id row keeps it; every other row gets a
server-side copy under ``user_<uid>/file_<fid>/<filename>``, its ``storage_path`` is repointed
and its (derived, regenerable) ``playback_path`` is cleared. One commit per row, so an
interrupted run resumes. When nothing is left to split, the missing indexes are built.

Usage (inside a backend container)::

    python -m app.scripts.split_shared_storage_paths            # dry run: counts only
    python -m app.scripts.split_shared_storage_paths --apply    # copy, repoint, build indexes

Exit status: 0 clean / dry run, 3 some rows could not be split.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


def split_shared_storage_paths(*, apply: bool) -> dict[str, int]:
    """Split every shared ``storage_path`` group; return counts only."""
    from minio.commonconfig import CopySource

    from app.core.config import settings
    from app.db import object_key_indexes
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.services.minio_service import minio_client
    from app.utils.filename import get_safe_storage_filename

    report = {"groups": 0, "rows_to_split": 0, "split": 0, "failed": 0}
    with session_scope() as db:
        groups = object_key_indexes.duplicate_groups(
            db.connection(), object_key_indexes.STORAGE_PATH
        )
    report["groups"] = len(groups)
    report["rows_to_split"] = sum(len(ids) - 1 for _, ids in groups)
    if not apply:
        return report

    bucket = settings.MEDIA_BUCKET_NAME
    for key, ids in groups:
        for file_id in ids[1:]:
            try:
                with session_scope() as db:
                    row = (
                        db.query(MediaFile.filename, MediaFile.user_id)
                        .filter(MediaFile.id == file_id)
                        .one()
                    )
                name = str(row[0] or key.rsplit("/", 1)[-1])
                new_key = get_safe_storage_filename(name, int(row[1]), file_id)
                # The copy runs with no transaction open.
                minio_client.copy_object(bucket, new_key, CopySource(bucket, key))
                with session_scope() as db:
                    db.query(MediaFile).filter(MediaFile.id == file_id).update(
                        {"storage_path": new_key, "playback_path": None},
                        synchronize_session=False,
                    )
                    db.commit()
                report["split"] += 1
            except Exception:
                logger.error("Could not split media_file %s from %s", file_id, key, exc_info=True)
                report["failed"] += 1

    try:
        with session_scope() as db:
            object_key_indexes.ensure_indexes(db.connection())
            db.commit()
    except SQLAlchemyError:
        logger.error("Could not build the object-key indexes", exc_info=True)
        report["failed"] += 1
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="split_shared_storage_paths", description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Copy and repoint (default: dry run).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    report = split_shared_storage_paths(apply=args.apply)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 3 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
