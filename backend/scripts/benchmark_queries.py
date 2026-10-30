#!/usr/bin/env python
"""
Database query performance benchmark script.

Measures key query patterns against real production data and outputs
timing metrics as JSON. Run before and after optimizations to compare.

Usage:
    cd /mnt/nvm/repos/transcribe-app/backend
    python -m scripts.benchmark_queries --user-id 1
    python -m scripts.benchmark_queries --user-id 1 --output optimized.json
"""

import argparse
import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy import func
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.tag import FileTag
from app.models.tag import Tag

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = settings.DATABASE_URL
engine = create_engine(DATABASE_URL, echo=False)
SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def timed(label: str, results: dict[str, Any]):
    """Context manager that records elapsed time in milliseconds."""
    start = time.perf_counter()
    yield
    elapsed_ms = (time.perf_counter() - start) * 1000
    results[label] = round(elapsed_ms, 2)
    logger.info(f"  {label}: {elapsed_ms:.2f} ms")


def benchmark_gallery_query(db: Session, user_id: int, results: dict):
    """Paginated gallery query — the most common query pattern."""
    logger.info("\n1. Gallery query (page 1, 20 items, ORDER BY upload_time DESC)")
    with timed("gallery_page1", results):
        rows = (
            db.query(MediaFile)
            .filter(MediaFile.user_id == user_id)
            .order_by(MediaFile.upload_time.desc())
            .offset(0)
            .limit(20)
            .all()
        )
    results["gallery_page1_rows"] = len(rows)


def benchmark_status_aggregation(db: Session, user_id: int, results: dict):
    """Status count aggregation — compare Python loop vs SQL GROUP BY."""
    logger.info("\n2. Status aggregation")

    # Method A: Load all files, count in Python (old approach)
    with timed("status_python_loop", results):
        all_files = db.query(MediaFile).filter(MediaFile.user_id == user_id).all()
        counts_py = {"total": len(all_files)}
        for f in all_files:
            status_val = f.status.value if hasattr(f.status, "value") else str(f.status)
            counts_py[status_val] = counts_py.get(status_val, 0) + 1
    results["status_python_files_loaded"] = counts_py.get("total", 0)

    # Method B: SQL GROUP BY (new approach)
    with timed("status_sql_group_by", results):
        rows = (
            db.query(MediaFile.status, func.count(MediaFile.id))
            .filter(MediaFile.user_id == user_id)
            .group_by(MediaFile.status)
            .all()
        )
        counts_sql = {}
        total = 0
        for status, count in rows:
            status_val = status.value if hasattr(status, "value") else str(status)
            counts_sql[status_val] = count
            total += count
        counts_sql["total"] = total

    if results["status_sql_group_by"] > 0:
        results["status_improvement_x"] = round(
            results["status_python_loop"] / results["status_sql_group_by"], 1
        )


def benchmark_speaker_filter(db: Session, user_id: int, results: dict):
    """Speaker filter — compare JOIN+DISTINCT vs EXISTS subquery."""
    # Get sample speakers
    speakers = (
        db.query(Speaker.display_name)
        .filter(Speaker.user_id == user_id, Speaker.display_name.isnot(None))
        .limit(3)
        .all()
    )
    if not speakers:
        logger.info("\n3. Speaker filter — SKIPPED (no speakers found)")
        results["speaker_filter_skipped"] = True
        return

    speaker_names = [s[0] for s in speakers if s[0]][:2]
    if not speaker_names:
        results["speaker_filter_skipped"] = True
        return

    logger.info(f"\n3. Speaker filter (speakers: {speaker_names})")

    # Method A: JOIN + JOIN + DISTINCT (old approach)
    import sqlalchemy as sa

    with timed("speaker_join_distinct", results):
        query = db.query(MediaFile).filter(MediaFile.user_id == user_id)
        conditions = [sa.or_(Speaker.display_name == s, Speaker.name == s) for s in speaker_names]
        query = (
            query.join(TranscriptSegment, TranscriptSegment.media_file_id == MediaFile.id)
            .join(Speaker, Speaker.id == TranscriptSegment.speaker_id)
            .filter(sa.or_(*conditions))
            .distinct()
        )
        rows_a = query.all()
    results["speaker_join_distinct_rows"] = len(rows_a)

    # Method B: EXISTS subquery (new approach)
    with timed("speaker_exists_subquery", results):
        query = db.query(MediaFile).filter(MediaFile.user_id == user_id)
        conditions = [sa.or_(Speaker.display_name == s, Speaker.name == s) for s in speaker_names]
        exists_subq = (
            sa.select(sa.literal(1))
            .select_from(Speaker)
            .where(Speaker.media_file_id == MediaFile.id)
            .where(sa.or_(*conditions))
            .exists()
        )
        query = query.filter(exists_subq)
        rows_b = query.all()
    results["speaker_exists_rows"] = len(rows_b)

    if results["speaker_exists_subquery"] > 0:
        results["speaker_improvement_x"] = round(
            results["speaker_join_distinct"] / results["speaker_exists_subquery"], 1
        )


