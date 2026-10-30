#!/usr/bin/env python3
"""
Benchmark v3→v4 speaker embedding migration throughput.

Uses the actual migration functions (presigned URLs + segment-level
ffmpeg seeking + multi-model GPU pipeline) to measure real extraction speed.

Does NOT write to OpenSearch — measures extraction speed only.

Usage:
    # Run inside the celery-worker container:
    docker exec opentranscribe-celery-worker python -u /app/scripts/benchmark_migration.py
    docker exec opentranscribe-celery-worker python -u /app/scripts/benchmark_migration.py 30
    docker exec opentranscribe-celery-worker python -u /app/scripts/benchmark_migration.py --all
"""

import sys
import time

sys.path.insert(0, "/app")


def main():  # noqa: C901
    from sqlalchemy import create_engine
    from sqlalchemy import func
    from sqlalchemy.orm import sessionmaker

    from app.core.config import settings
    from app.core.constants import SPEAKER_SHORT_SEGMENT_MIN_DURATION
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.services.embedding_mode_service import MODE_V4
    from app.services.speaker_analysis_models import EmbeddingModelAdapter
    from app.services.speaker_analysis_models import MultiModelRunner
    from app.services.speaker_embedding_service import get_cached_embedding_service
    from app.tasks.embedding_migration_v4_writer import _embedding_result_writer
    from app.tasks.migration_pipeline import DEFAULT_IO_WORKERS
    from app.tasks.migration_pipeline import prepare_file
    from app.tasks.migration_pipeline import process_batch_pipelined

    # Parse args
    sample_size = 10
    if len(sys.argv) > 1:
        sample_size = 99999 if sys.argv[1] == "--all" else int(sys.argv[1])

    # Connect to DB
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()

    total_completed = (
        db.query(func.count(MediaFile.id)).filter(MediaFile.status == FileStatus.COMPLETED).scalar()
    )

    files = (
        db.query(MediaFile)
        .filter(MediaFile.status == FileStatus.COMPLETED)
        .limit(sample_size)
        .all()
    )

    print(f"\n{'=' * 70}", flush=True)
    print("  V3→V4 Speaker Embedding Migration Benchmark", flush=True)
    print(f"{'=' * 70}", flush=True)
    print(f"  Total completed files in DB: {total_completed}", flush=True)
    print(f"  Sample size: {len(files)}", flush=True)
    print(f"  I/O threads: {DEFAULT_IO_WORKERS}", flush=True)
    print(flush=True)

    if not files:
        print("  No completed files found. Exiting.", flush=True)
        db.close()
        return

    # Phase 1: Load primary model
    print("  Phase 1: Loading WeSpeaker v4 embedding model...", flush=True)
    model_start = time.time()
    embedding_service = get_cached_embedding_service(mode=MODE_V4)
    model_time = time.time() - model_start
    print(f"    Primary model loaded in {model_time:.1f}s", flush=True)
    print(flush=True)

    # Stub out OpenSearch writes for benchmark
    import app.tasks.embedding_migration_v4_writer as _mig_mod

    _mig_mod._bulk_write_v4_embeddings = lambda docs: len(docs)  # type: ignore[assignment]

    # Phase 2a: Sequential baseline (single model, per-file pool)
    print("  Phase 2a: Sequential baseline (single model, per-file)", flush=True)
    print(f"  {'─' * 66}", flush=True)

    prep_times = []
    gpu_times = []
    total_times = []
    speaker_counts = []
    skipped = 0
    failed = 0

    for i, file in enumerate(files):
        file_uuid = str(file.uuid)
        file_mb = file.file_size / 1024 / 1024

        prep_start = time.time()
        try:
            prepared = prepare_file(file_uuid)
        except Exception as e:
            print(
                f"  [{i + 1:>4}/{len(files)}] {file_uuid[:12]}... PREP FAILED: {e}",
                flush=True,
            )
            failed += 1
            continue
        prep_time = time.time() - prep_start

        if prepared is None:
            skipped += 1
            print(
                f"  [{i + 1:>4}/{len(files)}] {file_uuid[:12]}... "
                f"no speakers (skip, {prep_time:.2f}s)",
                flush=True,
            )
            continue

        prep_times.append(prep_time)
        n_speakers = len(prepared.speakers)
        speaker_counts.append(n_speakers)

        gpu_start = time.time()
        try:
            runner = MultiModelRunner([EmbeddingModelAdapter(embedding_service)])
            success, fail = process_batch_pipelined(
                prepared_files=[(file_uuid, prepared)],
                runner=runner,
                result_writer=_embedding_result_writer,
                is_running_check=lambda: True,
                on_file_success=lambda _: None,
                on_file_failure=lambda _, __: None,
                min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION,
            )
            gpu_time = time.time() - gpu_start
            gpu_times.append(gpu_time)
            total = prep_time + gpu_time
            total_times.append(total)

            print(
                f"  [{i + 1:>4}/{len(files)}] {file_uuid[:12]}... "
                f"{file_mb:6.0f}MB {n_speakers}spk "
                f"prep={prep_time:.2f}s GPU={gpu_time:.2f}s total={total:.2f}s",
                flush=True,
            )
        except Exception as e:
            print(
                f"  [{i + 1:>4}/{len(files)}] {file_uuid[:12]}... GPU FAILED: {e}",
                flush=True,
            )
            failed += 1

    db.close()

    seq_processed = len(gpu_times)
    print(flush=True)

    if seq_processed == 0:
        print("  No files were successfully processed.", flush=True)
        return

    avg_prep = sum(prep_times) / len(prep_times)
    avg_gpu = sum(gpu_times) / len(gpu_times)
    avg_total_seq = sum(total_times) / len(total_times)

    print(
        f"  Sequential: {avg_total_seq:.2f}s/file (prep={avg_prep:.2f}s + GPU={avg_gpu:.2f}s)",
        flush=True,
    )
    print(flush=True)

    # Phase 2b: Multi-model pipelined benchmark
    print(
        f"  Phase 2b: Pipelined ({DEFAULT_IO_WORKERS} I/O threads)",
        flush=True,
    )
    print(f"  {'─' * 66}", flush=True)

    # Re-connect to DB for fresh data
    engine2 = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    session_factory2 = sessionmaker(bind=engine2)
    db2 = session_factory2()
    files2 = (
        db2.query(MediaFile)
        .filter(MediaFile.status == FileStatus.COMPLETED)
        .limit(sample_size)
        .all()
    )

    # Prepare all files
    prepared_files = []
    pipe_skipped = 0
    pipe_prep_start = time.time()
    for file in files2:
        fuuid = str(file.uuid)
        try:
            prepared = prepare_file(fuuid)
            if prepared is not None:
                prepared_files.append((fuuid, prepared))
            else:
                pipe_skipped += 1
        except Exception as e:
            print(f"    Prep failed for {fuuid[:12]}...: {e}", flush=True)
    pipe_prep_time = time.time() - pipe_prep_start
    db2.close()

    print(
        f"    Prepared {len(prepared_files)} files in {pipe_prep_time:.2f}s "
        f"({pipe_skipped} skipped)",
        flush=True,
    )

    # Run pipelined extraction
    runner = MultiModelRunner([EmbeddingModelAdapter(embedding_service)])

    pipe_start = time.time()
    pipe_migrated, pipe_failed = process_batch_pipelined(
        prepared_files=prepared_files,
        runner=runner,
        result_writer=_embedding_result_writer,
        is_running_check=lambda: True,
        on_file_success=lambda _: None,
        on_file_failure=lambda _, __: None,
        min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION,
    )
    pipe_time = time.time() - pipe_start

    pipe_per_file = pipe_time / len(prepared_files) if prepared_files else 0
    total_pipe_per_file = (
        (pipe_prep_time + pipe_time) / len(prepared_files) if prepared_files else 0
    )

    print(f"    Pipelined: {pipe_migrated} migrated, {pipe_failed} failed", flush=True)
    print(f"    Wall clock: {pipe_time:.2f}s ({pipe_per_file:.2f}s/file)", flush=True)
    print(
        f"    Including prep: {pipe_prep_time + pipe_time:.2f}s ({total_pipe_per_file:.2f}s/file)",
        flush=True,
    )
    print(flush=True)

    # Comparison
    speedup = avg_total_seq / total_pipe_per_file if total_pipe_per_file > 0 else 0

    print(f"{'=' * 70}", flush=True)
    print("  Results", flush=True)
    print(f"{'=' * 70}", flush=True)
    print(f"  Files processed: {seq_processed}", flush=True)
    print(f"  Files skipped:   {skipped}", flush=True)
    print(flush=True)
    print("  Sequential (1 model, per-file pool):", flush=True)
    print(f"    {avg_total_seq:.2f}s/file", flush=True)
    print(flush=True)
    print(f"  Pipelined ({DEFAULT_IO_WORKERS} I/O threads):", flush=True)
    print(f"    {total_pipe_per_file:.2f}s/file", flush=True)
    print(flush=True)
    print(f"  Speedup: {speedup:.1f}x", flush=True)
    print(flush=True)

    # Projections using pipelined rate
    print(f"  Projections (model load: {model_time:.0f}s one-time):", flush=True)
    print(f"  {'Files':>8}  {'Sequential':>12}  {'Pipelined':>12}", flush=True)
    print(f"  {'─' * 36}", flush=True)
    for n in [100, 500, 1000, total_completed]:
        seq = n * avg_total_seq + model_time
        pipe = n * total_pipe_per_file + model_time
        label = f"{n}*" if n == total_completed else str(n)
        print(f"  {label:>8}  {_fmt(seq):>12}  {_fmt(pipe):>12}", flush=True)

    if total_completed not in [100, 500, 1000]:
        print("  (* = your actual file count)", flush=True)

    print(f"\n{'=' * 70}", flush=True)


def _fmt(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"
    else:
        h, rem = divmod(int(seconds), 3600)
        m = rem // 60
        return f"{h}h {m}m"


if __name__ == "__main__":
    main()
