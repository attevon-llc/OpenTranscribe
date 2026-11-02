"""Console summaries printed per batch and at the end of a run."""

from __future__ import annotations

from bench_parallel.backend_api import db_query
from bench_parallel.formatting import fmt_duration
from bench_parallel.models import BatchResult, FileResult


def print_batch_summary(result: BatchResult):
    """Print summary for a single batch."""
    completed = [fr for fr in result.file_results if fr.status == 'completed']
    errored = [fr for fr in result.file_results if fr.status == 'error']

    print(f'\n  {"─" * 70}')
    print(f'  BATCH {result.batch_size} SUMMARY')
    print(f'  {"─" * 70}')
    print(f'  Total wall time:  {fmt_duration(result.wall_elapsed)}')
    print(f'  Completed:        {len(completed)}/{result.batch_size}')
    if errored:
        print(f'  Errors:           {len(errored)}')
    print(f'  VRAM peak:        {result.vram_peak_mb} MB')
    print(f'  VRAM avg:         {result.vram_avg_mb:.0f} MB')
    if result.cpu_samples:
        print(f'  CPU peak/avg:     {result.cpu_pct_peak:.0f}% / {result.cpu_pct_avg:.0f}%')
        print(f'  RAM peak/avg:     {result.ram_mb_peak:.0f} / {result.ram_mb_avg:.0f} MB')

    if completed:
        wall_times = [fr.wall_elapsed for fr in completed]
        print('\n  Per-file wall time:')
        print(f'    {"File":<45} {"Wall":>10} {"GPU":>10} {"Audio":>8}')
        print(f'    {"─" * 45} {"─" * 10} {"─" * 10} {"─" * 8}')
        for fr in completed:
            gpu_dur = fr.stages.get('3_gpu_transcribe', 0)
            audio_hrs = fr.duration_s / 3600
            print(
                f'    {fr.filename[:45]:<45} '
                f'{fmt_duration(fr.wall_elapsed):>10} '
                f'{fmt_duration(gpu_dur):>10} '
                f'{audio_hrs:.1f}h'
            )

        avg_wall = sum(wall_times) / len(wall_times)
        print(f'\n    Avg wall time/file:   {fmt_duration(avg_wall)}')
        print(f'    Min:                  {fmt_duration(min(wall_times))}')
        print(f'    Max:                  {fmt_duration(max(wall_times))}')

        # GPU stage breakdown
        gpu_times = [fr.stages.get('3_gpu_transcribe', 0) for fr in completed if fr.stages]
        if gpu_times and any(gpu_times):
            print('\n  Pipeline stage averages:')
            stage_keys = [
                '1_preprocess',
                '2_cpu_to_gpu_queue',
                '3_gpu_transcribe',
                '4_gpu_to_post_queue',
            ]
            for key in stage_keys:
                vals = [fr.stages.get(key, 0) for fr in completed if fr.stages and key in fr.stages]
                if vals:
                    avg = sum(vals) / len(vals)
                    print(
                        f'    {key:<25} avg={fmt_duration(avg):>10}  '
                        f'min={fmt_duration(min(vals)):>10}  max={fmt_duration(max(vals)):>10}'
                    )

    # GPU queue contention analysis
    print_queue_analysis(completed)
    print()


def print_queue_analysis(completed: list[FileResult]):
    """Analyze GPU queue contention — how much time files spent waiting."""
    files_with_gpu = [
        fr for fr in completed if fr.stages.get('_gpu_recv') and fr.stages.get('_gpu_end')
    ]
    if not files_with_gpu:
        return

    # Sort by GPU received time
    files_with_gpu.sort(key=lambda f: f.stages['_gpu_recv'])

    print('\n  GPU scheduling timeline:')
    print(f'    {"File":<35} {"Queue Wait":>10} {"GPU Start":>12} {"GPU End":>12} {"GPU Dur":>10}')
    print(f'    {"─" * 35} {"─" * 10} {"─" * 12} {"─" * 12} {"─" * 10}')

    t0 = files_with_gpu[0].stages.get('_dispatch', files_with_gpu[0].stages['_gpu_recv'])
    for fr in files_with_gpu:
        queue_wait = fr.stages.get('2_cpu_to_gpu_queue', 0)
        gpu_start = fr.stages['_gpu_recv'] - t0
        gpu_end = fr.stages['_gpu_end'] - t0
        gpu_dur = gpu_end - gpu_start
        print(
            f'    {fr.filename[:35]:<35} '
            f'{fmt_duration(queue_wait):>10} '
            f'{fmt_duration(gpu_start):>12} '
            f'{fmt_duration(gpu_end):>12} '
            f'{fmt_duration(gpu_dur):>10}'
        )