def benchmark_tag_filter(db: Session, user_id: int, results: dict):
    """Tag filter — compare chained JOINs vs single subquery with HAVING."""
    tags = (
        db.query(Tag.name)
        .join(FileTag)
        .join(MediaFile)
        .filter(MediaFile.user_id == user_id)
        .limit(3)
        .all()
    )
    if not tags:
        logger.info("\n4. Tag filter — SKIPPED (no tags found)")
        results["tag_filter_skipped"] = True
        return

    tag_names = [t[0] for t in tags][:2]
    logger.info(f"\n4. Tag filter (tags: {tag_names})")

    import sqlalchemy as sa

    # Method A: Chained joins (old approach — uses first tag only for fair comparison)
    with timed("tag_chained_join", results):
        query = db.query(MediaFile).filter(MediaFile.user_id == user_id)
        query = (
            query.join(FileTag, FileTag.media_file_id == MediaFile.id)
            .join(Tag, Tag.id == FileTag.tag_id)
            .filter(Tag.name.in_(tag_names))
            .distinct()
        )
        rows_a = query.all()
    results["tag_chained_join_rows"] = len(rows_a)

    # Method B: Single subquery with HAVING COUNT (new approach)
    with timed("tag_having_count", results):
        query = db.query(MediaFile).filter(MediaFile.user_id == user_id)
        matching_ids = (
            sa.select(FileTag.media_file_id)
            .join(Tag, Tag.id == FileTag.tag_id)
            .where(Tag.name.in_(tag_names))
            .group_by(FileTag.media_file_id)
            .having(func.count(func.distinct(Tag.id)) == len(tag_names))
        )
        query = query.filter(MediaFile.id.in_(matching_ids))
        rows_b = query.all()
    results["tag_having_count_rows"] = len(rows_b)

    if results["tag_having_count"] > 0:
        results["tag_improvement_x"] = round(
            results["tag_chained_join"] / results["tag_having_count"], 1
        )


def benchmark_transcript_search(db: Session, user_id: int, results: dict):
    """Transcript search — compare ILIKE vs OpenSearch."""
    search_term = "the"  # Common word to ensure hits
    logger.info(f"\n5. Transcript search (term: '{search_term}')")

    # Method A: ILIKE full table scan (old approach)
    with timed("transcript_ilike", results):
        query = db.query(MediaFile).filter(MediaFile.user_id == user_id)
        query = (
            query.join(TranscriptSegment, TranscriptSegment.media_file_id == MediaFile.id)
            .filter(TranscriptSegment.text.ilike(f"%{search_term}%"))
            .distinct()
        )
        rows_a = query.all()
    results["transcript_ilike_rows"] = len(rows_a)

    # Note: OpenSearch benchmark would require a running OpenSearch instance
    # and is best tested via the API endpoint directly


