#!/usr/bin/env python3
"""PyAnnote MPS benchmark script for Apple Silicon.

Runs stock vs optimized diarization on test audio files using MPS backend.
Usage:
    source venv/bin/activate
    HUGGINGFACE_TOKEN=hf_xxx python benchmark-pyannote-mps.py [--file 0.5h]
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Disable pyannote telemetry
os.environ['PYANNOTE_METRICS_ENABLED'] = 'false'

import torch
import torchaudio


def get_test_files(base_dir: Path, filter_name: str | None = None):
    """Find test audio files."""
    files = sorted(base_dir.glob('*.wav'))
    if filter_name:
        files = [f for f in files if filter_name in f.name]
    return files


def run_benchmark(audio_path: Path, token: str, device: str = 'mps'):
    """Run diarization benchmark on a single file."""
    from pyannote.audio import Pipeline

    print(f'\n{"=" * 60}')
    print(f'File: {audio_path.name}')
    print(f'Device: {device}')
    print(f'{"=" * 60}')

    # Load audio
    print('Loading audio...')
    waveform, sample_rate = torchaudio.load(str(audio_path))
    if sample_rate != 16000:
        waveform = torchaudio.functional.resample(waveform, sample_rate, 16000)
        sample_rate = 16000

    duration_s = waveform.shape[1] / sample_rate
    print(f'Duration: {duration_s:.1f}s ({duration_s / 3600:.2f}h)')

    # Load pipeline
    print('Loading pipeline...')
    t0 = time.perf_counter()
    pipeline = Pipeline.from_pretrained('pyannote/speaker-diarization-community-1', token=token)

    if pipeline is None:
        # Try fallback model
        pipeline = Pipeline.from_pretrained('pyannote/speaker-diarization-3.1', token=token)

    if pipeline is None:
        print('ERROR: Could not load pipeline. Check your HuggingFace token.')
        return None

    pipeline = pipeline.to(torch.device(device))
    pipeline.embedding_batch_size = 32  # Triggers auto-selection on optimized fork
    load_time = time.perf_counter() - t0
    print(f'Pipeline loaded in {load_time:.1f}s')

    # Prepare input
    audio_input = {'waveform': waveform, 'sample_rate': sample_rate}

    # Stage timing via hook
    stage_timing = {}

    # ARG001: the caller dictates this callback signature.
    def timing_hook(step_name, step_artefact, **kwargs):  # noqa: ARG001
        now = time.perf_counter()
        if step_name not in stage_timing:
            stage_timing[step_name] = {'start': now, 'last': now}
        else:
            stage_timing[step_name]['last'] = now

    # Run diarization
    print('Running diarization...')
    t_start = time.perf_counter()
    output = pipeline(audio_input, hook=timing_hook)
    total_time = time.perf_counter() - t_start

    # Extract results
    if hasattr(output, 'exclusive_speaker_diarization'):
        annotation = output.exclusive_speaker_diarization
    elif hasattr(output, 'speaker_diarization'):
        annotation = output.speaker_diarization
    else:
        annotation = output

    speakers = annotation.labels()
    segments = list(annotation.itertracks(yield_label=True))

    print('\nResults:')
    print(f'  Total time: {total_time:.2f}s')
    print(f'  Speakers: {len(speakers)}')
    print(f'  Segments: {len(segments)}')
    print(f'  Real-time factor: {total_time / duration_s:.4f}x')

    # Stage breakdown
    if stage_timing:
        print('\n  Stage breakdown:')
        stages = sorted(stage_timing.items(), key=lambda x: x[1]['start'])
        for name, times in stages:
            dur = times['last'] - times['start']
            if dur > 0.01:
                print(f'    {name}: {dur:.2f}s')

    result = {
        'file': audio_path.name,
        'duration_s': duration_s,
        'device': device,
        'total_time': round(total_time, 3),
        'speakers': len(speakers),
        'segments': len(segments),
        'rtf': round(total_time / duration_s, 6),
        'load_time': round(load_time, 3),
        'stages': {
            name: round(times['last'] - times['start'], 3)
            for name, times in sorted(stage_timing.items(), key=lambda x: x[1]['start'])
        },
    }

    return result


def main():
    parser = argparse.ArgumentParser(description='PyAnnote MPS Benchmark')
    parser.add_argument(
        '--file',
        type=str,
        default=None,
        help="Filter test files (e.g., '0.5h' for 30-min file only)",
    )
    parser.add_argument(
        '--audio-dir',
        type=str,
        default='benchmark/test_audio',
        help='Directory containing test audio files',
    )
    parser.add_argument(
        '--output', type=str, default='benchmark/results', help='Directory for result JSON files'
    )
    parser.add_argument('--device', type=str, default='mps', help='Device to use (mps or cpu)')
    args = parser.parse_args()

    # Check device
    if args.device == 'mps' and not torch.backends.mps.is_available():
        print('ERROR: MPS not available on this system')
        sys.exit(1)

    # Get HF token
    token = os.environ.get('HUGGINGFACE_TOKEN') or os.environ.get('HF_TOKEN')
    if not token:
        print('ERROR: Set HUGGINGFACE_TOKEN or HF_TOKEN environment variable')
        sys.exit(1)

    # Find test files
    audio_dir = Path(args.audio_dir)
    if not audio_dir.exists():
        print(f'ERROR: Audio directory not found: {audio_dir}')
        sys.exit(1)

    files = get_test_files(audio_dir, args.file)
    if not files:
        print(f'ERROR: No .wav files found in {audio_dir}')
        sys.exit(1)

    print(f'Found {len(files)} test file(s)')
    print(f'Device: {args.device}')
    print(f'PyTorch: {torch.__version__}')
    print(f'MPS available: {torch.backends.mps.is_available()}')

    # Run benchmarks
    results = []
    for audio_path in files:
        result = run_benchmark(audio_path, token, device=args.device)
        if result:
            results.append(result)

    # Save results
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'benchmark_mps_{timestamp}.json'
    with open(output_file, 'w') as f:
        json.dump(
            {
                'device': args.device,
                'pytorch_version': torch.__version__,
                'python_version': sys.version,
                'timestamp': timestamp,
                'results': results,
            },
            f,
            indent=2,
        )

    print(f'\n{"=" * 60}')
    print(f'Results saved to {output_file}')

    # Summary table
    if results:
        print('\nSummary:')
        print(f'{"File":<20} {"Time":>8} {"Speakers":>8} {"Segments":>8} {"RTF":>8}')
        print('-' * 56)
        total_time = 0
        for r in results:
            print(
                f'{r["file"]:<20} {r["total_time"]:>7.1f}s '
                f'{r["speakers"]:>8} {r["segments"]:>8} '
                f'{r["rtf"]:>7.4f}x'
            )
            total_time += r['total_time']
        print('-' * 56)
        print(f'{"TOTAL":<20} {total_time:>7.1f}s')


if __name__ == '__main__':
    main()
