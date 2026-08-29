# mypy: disable-error-code="assignment"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
"""
Integration test for v3→v4 speaker embedding migration.

Runs against LIVE services (Redis, OpenSearch, MinIO, PostgreSQL) to verify
the full migration pipeline works end-to-end with real files from the database.

Usage:
    # From backend/ with venv activated and dev environment running:
    pytest tests/test_embedding_migration_integration.py -v -s

    # Run just the throughput benchmark (processes 10 files):
    pytest tests/test_embedding_migration_integration.py -v -s -k throughput

    # Run with custom sample size:
    MIGRATION_TEST_SAMPLE=20 pytest tests/test_embedding_migration_integration.py -v -s -k throughput

Requires:
    - Dev environment running (./opentr.sh start dev)
    - At least a few completed transcriptions in the database
    - GPU available for embedding extraction
"""

import logging
import os
import sys
import time
from pathlib import Path

import pytest

# Add backend dir to path
_backend_dir = Path(__file__).parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

# These tests require live services — skip if env vars disable them
pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_REDIS", "").lower() == "true"
    or os.environ.get("SKIP_OPENSEARCH", "").lower() == "true"
    or os.environ.get("SKIP_S3", "").lower() == "true"
    or os.environ.get("TESTING", "").lower() == "true",
    reason="Requires live Redis, OpenSearch, MinIO (run outside pytest conftest)",
)

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def live_db_session():
    """Database session connected to the live dev database."""
    from dotenv import dotenv_values

    project_root = Path(__file__).parent.parent.parent
    env_file = project_root / ".env"
    if env_file.exists():
        env_values = dotenv_values(env_file)
        for key in ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"]:
            if key in env_values:
                os.environ[key] = env_values[key]

    os.environ.setdefault("POSTGRES_HOST", "localhost")
    os.environ.setdefault("POSTGRES_PORT", "5176")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.config import settings

    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    yield session
    session.close()


@pytest.fixture(scope="module")
def completed_files(live_db_session):
    """Get completed media files from the live database."""
    from app.models.media import FileStatus
    from app.models.media import MediaFile

    files = (
        live_db_session.query(MediaFile)
        .filter(MediaFile.status == FileStatus.COMPLETED)
        .limit(50)
        .all()
    )
    return files


class TestMigrationLockLive:
    """Test migration lock against live Redis."""

    def test_activate_deactivate_cycle(self):
        from app.services.migration_lock_service import MigrationLockService

        lock = MigrationLockService()
        # Clean up any stale lock first
        lock.deactivate()

        assert lock.is_active() is False

        assert lock.activate() is True
        assert lock.is_active() is True

        # Second activate increments the ref count (INCR semantics)
        assert lock.activate() is True

        assert lock.deactivate() is True
        assert lock.is_active() is False

    def test_refresh_ttl(self):
        from app.services.migration_lock_service import MigrationLockService

        lock = MigrationLockService()
        lock.deactivate()  # Clean slate

        lock.activate()
        assert lock.refresh_ttl(ttl=60) is True
        lock.deactivate()

    def test_refresh_ttl_fails_without_lock(self):
        from app.services.migration_lock_service import MigrationLockService

        lock = MigrationLockService()
        lock.deactivate()

        assert lock.refresh_ttl() is False


class TestSkipLogicLive:
    """Test skip logic against live OpenSearch."""

    def test_get_already_migrated_file_ids(self):
        from app.tasks.embedding_migration_v4 import _get_already_migrated_file_ids

        # Should return a set (may be empty if no v4 index exists yet)
        result = _get_already_migrated_file_ids()
        assert isinstance(result, set)


class TestMigrationStatusLive:
    """Test migration status against live services."""

    def test_get_migration_status(self):
        from app.tasks.embedding_migration_v4 import get_migration_status

        status = get_migration_status()
        assert "current_mode" in status
        assert "v3_document_count" in status
        assert "v4_document_count" in status
        assert "migration_needed" in status
        assert "transcription_paused" in status
        print(f"\n  Current mode: {status['current_mode']}")
        print(f"  V3 docs: {status['v3_document_count']}")
        print(f"  V4 docs: {status['v4_document_count']}")
        print(f"  Migration needed: {status['migration_needed']}")
        print(f"  Transcription paused: {status['transcription_paused']}")


class TestPrepareFileForGpuLive:
    """Test I/O preparation with real files."""

    def test_prepare_single_file(self, completed_files):
        if not completed_files:
            pytest.skip("No completed files in database")

        from app.tasks.migration_pipeline import prepare_file

        file = completed_files[0]
        file_uuid = str(file.uuid)

        start = time.time()
        prepared = prepare_file(file_uuid)
        elapsed = time.time() - start

        if prepared is None:
            print(f"\n  File {file_uuid} has no speakers (skipped)")
        else:
            print(f"\n  Prepared file {file_uuid} in {elapsed:.2f}s")
            print(f"    Speakers: {len(prepared.speakers)}")
            print(f"    Segment groups: {len(prepared.speaker_segments)}")
            print(f"    Audio source: {prepared.audio_source[:80]}...")
            assert prepared.audio_source.startswith("http")


