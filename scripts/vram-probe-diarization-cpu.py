#!/usr/bin/env python3
"""CPU analogue of vram-probe-diarization.py.

Forces ``device='cpu'`` on the pyannote pipeline and samples process RSS
(resident set size, from ``/proc/self/status:VmRSS``) at 100 ms cadence
to reconstruct a peak-RAM trace. Supports three backend modes:

- ``torch-only``  : everything in torch (segmentation + WeSpeaker embedding).
- ``onnx-seg-fp32``: segmentation via ONNX Runtime FP32, embedding in torch.
- ``onnx-seg-int8``: segmentation via ONNX Runtime INT8, embedding in torch.

Runs in the ``diarization-probe`` container. ONNX models must exist at
``${MODEL_CACHE_DIR}/onnx/pyannote_segmentation_{fp32,int8}.onnx`` — pre-convert
via ``scripts/preconvert-onnx-models.py`` once.

Phase CPU feasibility study. See
``docs/diarization-vram-profile/cpu-feasibility-plan.md``.
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
log = logging.getLogger('cpu_probe')

SAMPLE_INTERVAL_S = 0.1
PYANNOTE_MODEL = 'pyannote/speaker-diarization-community-1'


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------
@dataclass
class CPURunResult:
    timestamp: str
    audio_file: str
    audio_duration_s: float
    config: str  # torch-only | onnx-seg-fp32 | onnx-seg-int8
    num_threads: int
    wall_time_s: float
    model_load_time_s: float
    diarize_time_s: float
    realtime_factor: float  # wall / duration; <1 means faster than realtime
    num_speakers_detected: int
    num_segments: int
    rss_baseline_mb: float
    rss_peak_mb: float
    rss_delta_mb: float
    samples_count: int
    stage_timings_s: dict[str, float]  # hook-derived per-stage wall times
    error: str | None = None
    samples: list[tuple[float, float]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------
def _rss_mb() -> float:
    """Process RSS in MB, from /proc/self/status:VmRSS."""
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    # "VmRSS:  123456 kB"
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


class RSSSampler:
    def __init__(self) -> None:
        self._samples: list[tuple[float, float]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0

    def start(self) -> None:
        self._t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._run, daemon=True, name='rss-sampler')
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(SAMPLE_INTERVAL_S):
            self._samples.append((time.perf_counter() - self._t0, _rss_mb()))

    def stop(self) -> list[tuple[float, float]]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return self._samples


# --------------------------------------------------------------------------
# Audio loading + hook
# --------------------------------------------------------------------------
def load_audio(wav_path: Path, max_seconds: float | None = None) -> tuple[dict, float]:
    """Load a 16 kHz WAV; optionally slice to the first ``max_seconds``.

    Returns ``(pipeline_input_dict, duration_s)``.
    """
    import torch

    with wave.open(str(wav_path), 'rb') as wf:
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
    if sr != 16000:
        raise ValueError(f'expected 16 kHz, got {sr}')
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if max_seconds is not None:
        limit = int(max_seconds * sr)
        data = data[:limit]
    duration = len(data) / sr
    wav = torch.from_numpy(np.ascontiguousarray(data)).unsqueeze(0)
    return {'waveform': wav, 'sample_rate': sr, 'uri': wav_path.stem}, duration


def make_stage_timer() -> tuple[Any, dict[str, float]]:
    """Pipeline hook that records the wall time each stage enters."""
    stage_durations: dict[str, float] = {}
    tracked = [
        'segmentation',
        'speaker_counting',
        'binarization',
        'embedding_inference_start',
        'embeddings',
        'clustering_start',
    ]
    t_first: dict[str, float] = {}

    def hook(stage_name: str, *_args: Any, **_kwargs: Any) -> None:
        if stage_name not in tracked:
            return
        now = time.perf_counter()
        if stage_name not in t_first:
            t_first[stage_name] = now

    return hook, stage_durations


# --------------------------------------------------------------------------
# Main run
# --------------------------------------------------------------------------
def run_one(args: argparse.Namespace) -> CPURunResult:
    import torch

    # Pin torch to the requested thread count BEFORE importing/loading pyannote
    # so MKL + OpenMP pick it up.
    torch.set_num_threads(args.num_threads)
    torch.set_num_interop_threads(max(1, args.num_threads // 2))
    os.environ['OMP_NUM_THREADS'] = str(args.num_threads)
    os.environ['MKL_NUM_THREADS'] = str(args.num_threads)
    os.environ['ONNX_NUM_THREADS'] = str(args.num_threads)

    # Force the fork budget helper to return bs=1 on CPU (already the
    # default, but set explicitly so the trace is unambiguous).
    os.environ['PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE'] = '1'

    gc.collect()
    baseline_mb = _rss_mb()
    log.info(
        f'Run: file={args.audio_file} config={args.config} threads={args.num_threads} '
        f'max_seconds={args.max_seconds} baseline_rss={baseline_mb:.1f}MB'
    )

    t_load0 = time.perf_counter()

    from pyannote.audio import Pipeline

    pipeline = Pipeline.from_pretrained(PYANNOTE_MODEL, token=os.environ.get('HUGGINGFACE_TOKEN'))
    if pipeline is None:
        raise RuntimeError(f'{PYANNOTE_MODEL} returned None')

    # Explicitly move to CPU (overrides any env HF_AUTO device)
    pipeline.to(torch.device('cpu'))

    # Optional ONNX segmentation offload. Triggers the fork's
    # _setup_onnx_cpu path which loads pre-converted ONNX models.
    if args.config in ('onnx-seg-fp32', 'onnx-seg-int8'):
        quantize = args.config == 'onnx-seg-int8'
        # Call the private method directly; public constructor kwarg
        # couldn't be retro-applied to an already-loaded pipeline.
        pipeline._setup_onnx_cpu(quantize=quantize, num_threads=args.num_threads)

    t_load = time.perf_counter() - t_load0

    audio, duration_s = load_audio(Path(args.audio_file), max_seconds=args.max_seconds)

    hook, stage_durations = make_stage_timer()

    sampler = RSSSampler()
    sampler.start()

    err: str | None = None
    num_speakers = 0
    num_segments = 0
    t_diar0 = time.perf_counter()
    try:
        output = pipeline(audio, hook=hook)
        annotation = getattr(output, 'speaker_diarization', output)
        if annotation is not None:
            num_speakers = len(set(annotation.labels()))
            num_segments = sum(1 for _ in annotation.itertracks())
    except Exception as e:
        err = f'{type(e).__name__}: {e}'
        log.exception('CPU probe run failed')

    t_diar = time.perf_counter() - t_diar0
    samples = sampler.stop()

    peak_mb = max((s[1] for s in samples), default=baseline_mb)
    rtf = (t_load + t_diar) / max(1e-6, duration_s)

    del pipeline
    gc.collect()

    return CPURunResult(
        timestamp=datetime.now(UTC).isoformat(),
        audio_file=Path(args.audio_file).name,
        audio_duration_s=round(duration_s, 2),
        config=args.config,
        num_threads=args.num_threads,
        wall_time_s=round(t_load + t_diar, 3),
        model_load_time_s=round(t_load, 3),
        diarize_time_s=round(t_diar, 3),
        realtime_factor=round(rtf, 3),
        num_speakers_detected=num_speakers,
        num_segments=num_segments,
        rss_baseline_mb=round(baseline_mb, 1),
        rss_peak_mb=round(peak_mb, 1),
        rss_delta_mb=round(max(0.0, peak_mb - baseline_mb), 1),
        samples_count=len(samples),
        stage_timings_s=stage_durations,
        error=err,
        samples=[(round(t, 3), round(mb, 1)) for t, mb in samples],
    )


def write_result(result: CPURunResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = int(result.audio_duration_s)
    name = (
        f'{Path(result.audio_file).stem}__dur-{dur}s__{result.config}__'
        f'threads-{result.num_threads}.json'
    )
    path = out_dir / name
    path.write_text(json.dumps(asdict(result), indent=2))
    log.info(f'Wrote {path}  RTF={result.realtime_factor}  spk={result.num_speakers_detected}')
    return path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        '--audio-file',
        default='/app/benchmark/test_audio/0.5h_1899s.wav',
        help='Path to 16 kHz WAV file.',
    )
    p.add_argument(
        '--max-seconds',
        type=float,
        default=None,
        help='Slice the input to the first N seconds (for short-duration tests).',
    )
    p.add_argument(
        '--config',
        choices=['torch-only', 'onnx-seg-fp32', 'onnx-seg-int8'],
        default='torch-only',
    )
    p.add_argument('--num-threads', type=int, default=8)
    p.add_argument('--out', default='/app/docs/diarization-vram-profile/raw/cpu/')
    p.add_argument('--smoke', action='store_true', help='30-second smoke test.')
    p.add_argument(
        '--hard-timeout-min',
        type=float,
        default=15.0,
        help='Hard wall-time kill switch (SIGALRM). Guards against runaway loops.',
    )
    return p.parse_args()


def _install_timeout(minutes: float) -> None:
    import signal

    def _alarm(_sig: int, _frm: Any) -> None:
        log.error(f'hard timeout after {minutes} min -- aborting run')
        os._exit(124)

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(int(minutes * 60))


def main() -> int:
    args = parse_args()
    _install_timeout(args.hard_timeout_min)

    if args.smoke:
        # Minimal smoke: 30 s of audio, onnx-int8, 8 threads.
        args.max_seconds = 30.0
        args.config = 'onnx-seg-int8'
        args.num_threads = 8

    result = run_one(args)
    write_result(result, Path(args.out))
    return 0 if result.error is None else 2


if __name__ == '__main__':
    sys.exit(main())