def print_final_summary(
    all_batches: list[BatchResult],
    single_wall: float | None,
    corpus_total_audio_h: float | None = None,
    corpus_name: str | None = None,
):
    """Print the final scaling comparison."""
    print(f'\n{"=" * 100}')
    print('PARALLEL SCALING SUMMARY')
    if corpus_name:
        print(f'Corpus: {corpus_name}  ({corpus_total_audio_h:.2f}h total audio)')
    print(f'{"=" * 100}')
    print(
        f'{"Batch":>6} {"Wall Time":>12} {"Avg/File":>12} {"GPU Avg":>12} '
        f'{"VRAM Peak":>10} {"GPU Util%":>10} {"Throughput":>12} {"Speedup":>8}'
    )
    print(f'{"─" * 6} {"─" * 12} {"─" * 12} {"─" * 12} {"─" * 10} {"─" * 10} {"─" * 12} {"─" * 8}')

    for batch in all_batches:
        completed = [fr for fr in batch.file_results if fr.status == 'completed']
        if not completed:
            continue
        walls = [fr.wall_elapsed for fr in completed]
        gpus = [fr.stages.get('3_gpu_transcribe', 0) for fr in completed if fr.stages]
        total_audio_s = sum(fr.duration_s for fr in completed)
        throughput = (
            (total_audio_s / 3600) / (batch.wall_elapsed / 3600) if batch.wall_elapsed else 0
        )
        avg_util = (
            sum(s.utilization_pct for s in batch.vram_samples) / len(batch.vram_samples)
            if batch.vram_samples
            else 0
        )

        speedup = ''
        if single_wall:
            sp = (single_wall * len(completed)) / batch.wall_elapsed if batch.wall_elapsed else 0
            speedup = f'{sp:.2f}x'

        avg_gpu = fmt_duration(sum(gpus) / len(gpus)) if gpus else 'N/A'

        print(
            f'{batch.batch_size:>6} '
            f'{fmt_duration(batch.wall_elapsed):>12} '
            f'{fmt_duration(sum(walls) / len(walls)):>12} '
            f'{avg_gpu:>12} '
            f'{batch.vram_peak_mb:>8}MB '
            f'{avg_util:>9.0f}% '
            f'{throughput:>10.2f}x '
            f'{speedup:>8}'
        )

    print(f'{"=" * 100}')
    print('Throughput = audio hours processed per wall-clock hour')
    print('Speedup = ideal sequential time / actual batch time (linear=batch_size)')
    if corpus_total_audio_h:
        # Full-corpus aggregate RTF: the whitepaper headline metric
        for batch in all_batches:
            completed = [fr for fr in batch.file_results if fr.status == 'completed']
            if not completed or not batch.wall_elapsed:
                continue
            submitted_audio_h = sum(fr.duration_s for fr in batch.file_results) / 3600
            if abs(submitted_audio_h - corpus_total_audio_h) < 0.5:
                full_rtf = corpus_total_audio_h / (batch.wall_elapsed / 3600)
                print(
                    f'\n*** Full-corpus aggregate RTF (batch={batch.batch_size}): '
                    f'{full_rtf:.1f}x  '
                    f'({corpus_total_audio_h:.1f}h audio in {fmt_duration(batch.wall_elapsed)}) ***'
                )

    # Reprocessing projection based on measured throughput
    print_reprocess_projection(all_batches)


