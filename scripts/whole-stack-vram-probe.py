#!/usr/bin/env python3
"""Phase A.6 — Whole-stack VRAM probe: Whisper + diarization.

Runs the full OpenTranscribe model path on a single audio file:
  load Whisper → transcribe → release Whisper → load diarization → diarize.

Captures NVML peaks at every handoff, so the output identifies:
- device_baseline_mb (idle GPU before any model load)
- whisper_load_peak_mb (weights loaded, no inference yet)
- whisper_transcribe_peak_mb (during inference)
- post_whisper_release_mb (after release_transcriber() — A.6b verifies <100 MB above baseline)
- diarize_load_peak_mb (diarization weights resident)
- diarize_run_peak_mb (during diarization)

MUST run inside the benchmark container. See A.0.0.
"""

from __future__ import annotations

import sys

sys.path.insert(0, '/app')

import argparse
import ctypes
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('whole_stack')

A6000_TOTAL_GB = 48.0
NVML_SAMPLE_INTERVAL_S = 0.1
BENCHMARK_ROOT = Path('/app/benchmark/test_audio')


class _NvmlMem(ctypes.Structure):
    _fields_ = [
        ('total', ctypes.c_ulonglong),
        ('free', ctypes.c_ulonglong),
        ('used', ctypes.c_ulonglong),
    ]


def _load_nvml() -> Any:
    try:
        lib = ctypes.CDLL('libnvidia-ml.so.1')
        lib.nvmlInit_v2()
        return lib
    except Exception as e:
        log.warning(f'NVML load failed: {e}')
        return None


def _nvml_used_mb(lib: Any, handle: Any) -> float:
    mem = _NvmlMem()
    lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem))
    return mem.used / (1024**2)


class Sampler:
    def __init__(self, lib: Any, handle: Any) -> None:
        self.lib = lib
        self.handle = handle
        self._samples: list[tuple[float, str, float]] = []  # (t, stage, used_mb)
        self._stage = 'baseline'
        self._stop = threading.Event()
        self._t0 = 0.0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._run, daemon=True, name='nvml-sampler')
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(NVML_SAMPLE_INTERVAL_S):
            try:
                self._samples.append(
                    (
                        time.perf_counter() - self._t0,
                        self._stage,
                        _nvml_used_mb(self.lib, self.handle),
                    )
                )
            except Exception:
                continue

    def set_stage(self, stage: str) -> None:
        self._stage = stage

    def stop(self) -> list[tuple[float, str, float]]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return self._samples


@dataclass
class StageMeasurement:
    name: str
    t_start_s: float
    t_end_s: float
    nvml_peak_mb: float
    nvml_mean_mb: float
    nvml_sample_count: int


@dataclass
class WholeStackResult:
    timestamp: str
    audio_file: str
    audio_duration_s: float
    cap_gb: float | None
    whisper_model: str
    whisper_compute_type: str
    diarization_batch_size: int
    diarization_precision: str
    device_baseline_mb: float
    stages: list[StageMeasurement]
    handoff_residue_mb: float  # A.6b: post_whisper_release − baseline
    segments_transcribed: int
    speakers_detected: int
    error: str | None = None
    all_samples: list[tuple[float, str, float]] = field(default_factory=list)


def require_container() -> None:
    if Path('/.dockerenv').exists() or os.environ.get('OPENTRANSCRIBE_IN_CONTAINER') == '1':
        return
    log.error('Refusing to run outside container. See plan A.0.0.')
    sys.exit(2)


def apply_cap(cap_gb: str) -> float | None:
    if cap_gb in ('unlimited', 'unl', 'none'):
        return None
    import torch

    cap = float(cap_gb)
    frac = cap / A6000_TOTAL_GB
    torch.cuda.set_per_process_memory_fraction(frac, 0)
    log.info(f'Applied VRAM cap: {cap} GB ({frac:.4f} of A6000)')
    return cap


