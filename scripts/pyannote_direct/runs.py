"""Execute one diarization run, optionally under the profiler."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Any

from pyannote_direct.audio_io import annotation_to_rttm, load_waveform
from pyannote_direct.vram import (
    MPSVRAMPoller,
    device_peak_vram_mb,
    device_steady_state_vram_mb,
    get_vram_used_mb,
    reset_device_peak_vram,
    sync_device,
)


def run_diarization(
    pipeline,
    audio_path: str,
    label: str,
    device_str: str,
    rttm_out_path: Path | None = None,
    run_idx: int = 0,
    mps_poller: MPSVRAMPoller | None = None,
) -> dict:
    """Run diarization on a single file with timing + per-stage VRAM hooks."""
    import torch

    stage_timing: dict[str, dict[str, float]] = {}
    stage_order: list[str] = []

    def timing_hook(step_name, step_artefact, **kwargs):  # noqa: ARG001 (pyannote hook signature)
        # Sync so the timestamp reflects actual kernel completion, not enqueue.
        sync_device(device_str)
        now = time.perf_counter()
        # Capture cumulative peak at this stage boundary WITHOUT resetting —
        # peak monotonically grows so the overall peak equals the final capture.
        cum_peak = device_peak_vram_mb(device_str, mps_poller)
        if stage_order:
            prev_stage = stage_order[-1]
            stage_timing[prev_stage]['cumulative_peak_vram_mb'] = cum_peak
        if step_name not in stage_timing:
            stage_timing[step_name] = {'start': now, 'cumulative_peak_vram_mb': 0}
            stage_order.append(step_name)
        stage_timing[step_name]['last'] = now

    vram_before_nvml = get_vram_used_mb()
    vram_before_alloc = device_steady_state_vram_mb(device_str, mps_poller)

    waveform, sample_rate = load_waveform(audio_path)
    audio_input = {'waveform': waveform, 'sample_rate': sample_rate}
    duration_s = waveform.shape[1] / sample_rate
    print(f'  [{label} run={run_idx}] Diarizing ({duration_s:.0f}s, sr={sample_rate})...')

    # Reset peak before the pipeline call so the peak reflects only this run.
    reset_device_peak_vram(device_str, mps_poller)
    baseline_alloc_mb = device_steady_state_vram_mb(device_str, mps_poller)

    t0 = time.perf_counter()
    diarization = pipeline(audio_input, hook=timing_hook)
    sync_device(device_str)
    elapsed = time.perf_counter() - t0

    # Capture final cumulative peak (hook won't fire again after pipeline returns).
    final_peak = device_peak_vram_mb(device_str, mps_poller)
    if stage_order:
        last_stage = stage_order[-1]
        stage_timing[last_stage]['cumulative_peak_vram_mb'] = final_peak

    # Overall peak is monotonic — equals the final capture.
    overall_peak_mb = final_peak
    # Over-baseline delta (subtract resident allocation from before the run).
    overall_peak_over_baseline_mb = max(0, overall_peak_mb - baseline_alloc_mb)

    # Steady-state VRAM +2 seconds after pipeline returns
    time.sleep(2.0)
    steady_state_mb = device_steady_state_vram_mb(device_str, mps_poller)

    vram_after_nvml = get_vram_used_mb()

    # Extract results (v3 Annotation, v4 DiarizeOutput)
    annotation = getattr(diarization, 'speaker_diarization', diarization)
    speakers: set[str] = set()
    segments: list[dict[str, Any]] = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        speakers.add(speaker)
        segments.append(
            {
                'start': round(turn.start, 3),
                'end': round(turn.end, 3),
                'speaker': speaker,
            }
        )

    # RTTM output (for DER scoring downstream)
    rttm_path_str: str | None = None
    if rttm_out_path is not None:
        rttm_out_path.parent.mkdir(parents=True, exist_ok=True)
        rttm_text = annotation_to_rttm(annotation, uri=label)
        rttm_out_path.write_text(rttm_text)
        rttm_path_str = str(rttm_out_path)

    # Compute stage durations and per-stage VRAM deltas.
    # `cumulative_peak_vram_mb` is monotonic — the stage's incremental peak is
    # the delta over the previous stage's cumulative peak.
    stage_durations: dict[str, float] = {}
    cumulative_peaks: dict[str, int] = {}
    stage_vram_delta: dict[str, int] = {}
    stages = sorted(stage_timing.items(), key=lambda x: x[1].get('start', 0))
    prev_cum = baseline_alloc_mb
    for i, (name, times) in enumerate(stages):
        if i + 1 < len(stages):
            next_start = stages[i + 1][1]['start']
            dur = next_start - times['start']
        else:
            dur = (t0 + elapsed) - times['start']
        stage_durations[name] = round(dur, 3)
        cum = int(times.get('cumulative_peak_vram_mb', 0))
        cumulative_peaks[name] = cum
        stage_vram_delta[name] = max(0, cum - prev_cum)
        prev_cum = max(prev_cum, cum)  # monotonic

    # Total speech duration
    total_speech_s = sum(s['end'] - s['start'] for s in segments)

    # Release cache at run end so subsequent runs start clean
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    if device_str == 'mps':
        with contextlib.suppress(Exception):
            torch.mps.empty_cache()

    result: dict[str, Any] = {
        'label': label,
        'run_idx': run_idx,
        'audio_file': str(audio_path),
        'duration_s': round(duration_s, 1),
        'wall_seconds': round(elapsed, 3),
        'speaker_count': len(speakers),
        'segment_count': len(segments),
        'total_speech_seconds': round(total_speech_s, 2),
        'speakers': sorted(speakers),
        'stage_durations': stage_durations,
        'stage_cumulative_peak_vram_mb': cumulative_peaks,
        'stage_vram_delta_mb': stage_vram_delta,
        'overall_peak_vram_mb': overall_peak_mb,
        'overall_peak_over_baseline_mb': overall_peak_over_baseline_mb,
        'baseline_alloc_mb': baseline_alloc_mb,
        'steady_state_vram_mb': steady_state_mb,
        'vram_before_nvml_mb': vram_before_nvml,
        'vram_after_nvml_mb': vram_after_nvml,
        'vram_before_alloc_mb': vram_before_alloc,
        'rttm_path': rttm_path_str,
    }

    # Only include segment list on the first run (saves disk on N-run benchmarks).
    if run_idx == 0:
        result['all_segments'] = segments

    print(
        f'  [{label} run={run_idx}] Done in {elapsed:.1f}s | '
        f'{len(speakers)} speakers | {len(segments)} segments | '
        f'peak VRAM {overall_peak_mb} MB | steady {steady_state_mb} MB'
    )
    if stage_durations:
        for stage, dur in stage_durations.items():
            cum = cumulative_peaks.get(stage, 0)
            delta = stage_vram_delta.get(stage, 0)
            print(f'    {stage}: {dur:.2f}s   cum peak {cum} MB   (+{delta} MB)')

    return result


def run_diarization_with_profiler(
    pipeline,
    audio_path: str,
    label: str,
    device_str: str,
    trace_out_path: Path,
    rttm_out_path: Path | None = None,
    mps_poller: MPSVRAMPoller | None = None,
) -> dict:
    """Single run wrapped in torch.profiler; exports a Chrome trace."""
    from torch.profiler import ProfilerActivity, profile

    activities: list[Any] = [ProfilerActivity.CPU]
    if device_str.startswith('cuda'):
        activities.append(ProfilerActivity.CUDA)
    # MPS activity is not a standard ProfilerActivity across torch versions;
    # CPU-only profiling on MPS still captures op-level timing.

    trace_out_path.parent.mkdir(parents=True, exist_ok=True)

    with profile(activities=activities, record_shapes=True, with_stack=False) as prof:
        result = run_diarization(
            pipeline,
            audio_path,
            label,
            device_str,
            rttm_out_path=rttm_out_path,
            run_idx=0,
            mps_poller=mps_poller,
        )

    prof.export_chrome_trace(str(trace_out_path))
    result['profiler_trace'] = str(trace_out_path)
    print(f'  [profiler] Chrome trace saved to {trace_out_path}')
    return result