def print_reprocess_projection(all_batches: list[BatchResult]):
    """Query DB for total audio hours and project reprocessing time."""
    rows = db_query(
        'SELECT COUNT(*), COALESCE(SUM(duration), 0), '
        'COALESCE(AVG(duration), 0), COALESCE(MIN(duration), 0), '
        'COALESCE(MAX(duration), 0) '
        "FROM media_file WHERE status = 'completed' AND duration > 0"
    )
    if not rows:
        return

    total_files = int(rows[0][0])
    total_duration_s = float(rows[0][1])
    avg_duration_s = float(rows[0][2])
    total_hours = total_duration_s / 3600

    if total_files == 0:
        return

    # Get duration distribution
    dist_rows = db_query(
        'SELECT '
        '  CASE '
        "    WHEN duration < 300 THEN '< 5min' "
        "    WHEN duration < 1800 THEN '5-30min' "
        "    WHEN duration < 3600 THEN '30-60min' "
        "    WHEN duration < 7200 THEN '1-2hr' "
        "    WHEN duration < 10800 THEN '2-3hr' "
        "    ELSE '3hr+' "
        '  END AS bucket, '
        '  COUNT(*), SUM(duration) / 3600.0 '
        'FROM media_file '
        "WHERE status = 'completed' AND duration > 0 "
        'GROUP BY 1 ORDER BY MIN(duration)'
    )

    print(f'\n{"=" * 90}')
    print('REPROCESSING PROJECTION')
    print(f'{"=" * 90}')
    print(f'  Total completed files:     {total_files}')
    print(f'  Total audio duration:      {total_hours:.1f} hours ({total_hours / 24:.1f} days)')
    print(f'  Average file duration:     {fmt_duration(avg_duration_s)}')

    if dist_rows:
        print('\n  Duration Distribution:')
        print(f'    {"Bucket":<12} {"Files":>8} {"Hours":>10}')
        print(f'    {"─" * 12} {"─" * 8} {"─" * 10}')
        for row in dist_rows:
            bucket = row[0].strip()
            count = int(row[1].strip())
            hours = float(row[2].strip())
            print(f'    {bucket:<12} {count:>8} {hours:>9.1f}')

    # Use best measured throughput for projections
    best_throughput = 0
    best_batch = 0
    for batch in all_batches:
        completed = [fr for fr in batch.file_results if fr.status == 'completed']
        if not completed or batch.wall_elapsed == 0:
            continue
        total_audio_s = sum(fr.duration_s for fr in completed)
        throughput = (total_audio_s / 3600) / (batch.wall_elapsed / 3600)
        if throughput > best_throughput:
            best_throughput = throughput
            best_batch = batch.batch_size

    # Single-file throughput (batch=1)
    single_throughput = 0
    for batch in all_batches:
        if batch.batch_size == 1:
            completed = [fr for fr in batch.file_results if fr.status == 'completed']
            if completed and batch.wall_elapsed > 0:
                total_audio_s = sum(fr.duration_s for fr in completed)
                single_throughput = (total_audio_s / 3600) / (batch.wall_elapsed / 3600)
            break

    if single_throughput > 0 or best_throughput > 0:
        print('\n  Projected Reprocessing Times:')
        print(f'    {"Config":<35} {"Throughput":>12} {"Est. Time":>12}')
        print(f'    {"─" * 35} {"─" * 12} {"─" * 12}')

        if single_throughput > 0:
            est_hrs = total_hours / single_throughput
            print(
                f'    {"1 worker (measured)":35} {single_throughput:>10.1f}x {fmt_duration(est_hrs * 3600):>12}'
            )

        if best_throughput > 0 and best_batch > 1:
            est_hrs = total_hours / best_throughput
            print(
                f'    {f"{best_batch} workers (measured)":35} {best_throughput:>10.1f}x {fmt_duration(est_hrs * 3600):>12}'
            )

        # Extrapolate for higher worker counts (sub-linear scaling)
        if single_throughput > 0:
            for workers in [5, 9]:
                if workers <= best_batch:
                    continue
                # ~15% overhead per additional concurrent task
                eff = workers * (1 / (1 + 0.15 * (workers - 1)))
                projected = single_throughput * eff
                est_hrs = total_hours / projected
                print(
                    f'    {f"{workers} workers (projected)":35} {projected:>10.1f}x {fmt_duration(est_hrs * 3600):>12}'
                )

    print(f'{"=" * 90}')
