"""Write the CSV/JSON/Markdown report files for a finished benchmark."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from bench_parallel.console_reports import print_final_summary
from bench_parallel.formatting import fmt_duration
from bench_parallel.models import BatchResult, percentile


def write_reports(
    all_batches: list[BatchResult],
    output_dir: Path,
    corpus_total_audio_h: float | None = None,
    corpus_name: str | None = None,
    run_label: str = '',
):
    """Write CSV reports, machine-readable metrics.json, and final summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    # 1. Per-file details CSV
    csv_path = output_dir / f'benchmark_files_{timestamp}.csv'
    with open(csv_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                'batch_size',
                'file_uuid',
                'filename',
                'audio_duration_s',
                'audio_hours',
                'wall_elapsed_s',
                'status',
                'task_id',
                'preprocess_s',
                'cpu_to_gpu_queue_s',
                'gpu_transcribe_s',
                'gpu_to_post_queue_s',
                'total_to_gpu_end_s',
                'total_to_postprocess_s',
            ]
        )
        for batch in all_batches:
            for fr in batch.file_results:
                writer.writerow(
                    [
                        batch.batch_size,
                        fr.uuid,
                        fr.filename,
                        f'{fr.duration_s:.0f}',
                        f'{fr.duration_s / 3600:.2f}',
                        f'{fr.wall_elapsed:.1f}',
                        fr.status,
                        fr.task_id,
                        fr.stages.get('1_preprocess', ''),
                        fr.stages.get('2_cpu_to_gpu_queue', ''),
                        fr.stages.get('3_gpu_transcribe', ''),
                        fr.stages.get('4_gpu_to_post_queue', ''),
                        fr.stages.get('total_to_gpu_end', ''),
                        fr.stages.get('total_to_postprocess', ''),
                    ]
                )
    print(f'\nPer-file CSV: {csv_path}')

    # 2. Batch summary CSV (+ per-stage p50/p95, CPU/RAM)
    summary_path = output_dir / f'benchmark_summary_{timestamp}.csv'
    summary_rows: list[dict] = []
    with open(summary_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                'batch_size',
                'batch_wall_s',
                'batch_wall_fmt',
                'files_completed',
                'files_errored',
                'avg_file_wall_s',
                'min_file_wall_s',
                'max_file_wall_s',
                'avg_gpu_s',
                'min_gpu_s',
                'max_gpu_s',
                'preprocess_p50_s',
                'preprocess_p95_s',
                'cpu_to_gpu_p50_s',
                'cpu_to_gpu_p95_s',
                'gpu_transcribe_p50_s',
                'gpu_transcribe_p95_s',
                'gpu_to_post_p50_s',
                'gpu_to_post_p95_s',
                'vram_peak_mb',
                'vram_avg_mb',
                'gpu_util_pct_avg',
                'cpu_pct_peak',
                'cpu_pct_avg',
                'ram_mb_peak',
                'ram_mb_avg',
                'throughput_audio_hrs_per_wall_hr',
                'speedup_vs_single',
            ]
        )
        single_batch_wall = None
        stage_keys = [
            '1_preprocess',
            '2_cpu_to_gpu_queue',
            '3_gpu_transcribe',
            '4_gpu_to_post_queue',
        ]
        for batch in all_batches:
            completed = [fr for fr in batch.file_results if fr.status == 'completed']
            if not completed:
                continue
            walls = [fr.wall_elapsed for fr in completed]
            gpus = [fr.stages.get('3_gpu_transcribe', 0) for fr in completed if fr.stages]
            # Aggregate total-throughput RTF: ALL audio processed divided by the
            # batch wall-clock window (first dispatch -> last completion). This is
            # the steady-state system throughput, NOT a per-file end-to-end ratio.
            total_audio_s = sum(fr.duration_s for fr in completed)
            throughput = (
                (total_audio_s / 3600) / (batch.wall_elapsed / 3600) if batch.wall_elapsed else 0
            )

            if single_batch_wall is None:
                single_batch_wall = batch.wall_elapsed
            speedup = (
                (single_batch_wall * len(completed)) / batch.wall_elapsed
                if batch.wall_elapsed
                else 0
            )
            avg_util = (
                sum(s.utilization_pct for s in batch.vram_samples) / len(batch.vram_samples)
                if batch.vram_samples
                else 0
            )
            # Per-stage percentiles across completed files.
            pct: dict[str, float] = {}
            for key in stage_keys:
                vals = [fr.stages[key] for fr in completed if fr.stages and key in fr.stages]
                pct[f'{key}_p50'] = percentile(vals, 50)
                pct[f'{key}_p95'] = percentile(vals, 95)

            row = {
                'batch_size': batch.batch_size,
                'batch_wall_s': round(batch.wall_elapsed, 1),
                'files_completed': len(completed),
                'files_errored': len([fr for fr in batch.file_results if fr.status == 'error']),
                'avg_file_wall_s': round(sum(walls) / len(walls), 1),
                'min_file_wall_s': round(min(walls), 1),
                'max_file_wall_s': round(max(walls), 1),
                'avg_gpu_s': round(sum(gpus) / len(gpus), 1) if gpus else None,
                'preprocess_p50_s': round(pct['1_preprocess_p50'], 1),
                'preprocess_p95_s': round(pct['1_preprocess_p95'], 1),
                'gpu_transcribe_p50_s': round(pct['3_gpu_transcribe_p50'], 1),
                'gpu_transcribe_p95_s': round(pct['3_gpu_transcribe_p95'], 1),
                'vram_peak_mb': batch.vram_peak_mb,
                'vram_avg_mb': round(batch.vram_avg_mb, 0),
                'gpu_util_pct_avg': round(avg_util, 0),
                'cpu_pct_peak': round(batch.cpu_pct_peak, 1),
                'cpu_pct_avg': round(batch.cpu_pct_avg, 1),
                'ram_mb_peak': round(batch.ram_mb_peak, 0),
                'ram_mb_avg': round(batch.ram_mb_avg, 0),
                'throughput_audio_hrs_per_wall_hr': round(throughput, 2),
                'speedup_vs_single': round(speedup, 2),
            }
            summary_rows.append(row)

            writer.writerow(
                [
                    batch.batch_size,
                    f'{batch.wall_elapsed:.1f}',
                    fmt_duration(batch.wall_elapsed),
                    len(completed),
                    len([fr for fr in batch.file_results if fr.status == 'error']),
                    f'{sum(walls) / len(walls):.1f}',
                    f'{min(walls):.1f}',
                    f'{max(walls):.1f}',
                    f'{sum(gpus) / len(gpus):.1f}' if gpus else '',
                    f'{min(gpus):.1f}' if gpus else '',
                    f'{max(gpus):.1f}' if gpus else '',
                    f'{pct["1_preprocess_p50"]:.1f}',
                    f'{pct["1_preprocess_p95"]:.1f}',
                    f'{pct["2_cpu_to_gpu_queue_p50"]:.1f}',
                    f'{pct["2_cpu_to_gpu_queue_p95"]:.1f}',
                    f'{pct["3_gpu_transcribe_p50"]:.1f}',
                    f'{pct["3_gpu_transcribe_p95"]:.1f}',
                    f'{pct["4_gpu_to_post_queue_p50"]:.1f}',
                    f'{pct["4_gpu_to_post_queue_p95"]:.1f}',
                    batch.vram_peak_mb,
                    f'{batch.vram_avg_mb:.0f}',
                    f'{avg_util:.0f}',
                    f'{batch.cpu_pct_peak:.1f}',
                    f'{batch.cpu_pct_avg:.1f}',
                    f'{batch.ram_mb_peak:.0f}',
                    f'{batch.ram_mb_avg:.0f}',
                    f'{throughput:.2f}',
                    f'{speedup:.2f}',
                ]
            )
    print(f'Summary CSV:  {summary_path}')

    # 3. VRAM timeline CSV
    vram_path = output_dir / f'benchmark_vram_{timestamp}.csv'
    with open(vram_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(
            ['batch_size', 'elapsed_s', 'used_mb', 'total_mb', 'free_mb', 'util_pct', 'temp_c']
        )
        for batch in all_batches:
            if batch.vram_samples:
                t0 = batch.vram_samples[0].timestamp
                for s in batch.vram_samples:
                    writer.writerow(
                        [
                            batch.batch_size,
                            f'{s.timestamp - t0:.1f}',
                            s.used_mb,
                            s.total_mb,
                            s.free_mb,
                            s.utilization_pct,
                            s.temp_c,
                        ]
                    )
    print(f'VRAM CSV:     {vram_path}')

    # 4. CPU/RAM timeline CSV (worker container compute cost)
    cpu_path = output_dir / f'benchmark_cpu_{timestamp}.csv'
    with open(cpu_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['batch_size', 'elapsed_s', 'cpu_pct', 'mem_mb'])
        for batch in all_batches:
            if batch.cpu_samples:
                t0 = batch.cpu_samples[0].timestamp
                for cs in batch.cpu_samples:
                    writer.writerow(
                        [
                            batch.batch_size,
                            f'{cs.timestamp - t0:.1f}',
                            f'{cs.cpu_pct:.1f}',
                            f'{cs.mem_mb:.0f}',
                        ]
                    )
    print(f'CPU/RAM CSV:  {cpu_path}')

    # 5. Machine-readable metrics.json — the collator's single source of truth.
    metrics = {
        'run_label': run_label,
        'timestamp': timestamp,
        'corpus_name': corpus_name,
        'corpus_total_audio_h': corpus_total_audio_h,
        'batches': summary_rows,
    }
    metrics_path = output_dir / 'metrics.json'
    with open(metrics_path, 'w') as f:
        json.dump(metrics, f, indent=2)
    print(f'Metrics JSON: {metrics_path}')

    # 6. Final comparison table
    print_final_summary(
        all_batches,
        single_batch_wall,
        corpus_total_audio_h=corpus_total_audio_h,
        corpus_name=corpus_name,
    )
