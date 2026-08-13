#!/usr/bin/env python3
"""Thorough benchmark: multiple runs with RAM and GPU memory tracking.

Usage:
    # CUDA
    python benchmark-thorough.py --device cuda --runs 3
    # MPS
    python benchmark-thorough.py --device mps --runs 3
"""

import argparse
import gc
import json
import os
import sys
import time
import warnings

warnings.filterwarnings('ignore')
os.environ['PYANNOTE_METRICS_ENABLED'] = 'false'

# E402 on the block below: warnings.filterwarnings() and PYANNOTE_METRICS_ENABLED
# must be in place BEFORE torch / pyannote import, or the setting is read too late.
import numpy as np  # noqa: E402
import torch  # noqa: E402
import torchaudio  # noqa: E402


def get_process_rss_mb():
    """Get current process RSS (resident set size) in MB."""
    try:
        import resource

        # maxrss is in KB on Linux, bytes on macOS
        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == 'darwin':
            return maxrss / (1024 * 1024)  # bytes -> MB
        return maxrss / 1024  # KB -> MB
    except Exception:
        return 0


def get_gpu_memory_mb(device_type):
    """Get current GPU memory usage in MB."""
    if device_type == 'cuda':
        return torch.cuda.memory_allocated() / (1024 * 1024)
    elif device_type == 'mps' and hasattr(torch.mps, 'current_allocated_memory'):
        return torch.mps.current_allocated_memory() / (1024 * 1024)
    return 0


def get_gpu_memory_reserved_mb(device_type):
    """Get reserved/driver GPU memory in MB."""
    if device_type == 'cuda':
        return torch.cuda.memory_reserved() / (1024 * 1024)
    elif device_type == 'mps' and hasattr(torch.mps, 'driver_allocated_memory'):
        return torch.mps.driver_allocated_memory() / (1024 * 1024)
    return 0


