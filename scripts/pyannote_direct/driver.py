"""Run a variant across the test files and write the report."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyannote_direct.aggregate import aggregate_runs
from pyannote_direct.config import (
    AUDIO_DIR,
    DEFAULT_MODEL,
    DEFAULT_OPTIMIZED_SRC,
    RESULTS_DIR,
)
from pyannote_direct.pipeline import load_pipeline, resolve_device_str
from pyannote_direct.runs import run_diarization, run_diarization_with_profiler
from pyannote_direct.summary import print_summary
from pyannote_direct.vram import MPSVRAMPoller, get_gpu_info

logger = logging.getLogger(__name__)


def run_benchmark(
    variant: str,
    file_labels: list[str],
    device: str = 'cuda',
    gpu_index: int = 0,
    onnx_cpu: bool = False,
    runs: int = 1,
    tag: str | None = None,
    rttm_out: Path | None = None,
    profiler: bool = False,
    model_id: str = DEFAULT_MODEL,
    optimized_src: Path | None = None,
    torch_compile: bool = False,
    segmentation_step: float | None = None,
) -> dict:
    """Run benchmark suite. If `runs > 1`, per-file statistical aggregates emitted."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    gpu_info = get_gpu_info()
    if gpu_info:
        print(
            f'GPU: {gpu_info["gpu_name"]} | '
            f'VRAM: {gpu_info["vram_used_mb"]}/{gpu_info["vram_total_mb"]} MB'
        )

    device_str = resolve_device_str(device, gpu_index)

    # MPS poller runs for the lifetime of the benchmark (lightweight at 100ms).
    mps_poller: MPSVRAMPoller | None = None
    if device_str == 'mps':
        mps_poller = MPSVRAMPoller(interval_s=0.1)
        mps_poller.start()

    try:
        pipeline, load_time = load_pipeline(
            variant,
            device,
            gpu_index,
            onnx_cpu=onnx_cpu,
            model_id=model_id,
            optimized_src=optimized_src,
            torch_compile=torch_compile,
            segmentation_step=segmentation_step,
        )

        per_file_results: list[dict] = []
        for label in file_labels:
            audio_path = AUDIO_DIR / f'{label}.wav'
            if not audio_path.exists():
                print(f'  SKIP {label} — {audio_path} not found')
                continue

            run_results: list[dict] = []
            for run_idx in range(runs):
                try:
                    rttm_out_path: Path | None = None
                    if rttm_out is not None:
                        rttm_out_path = rttm_out / f'{label}_run{run_idx}.rttm'

                    if profiler and run_idx == 0:
                        trace_out = RESULTS_DIR / 'traces' / f'{tag or variant}_{label}_run0.json'
                        r = run_diarization_with_profiler(
                            pipeline,
                            str(audio_path),
                            label,
                            device_str,
                            trace_out_path=trace_out,
                            rttm_out_path=rttm_out_path,
                            mps_poller=mps_poller,
                        )
                    else:
                        r = run_diarization(
                            pipeline,
                            str(audio_path),
                            label,
                            device_str,
                            rttm_out_path=rttm_out_path,
                            run_idx=run_idx,
                            mps_poller=mps_poller,
                        )
                    run_results.append(r)
                except Exception as e:  # per-run isolation boundary: recorded in the run's error
                    logger.exception('[%s run=%s] failed', label, run_idx)
                    print(f'  [{label} run={run_idx}] ERROR: {e}')
                    run_results.append(
                        {
                            'label': label,
                            'run_idx': run_idx,
                            'error': str(e),
                            'wall_seconds': 0,
                        }
                    )

            # Per-file summary with statistical aggregation
            successful = [r for r in run_results if 'error' not in r]
            per_file_entry: dict[str, Any] = {
                'label': label,
                'runs': run_results,
                'errors': len(run_results) - len(successful),
            }
            if successful:
                per_file_entry['aggregate'] = aggregate_runs(successful)
                # Legacy-compatible top-level fields (first successful run)
                first = successful[0]
                per_file_entry['total_seconds'] = first['wall_seconds']
                per_file_entry['speaker_count'] = first['speaker_count']
                per_file_entry['segment_count'] = first['segment_count']
                per_file_entry['stage_durations'] = first['stage_durations']
                per_file_entry['vram_before_mb'] = first.get('vram_before_nvml_mb')
                per_file_entry['vram_after_mb'] = first.get('vram_after_nvml_mb')
                per_file_entry['total_speech_seconds'] = first.get('total_speech_seconds')
                per_file_entry['all_segments'] = first.get('all_segments', [])
            per_file_results.append(per_file_entry)

    finally:
        if mps_poller is not None:
            mps_poller.stop()

    # Build full report
    timestamp = datetime.now(UTC).strftime('%Y%m%d_%H%M%S')
    import torch

    report: dict[str, Any] = {
        'variant': variant,
        'device': device,
        'device_str': device_str,
        'model_id': model_id,
        'tag': tag,
        'runs': runs,
        'timestamp': timestamp,
        'gpu_info': gpu_info,
        'pipeline_load_time_s': round(load_time, 2),
        'files': per_file_results,
        'torch_version': torch.__version__,
        'cuda_version': torch.version.cuda if torch.cuda.is_available() else None,
        'summary': {
            'total_files': len(per_file_results),
            'total_time_s': round(sum(f.get('total_seconds', 0) for f in per_file_results), 2),
            'errors': sum(f.get('errors', 0) for f in per_file_results),
        },
    }

    # Fork git SHA (best-effort)
    try:
        src_for_sha = Path(os.environ.get('PYANNOTE_OPTIMIZED_SRC') or DEFAULT_OPTIMIZED_SRC)
        if src_for_sha.exists():
            repo_root = src_for_sha
            while repo_root != repo_root.parent and not (repo_root / '.git').exists():
                repo_root = repo_root.parent
            if (repo_root / '.git').exists():
                sha = subprocess.run(
                    ['git', '-C', str(repo_root), 'rev-parse', 'HEAD'],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                report['pyannote_fork_sha'] = sha
                report['pyannote_fork_path'] = str(repo_root)
    except (subprocess.SubprocessError, OSError):
        logger.warning('could not read the pyannote fork git SHA', exc_info=True)

    # Tagged file + legacy "latest" for the --compare flow
    stem = f'benchmark_{variant}_{tag}_{timestamp}' if tag else f'benchmark_{variant}_{timestamp}'
    out_file = RESULTS_DIR / f'{stem}.json'
    with open(out_file, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(f'\nResults saved to: {out_file}')

    latest_file = RESULTS_DIR / f'benchmark_{variant}_latest.json'
    with open(latest_file, 'w') as f:
        json.dump(report, f, indent=2, default=str)

    print_summary(report)
    return report