def load_audio_wave(wav_path: Path) -> tuple[np.ndarray, float]:
    with wave.open(str(wav_path), 'rb') as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if sr != 16000:
        raise ValueError(f'Expected 16kHz, got {sr}')
    return data, n / sr


def measure_stage(
    samples: list[tuple[float, str, float]],
    name: str,
    t_start: float,
    t_end: float,
) -> StageMeasurement:
    in_stage = [s for s in samples if t_start <= s[0] <= t_end and s[1] == name]
    if not in_stage:
        # Fall back: window only
        in_stage = [s for s in samples if t_start <= s[0] <= t_end]
    if not in_stage:
        return StageMeasurement(name, t_start, t_end, 0.0, 0.0, 0)
    used = [s[2] for s in in_stage]
    return StageMeasurement(
        name=name,
        t_start_s=round(t_start, 3),
        t_end_s=round(t_end, 3),
        nvml_peak_mb=round(max(used), 1),
        nvml_mean_mb=round(sum(used) / len(used), 1),
        nvml_sample_count=len(used),
    )


def run_one(args: argparse.Namespace) -> WholeStackResult:
    import torch

    from app.transcription.config import TranscriptionConfig
    from app.transcription.diarizer import SpeakerDiarizer
    from app.transcription.transcriber import Transcriber

    os.environ.setdefault('PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE', str(args.diarization_batch_size))
    cap_applied = apply_cap(args.cap_gb)

    lib = _load_nvml()
    if lib is None:
        raise RuntimeError('NVML unavailable')
    handle = ctypes.c_void_p()
    lib.nvmlDeviceGetHandleByIndex(0, ctypes.byref(handle))
    baseline_mb = _nvml_used_mb(lib, handle)
    log.info(f'Baseline device_used_mb={baseline_mb:.1f}')

    sampler = Sampler(lib, handle)
    sampler.set_stage('baseline')
    sampler.start()
    time.sleep(0.3)

    wav_path = BENCHMARK_ROOT / args.audio_file
    audio, duration_s = load_audio_wave(wav_path)

    err: str | None = None
    segments_transcribed = 0
    speakers_detected = 0
    stages: list[StageMeasurement] = []

    try:
        # --- Whisper load ---
        sampler.set_stage('whisper_load')
        t0 = time.perf_counter() - sampler._t0
        cfg = TranscriptionConfig(
            model_name=args.whisper_model,
            compute_type=args.whisper_compute_type,
            device='cuda',
            device_index=0,
            beam_size=1,  # keep probe short; beam=1 avoids decoder KV explosion
            batch_size=8,
            enable_diarization=False,
        )
        transcriber = Transcriber(cfg)
        transcriber.load_model()
        t_whisper_loaded = time.perf_counter() - sampler._t0
        stages.append(measure_stage(sampler._samples, 'whisper_load', t0, t_whisper_loaded))

        # --- Whisper transcribe ---
        sampler.set_stage('whisper_transcribe')
        t0 = t_whisper_loaded
        out = transcriber.transcribe(audio)
        segments_transcribed = len(out.get('segments', []))
        t_whisper_ran = time.perf_counter() - sampler._t0
        stages.append(measure_stage(sampler._samples, 'whisper_transcribe', t0, t_whisper_ran))

        # --- Release Whisper (A.6b handoff check) ---
        sampler.set_stage('whisper_release')
        transcriber.unload_model()
        del transcriber
        gc.collect()
        torch.cuda.empty_cache()
        time.sleep(0.5)  # give NVML a few samples to settle
        t_whisper_gone = time.perf_counter() - sampler._t0
        stages.append(
            measure_stage(sampler._samples, 'whisper_release', t_whisper_ran, t_whisper_gone)
        )
        post_release_samples = [s for s in sampler._samples if s[1] == 'whisper_release']
        post_release_mb = (
            min(s[2] for s in post_release_samples) if post_release_samples else baseline_mb
        )
        handoff_residue = post_release_mb - baseline_mb
        log.info(f'Handoff residue: {handoff_residue:.1f} MB (A.6b gate: <100 MB)')

        # --- Diarization load ---
        sampler.set_stage('diarize_load')
        t0 = t_whisper_gone
        diarizer = SpeakerDiarizer(cfg)
        diarizer.load_model()
        t_diar_loaded = time.perf_counter() - sampler._t0
        stages.append(measure_stage(sampler._samples, 'diarize_load', t0, t_diar_loaded))

        # --- Diarization run ---
        sampler.set_stage('diarize_run')
        t0 = t_diar_loaded
        audio_tensor = torch.from_numpy(audio).unsqueeze(0)
        diar_input = {'waveform': audio_tensor, 'sample_rate': 16000, 'uri': wav_path.stem}
        diar_output = diarizer._pipeline(diar_input)  # type: ignore[attr-defined]
        annotation = getattr(diar_output, 'speaker_diarization', diar_output)
        speakers_detected = len(annotation.labels()) if annotation is not None else 0
        t_diar_ran = time.perf_counter() - sampler._t0
        stages.append(measure_stage(sampler._samples, 'diarize_run', t0, t_diar_ran))

    except Exception as e:
        err = f'{type(e).__name__}: {e}'
        log.exception('whole-stack probe failed')

    all_samples = sampler.stop()

    return WholeStackResult(
        timestamp=datetime.now(UTC).isoformat(),
        audio_file=args.audio_file,
        audio_duration_s=round(duration_s, 2),
        cap_gb=cap_applied,
        whisper_model=args.whisper_model,
        whisper_compute_type=args.whisper_compute_type,
        diarization_batch_size=args.diarization_batch_size,
        diarization_precision='fp32' if not args.diarization_mp else 'fp16',
        device_baseline_mb=round(baseline_mb, 1),
        stages=stages,
        handoff_residue_mb=round(handoff_residue, 1) if err is None else 0.0,
        segments_transcribed=segments_transcribed,
        speakers_detected=speakers_detected,
        error=err,
        all_samples=[(round(t, 3), stage, round(mb, 1)) for t, stage, mb in all_samples],
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--audio-file', default='0.5h_1899s.wav')
    p.add_argument('--whisper-model', default='small')
    p.add_argument('--whisper-compute-type', default='float16')
    p.add_argument('--diarization-batch-size', type=int, default=16)
    p.add_argument(
        '--diarization-mp', action='store_true', help='Enable fp16 autocast in diarization'
    )
    p.add_argument('--cap-gb', default='unlimited', help='e.g. 4, 6, 8, unlimited')
    p.add_argument('--out', default='/app/docs/diarization-vram-profile/raw/whole-stack/')
    p.add_argument(
        '--sweep', action='store_true', help='Run predefined caps × whisper-models matrix'
    )
    return p.parse_args()


def write_result(result: WholeStackResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    cap_tag = 'unl' if result.cap_gb is None else f'{result.cap_gb:.0f}GB'
    fname = (
        f'{Path(result.audio_file).stem}__cap-{cap_tag}__whisper-{result.whisper_model}__'
        f'bs-{result.diarization_batch_size}.json'
    )
    path = out_dir / fname
    path.write_text(json.dumps(asdict(result), indent=2))
    log.info(f'Wrote {path}')
    return path


def run_sweep(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    caps = ['4', '6', '8', 'unlimited']
    models = ['base', 'small', 'medium']
    total = len(caps) * len(models)
    idx = 0
    fails = 0
    t0 = time.perf_counter()
    for cap in caps:
        for model in models:
            idx += 1
            log.info(f'=== Whole-stack sweep {idx}/{total}: cap={cap} whisper={model} ===')
            sub = argparse.Namespace(**{**vars(args), 'cap_gb': cap, 'whisper_model': model})
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
    log.info(f'Whole-stack sweep complete: {idx} runs, {fails} failures')
    return 0 if fails == 0 else 2


def main() -> int:
    args = parse_args()
    require_container()
    if args.sweep:
        return run_sweep(args)
    result = run_one(args)
    write_result(result, Path(args.out))
    if result.error:
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