def run_single(pipeline, waveform, sample_rate, duration, device_type, run_idx):
    """Run a single diarization and collect metrics."""
    gc.collect()
    if device_type == 'cuda':
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
    elif device_type == 'mps' and hasattr(torch.mps, 'empty_cache'):
        torch.mps.empty_cache()

    rss_before = get_process_rss_mb()
    gpu_before = get_gpu_memory_mb(device_type)
    gpu_reserved_before = get_gpu_memory_reserved_mb(device_type)

    timing = {}

    # ARG001: the caller dictates this callback signature.
    def hook(name, artefact, **kw):  # noqa: ARG001
        now = time.perf_counter()
        if name not in timing:
            timing[name] = {'start': now}
        timing[name]['last'] = now

    audio_input = {'waveform': waveform, 'sample_rate': sample_rate}

    t0 = time.perf_counter()
    output = pipeline(audio_input, hook=hook)
    total = time.perf_counter() - t0

    rss_after = get_process_rss_mb()
    gpu_after = get_gpu_memory_mb(device_type)
    gpu_reserved_after = get_gpu_memory_reserved_mb(device_type)

    gpu_peak = 0
    if device_type == 'cuda':
        gpu_peak = torch.cuda.max_memory_allocated() / (1024 * 1024)

    if hasattr(output, 'exclusive_speaker_diarization'):
        ann = output.exclusive_speaker_diarization
    elif hasattr(output, 'speaker_diarization'):
        ann = output.speaker_diarization
    else:
        ann = output

    speakers = len(ann.labels())
    segments = len(list(ann.itertracks(yield_label=True)))

    stages = {}
    for name, t in sorted(timing.items(), key=lambda x: x[1]['start']):
        dur = t['last'] - t['start']
        if dur > 0.01:
            stages[name] = round(dur, 3)

    result = {
        'run': run_idx,
        'total_s': round(total, 2),
        'speakers': speakers,
        'segments': segments,
        'rtf': round(total / duration, 5),
        'rss_before_mb': round(rss_before, 1),
        'rss_after_mb': round(rss_after, 1),
        'rss_delta_mb': round(rss_after - rss_before, 1),
        'gpu_before_mb': round(gpu_before, 1),
        'gpu_after_mb': round(gpu_after, 1),
        'gpu_reserved_before_mb': round(gpu_reserved_before, 1),
        'gpu_reserved_after_mb': round(gpu_reserved_after, 1),
        'gpu_peak_mb': round(gpu_peak, 1),
        'stages': stages,
    }

    print(
        f'  Run {run_idx}: {total:.1f}s | {speakers} spk | {segments} seg | '
        f'RSS: {rss_before:.0f}->{rss_after:.0f}MB | '
        f'GPU: {gpu_before:.0f}->{gpu_after:.0f}MB (peak: {gpu_peak:.0f}MB)'
    )

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default='auto', help='cuda, mps, cpu, or auto')
    parser.add_argument('--runs', type=int, default=3, help='Number of runs per file')
    parser.add_argument('--file', type=str, default='0.5h', help="Filter test files (e.g., '0.5h')")
    parser.add_argument('--audio-dir', default='benchmark/test_audio')
    args = parser.parse_args()

    if args.device == 'auto':
        if torch.cuda.is_available():
            device_str = 'cuda'
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device_str = 'mps'
        else:
            device_str = 'cpu'
    else:
        device_str = args.device

    token = os.environ.get('HUGGINGFACE_TOKEN') or os.environ.get('HF_TOKEN')
    if not token:
        print('ERROR: Set HUGGINGFACE_TOKEN')
        sys.exit(1)

    from pathlib import Path

    audio_dir = Path(args.audio_dir)
    files = sorted(f for f in audio_dir.glob('*.wav') if args.file in f.name)
    if not files:
        print(f"No files matching '{args.file}' in {audio_dir}")
        sys.exit(1)

    device = torch.device(device_str)
    print(f'Device: {device_str}')
    print(f'PyTorch: {torch.__version__}')
    print(f'Runs: {args.runs}')
    print(f'RSS at start: {get_process_rss_mb():.0f}MB')
    print()

    from pyannote.audio import Pipeline

    # Use different model names depending on what's accessible
    for model_name in [
        'pyannote/speaker-diarization-3.1',
        'pyannote/speaker-diarization-community-1',
    ]:
        try:
            pipeline = Pipeline.from_pretrained(model_name, token=token)
            print(f'Model: {model_name}')
            break
        except Exception:
            continue
    else:
        print('ERROR: Could not load any pipeline model')
        sys.exit(1)

    pipeline = pipeline.to(device)
    pipeline.embedding_batch_size = 32

    print(f'RSS after pipeline load: {get_process_rss_mb():.0f}MB')
    print(f'GPU after pipeline load: {get_gpu_memory_mb(device_str):.0f}MB')
    print()

    all_results = []
    for audio_path in files:
        print(f'{"=" * 60}')
        print(f'File: {audio_path.name}')
        print(f'{"=" * 60}')

        waveform, sr = torchaudio.load(str(audio_path))
        if sr != 16000:
            waveform = torchaudio.functional.resample(waveform, sr, 16000)
            sr = 16000
        duration = waveform.shape[1] / sr
        waveform_mb = waveform.element_size() * waveform.nelement() / (1024 * 1024)
        print(f'Duration: {duration:.1f}s ({duration / 3600:.2f}h)')
        print(f'Waveform RAM: {waveform_mb:.1f}MB')
        print()

        file_results = []
        for run_idx in range(1, args.runs + 1):
            result = run_single(pipeline, waveform, sr, duration, device_str, run_idx)
            result['file'] = audio_path.name
            result['duration_s'] = duration
            file_results.append(result)
        all_results.extend(file_results)

        # Summary for this file
        times = [r['total_s'] for r in file_results]
        print(
            f'\n  Summary: mean={np.mean(times):.1f}s, '
            f'std={np.std(times):.1f}s, '
            f'min={np.min(times):.1f}s, max={np.max(times):.1f}s'
        )
        print()

    # Save results
    output = {
        'device': device_str,
        'pytorch': torch.__version__,
        'python': sys.version.split()[0],
        'runs_per_file': args.runs,
        'results': all_results,
    }
    out_file = f'benchmark_thorough_{device_str}_{time.strftime("%Y%m%d_%H%M%S")}.json'
    with open(out_file, 'w') as f:
        json.dump(output, f, indent=2)
    print(f'Results saved to {out_file}')


if __name__ == '__main__':
    main()