class TestThroughputBenchmark:
    """Benchmark migration throughput with real files.

    This is the key test for understanding migration speed.
    Processes a sample of files through the full pipeline and reports timing.
    """

    def test_throughput_benchmark(self, completed_files):
        """Process a sample of files and measure throughput.

        Set MIGRATION_TEST_SAMPLE env var to control sample size (default: 10).
        """
        if not completed_files:
            pytest.skip("No completed files in database")

        from app.core.constants import SPEAKER_SHORT_SEGMENT_MIN_DURATION
        from app.services.embedding_mode_service import MODE_V4
        from app.services.speaker_analysis_models import EmbeddingModelAdapter
        from app.services.speaker_analysis_models import MultiModelRunner
        from app.services.speaker_embedding_service import get_cached_embedding_service
        from app.tasks.embedding_migration_v4 import _embedding_result_writer
        from app.tasks.migration_pipeline import prepare_file
        from app.tasks.migration_pipeline import process_batch_pipelined

        sample_size = int(os.environ.get("MIGRATION_TEST_SAMPLE", "10"))
        sample = completed_files[:sample_size]

        print(f"\n{'=' * 70}")
        print("  V3->V4 Migration Throughput Benchmark")
        print(f"  Sample size: {len(sample)} files")
        print(f"{'=' * 70}")

        # Phase 1: Measure model load time
        print("\n  Phase 1: Loading embedding model (one-time cost)...")
        model_start = time.time()
        embedding_service = get_cached_embedding_service(mode=MODE_V4)
        model_time = time.time() - model_start
        print(f"    Model load: {model_time:.1f}s")

        # Phase 2: Prepare files and measure I/O
        io_times = []
        speaker_counts = []
        prepared_files = []
        skipped = 0

        for i, file in enumerate(sample):
            file_uuid = str(file.uuid)

            # I/O phase: download + DB query
            io_start = time.time()
            try:
                prepared = prepare_file(file_uuid)
            except Exception as e:
                print(f"    [{i + 1}/{len(sample)}] {file_uuid}: I/O FAILED - {e}")
                continue
            io_time = time.time() - io_start

            if prepared is None:
                skipped += 1
                print(f"    [{i + 1}/{len(sample)}] {file_uuid}: no speakers (skipped)")
                continue

            io_times.append(io_time)
            speaker_counts.append(len(prepared.speakers))
            prepared_files.append((file_uuid, prepared))
            print(
                f"    [{i + 1}/{len(sample)}] {file_uuid}: "
                f"{len(prepared.speakers)} speakers, I/O={io_time:.2f}s"
            )

        # Phase 3: Process all prepared files through the pipeline
        if not prepared_files:
            if skipped == len(sample):
                # Every sampled file legitimately has no speakers to benchmark — a real
                # (if unhelpful) environment state, not a broken one.
                pytest.skip(
                    f"None of the {len(sample)} sampled completed files have speakers to benchmark"
                )
            pytest.fail(
                f"No files were prepared successfully out of {len(sample)} sampled "
                f"({skipped} skipped for no speakers, {len(sample) - skipped} hit I/O "
                "errors — see the per-file log above for the exceptions)"
            )

        runner = MultiModelRunner([EmbeddingModelAdapter(embedding_service)])

        gpu_start = time.time()
        success_count, fail_count = process_batch_pipelined(
            prepared_files=prepared_files,
            runner=runner,
            result_writer=_embedding_result_writer,
            is_running_check=lambda: True,
            on_file_success=lambda _: None,
            on_file_failure=lambda _, __: None,
            min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION,
        )
        gpu_time = time.time() - gpu_start

        # Summary
        print(f"\n{'=' * 70}")
        print("  Results")
        print(f"{'=' * 70}")

        processed = len(prepared_files)
        avg_io = sum(io_times) / len(io_times)
        avg_gpu = gpu_time / processed
        avg_total = avg_io + avg_gpu
        avg_speakers = sum(speaker_counts) / len(speaker_counts)

        print(f"  Files processed: {processed} ({success_count} succeeded, {fail_count} failed)")
        print(f"  Files skipped (no speakers): {skipped}")
        print(f"  Avg speakers/file: {avg_speakers:.1f}")
        print()
        print(f"  Avg I/O time (download + DB): {avg_io:.2f}s/file")
        print(f"  Avg GPU time (extraction):    {avg_gpu:.2f}s/file")
        print(f"  Avg total per file:           {avg_total:.2f}s/file")
        print()

        # Pipeline estimate: with prefetch, GPU doesn't wait for I/O
        pipeline_per_file = max(avg_io, avg_gpu)  # Bottleneck determines rate
        print("  Pipeline throughput (with prefetch):")
        print(f"    Bottleneck: {'I/O' if avg_io > avg_gpu else 'GPU'} ({pipeline_per_file:.2f}s)")
        print(f"    ~{pipeline_per_file:.2f}s/file effective")
        print()

        # Projections
        for total_files in [100, 500, 1000, 2500]:
            sequential = total_files * avg_total
            pipelined = total_files * pipeline_per_file + model_time
            print(
                f"  {total_files:>5} files: "
                f"sequential={_fmt_duration(sequential)}, "
                f"pipelined={_fmt_duration(pipelined)}"
            )

        print(f"\n  Model load (one-time): {model_time:.1f}s")
        print(f"{'=' * 70}")

        # This printed a report and asserted nothing, so it could only fail by raising — a
        # benchmark wearing a test's clothes (issue #431). A throughput floor would be
        # machine-dependent and flaky, but these are not: the pipeline must actually have
        # processed the sample it was given, and must not have failed every file. That makes
        # it a genuine end-to-end smoke test of the v3->v4 migration on real data, while the
        # timings above stay purely informational.
        assert processed > 0, "the benchmark prepared no files, so it measured nothing"
        assert success_count + fail_count == processed, (
            f"pipeline accounting is inconsistent: {success_count} + {fail_count} != {processed}"
        )
        assert success_count > 0, (
            f"every file failed the migration pipeline ({fail_count} failures) — the numbers "
            "above describe a broken run, not a slow one"
        )
        assert avg_speakers > 0, "no speakers were extracted from any sampled file"


def _fmt_duration(seconds: float) -> str:
    """Format duration in human-readable form."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}min"
    else:
        return f"{seconds / 3600:.1f}hr"
