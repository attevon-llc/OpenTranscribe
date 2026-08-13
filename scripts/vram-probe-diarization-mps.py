#!/usr/bin/env python3
"""MPS analogue of vram-probe-diarization.py.

Runs on macOS (Apple Silicon) and mirrors the CUDA isolation probe as
closely as the MPS torch API permits:

- Polls torch.mps.driver_allocated_memory() at 100 ms cadence in a
  background thread to reconstruct a peak-allocator trace (MPS lacks a
  torch.mps.max_memory_allocated equivalent).
- Forces embedding batch sizes via PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE
  (added to the fork in Phase A characterization).
- Emits per-run JSON files compatible with the CUDA schema so
  cross-platform analysis scripts can read both sets.

Runs on the Mac Studio via SSH in the transcribe-app workflow:

    ssh superstudio@192.168.30.26 "\\
        cd ~/repos/pyannote-audio && source venv/bin/activate && \\
        python /tmp/vram-probe-diarization-mps.py \\
            --audio-file /tmp/test_0.5h.wav \\
            --small-batch-sweep \\
            --out /tmp/mps-probe-out"

The host workstation scp's this file + the audio over, runs the probe,
then scp's the JSONs back into docs/diarization-vram-profile/raw/mps/.

Refs: plan i-need-a-full-stateful-origami.md § A.2 + A.7.
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import sys
import threading
import time
import wave
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
log = logging.getLogger('mps_probe')

MPS_SAMPLE_INTERVAL_S = 0.1
PYANNOTE_MODEL = 'pyannote/speaker-diarization-community-1'


@dataclass
class MPSRunResult:
    timestamp: str
    audio_file: str
    audio_duration_s: float
    device: str
    embedding_batch_size_setting: int
    mixed_precision: str
    repeat_index: int
    wall_time_s: float
    model_load_time_s: float
    diarize_time_s: float
    num_speakers_detected: int
    num_segments: int
    mps_baseline_mb: float
    mps_peak_mb: float
    mps_delta_mb: float
    mps_samples_count: int
    recommended_max_mb: float
    error: str | None = None
    samples: list[tuple[float, float]] = field(default_factory=list)


class MPSSampler:
    """Background thread polling torch.mps.driver_allocated_memory."""

    def __init__(self) -> None:
        import torch

        self._torch = torch
        self._samples: list[tuple[float, float]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0

    def start(self) -> None:
        self._t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._run, daemon=True, name='mps-sampler')
        self._thread.start()

    def _run(self) -> None:
        mps = self._torch.mps
        while not self._stop.wait(MPS_SAMPLE_INTERVAL_S):
            try:
                used_mb = mps.driver_allocated_memory() / (1024**2)
                self._samples.append((time.perf_counter() - self._t0, used_mb))
            except Exception:
                continue

    def stop(self) -> list[tuple[float, float]]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return self._samples


def load_audio(wav_path: Path) -> dict[str, Any]:
    import torch

    with wave.open(str(wav_path), 'rb') as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    if sr != 16000:
        raise ValueError(f'expected 16kHz, got {sr}')
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return {
        'waveform': torch.from_numpy(np.ascontiguousarray(data)).unsqueeze(0),
        'sample_rate': sr,
        'uri': wav_path.stem,
    }


def run_one(args: argparse.Namespace) -> MPSRunResult:
    import torch
    from pyannote.audio import Pipeline

    if not torch.backends.mps.is_available():
        raise RuntimeError('MPS not available')

    # Force the fork's auto-scaler to honor our batch choice.
    if args.embedding_batch_size and args.embedding_batch_size > 0:
        os.environ['PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE'] = str(args.embedding_batch_size)
    else:
        os.environ.pop('PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE', None)

    gc.collect()
    torch.mps.empty_cache()
    baseline_mb = torch.mps.driver_allocated_memory() / (1024**2)
    recommended_max = (
        torch.mps.recommended_max_memory() / (1024**2)
        if hasattr(torch.mps, 'recommended_max_memory')
        else 0.0
    )
    log.info(
        f'Run: file={args.audio_file} bs={args.embedding_batch_size} '
        f'mp={args.mixed_precision} baseline={baseline_mb:.1f}MB'
    )

    t_load0 = time.perf_counter()
    pipeline = Pipeline.from_pretrained(PYANNOTE_MODEL, token=os.environ.get('HUGGINGFACE_TOKEN'))
    if pipeline is None:
        raise RuntimeError(f'{PYANNOTE_MODEL} load returned None')
    pipeline.to(torch.device('mps'))
    if hasattr(pipeline, 'embedding_mixed_precision'):
        pipeline.embedding_mixed_precision = args.mixed_precision == 'on'
    t_load = time.perf_counter() - t_load0

    sampler = MPSSampler()
    sampler.start()

    err: str | None = None
    num_speakers = 0
    num_segments = 0
    t_diar0 = time.perf_counter()
    try:
        audio = load_audio(Path(args.audio_file))
        output = pipeline(audio)
        annotation = getattr(output, 'speaker_diarization', output)
        if annotation is not None:
            num_speakers = len(set(annotation.labels()))
            num_segments = sum(1 for _ in annotation.itertracks())
        torch.mps.synchronize()
    except Exception as e:
        err = f'{type(e).__name__}: {e}'
        log.exception('mps probe failed')

    samples = sampler.stop()
    t_diar = time.perf_counter() - t_diar0

    peak_mb = max((s[1] for s in samples), default=baseline_mb)
    delta_mb = max(0.0, peak_mb - baseline_mb)

    del pipeline
    gc.collect()
    torch.mps.empty_cache()

    return MPSRunResult(
        timestamp=datetime.now(UTC).isoformat(),
        audio_file=Path(args.audio_file).name,
        audio_duration_s=round(wave.open(args.audio_file, 'rb').getnframes() / 16000, 2),
        device='mps',
        embedding_batch_size_setting=args.embedding_batch_size,
        mixed_precision=args.mixed_precision,
        repeat_index=args.repeat_index,
        wall_time_s=round(t_load + t_diar, 3),
        model_load_time_s=round(t_load, 3),
        diarize_time_s=round(t_diar, 3),
        num_speakers_detected=num_speakers,
        num_segments=num_segments,
        mps_baseline_mb=round(baseline_mb, 1),
        mps_peak_mb=round(peak_mb, 1),
        mps_delta_mb=round(delta_mb, 1),
        mps_samples_count=len(samples),
        recommended_max_mb=round(recommended_max, 1),
        error=err,
        samples=[(round(t, 3), round(mb, 1)) for t, mb in samples],
    )


def write_result(result: MPSRunResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    bs = result.embedding_batch_size_setting
    fname = (
        f'{Path(result.audio_file).stem}__mps__bs-{bs}__mp-{result.mixed_precision}'
        f'__r{result.repeat_index}.json'
    )
    path = out_dir / fname
    path.write_text(json.dumps(asdict(result), indent=2))
    log.info(f'Wrote {path}')
    return path


def run_small_batch_sweep(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    batches = [1, 4, 8, 16, 32, 64]
    precisions = ['off']
    total = len(batches) * len(precisions)
    idx = 0
    fails = 0
    t0 = time.perf_counter()
    for bs in batches:
        for mp in precisions:
            idx += 1
            log.info(f'=== MPS sweep {idx}/{total} ===')
            sub = argparse.Namespace(
                **{
                    **vars(args),
                    'embedding_batch_size': bs,
                    'mixed_precision': mp,
                    'repeat_index': 0,
                }
            )
            try:
                result = run_one(sub)
                write_result(result, out_dir)
                if result.error:
                    fails += 1
            except Exception as e:
                log.error(f'Sweep run {idx} failed: {e}')
                fails += 1
            log.info(
                f'Sweep {idx}/{total} fails={fails} elapsed={(time.perf_counter() - t0) / 60:.1f}min'
            )
    log.info(f'MPS sweep complete: {idx} runs, {fails} failures')
    return 0 if fails == 0 else 2


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--audio-file', default='/tmp/test_0.5h.wav')
    p.add_argument('--embedding-batch-size', type=int, default=16)
    p.add_argument('--mixed-precision', choices=['off', 'on'], default='off')
    p.add_argument('--repeat-index', type=int, default=0)
    p.add_argument('--out', default='/tmp/mps-probe-out')
    p.add_argument('--small-batch-sweep', action='store_true')
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.small_batch_sweep:
        return run_small_batch_sweep(args)
    result = run_one(args)
    write_result(result, Path(args.out))
    return 0 if result.error is None else 2


if __name__ == '__main__':
    sys.exit(main())