def benchmark_index_usage(db: Session, results: dict):
    """Report index scan statistics from pg_stat_user_indexes."""
    logger.info("\n6. Index usage statistics (top 15)")
    rows = db.execute(
        text("""
            SELECT
                relname AS table_name,
                indexrelname AS index_name,
                idx_scan AS scans,
                idx_tup_read AS tuples_read,
                idx_tup_fetch AS tuples_fetched
            FROM pg_stat_user_indexes
            WHERE schemaname = 'public'
            ORDER BY idx_scan DESC
            LIMIT 15
        """)
    ).fetchall()

    index_stats = []
    for row in rows:
        entry = {
            "table": row[0],
            "index": row[1],
            "scans": row[2],
            "tuples_read": row[3],
            "tuples_fetched": row[4],
        }
        index_stats.append(entry)
        logger.info(f"  {entry['index']}: {entry['scans']} scans, {entry['tuples_read']} reads")

    results["index_stats"] = index_stats


def benchmark_table_sizes(db: Session, results: dict):
    """Report table sizes."""
    logger.info("\n7. Table sizes (top 10)")
    rows = db.execute(
        text("""
            SELECT
                relname AS table_name,
                pg_size_pretty(pg_total_relation_size(relid)) AS size,
                pg_total_relation_size(relid) AS bytes
            FROM pg_catalog.pg_statio_user_tables
            WHERE schemaname = 'public'
            ORDER BY pg_total_relation_size(relid) DESC
            LIMIT 10
        """)
    ).fetchall()

    table_sizes = []
    for row in rows:
        entry = {"table": row[0], "size": row[1], "bytes": row[2]}
        table_sizes.append(entry)
        logger.info(f"  {entry['table']}: {entry['size']}")

    results["table_sizes"] = table_sizes


def run_benchmarks(user_id: int, output_file: str | None = None):
    """Run all benchmarks and optionally save results to JSON."""
    db = SessionFactory()
    results: dict[str, Any] = {}

    try:
        # Metadata
        file_count = (
            db.query(func.count(MediaFile.id)).filter(MediaFile.user_id == user_id).scalar()
        )
        segment_count = (
            db.query(func.count(TranscriptSegment.id))
            .join(MediaFile)
            .filter(MediaFile.user_id == user_id)
            .scalar()
        )
        speaker_count = db.query(func.count(Speaker.id)).filter(Speaker.user_id == user_id).scalar()

        logger.info("=" * 70)
        logger.info("OpenTranscribe Database Query Performance Benchmark")
        logger.info("=" * 70)
        logger.info(f"  User ID:            {user_id}")
        logger.info(f"  Total files:        {file_count}")
        logger.info(f"  Transcript segments: {segment_count}")
        logger.info(f"  Speakers:           {speaker_count}")
        logger.info(f"  Database:           {settings.POSTGRES_DB}")
        logger.info("=" * 70)

        results["user_id"] = user_id
        results["total_files"] = file_count
        results["total_segments"] = segment_count
        results["total_speakers"] = speaker_count
        results["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")

        # Run each benchmark
        benchmark_gallery_query(db, user_id, results)
        benchmark_status_aggregation(db, user_id, results)
        benchmark_speaker_filter(db, user_id, results)
        benchmark_tag_filter(db, user_id, results)
        benchmark_transcript_search(db, user_id, results)
        benchmark_index_usage(db, results)
        benchmark_table_sizes(db, results)

        logger.info("\n" + "=" * 70)
        logger.info("Benchmark complete.")

        if output_file:
            out_path = Path(output_file)
            out_path.write_text(json.dumps(results, indent=2, default=str))
            logger.info(f"Results saved to {out_path}")

        logger.info("=" * 70)

    finally:
        db.close()

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark database query performance")
    parser.add_argument("--user-id", type=int, required=True, help="User ID to benchmark")
    parser.add_argument("--output", type=str, default=None, help="Output JSON file path")
    args = parser.parse_args()

    run_benchmarks(args.user_id, args.output)
