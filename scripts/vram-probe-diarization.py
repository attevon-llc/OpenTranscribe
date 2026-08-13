#!/usr/bin/env python3
"""Isolation harness for PyAnnote diarization VRAM characterization.

MUST RUN INSIDE THE opentranscribe-celery-worker CONTAINER. Running from the
host Python environment against a production GPU wedged the A6000 driver on
2026-04-19; the script hard-fails unless /.dockerenv is present. See plan
A.0.0 for the post-mortem.

Correct invocation (from host shell):
    # Does NOT stop the worker; runs the probe in a new one-shot container
    # that shares the same image / CUDA stack:
    docker compose run --rm --entrypoint "" celery-worker \\
        python /app/scripts/vram-probe-diarization.py --smoke \\
        --out /app/docs/diarization-vram-profile/raw/

Forbidden:
    CUDA_VISIBLE_DEVICES=0 python scripts/vram-probe-diarization.py ...   # BREAKS GPU

Loads the PyAnnote pipeline directly (no backend, no Celery, no Whisper) and
measures per-run wall-time, NVML device peak, PyTorch allocator peak, and
per-stage peaks via the pipeline hook callback. Supports simulating smaller
GPUs on the available A6000 via torch.cuda.set_per_process_memory_fraction.

See docs for Phase A methodology:
    /home/superdave/.claude/plans/i-need-a-full-stateful-origami.md
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Note: PYTORCH_CUDA_ALLOC_CONF allocator tweaks intentionally not set. Syntax
# varies across torch versions (2.8 rejects bare "max_split_size_mb=128") and
# the fragmentation control is cosmetic for these measurements. If a matrix run
# shows noise > 5% repeat-to-repeat, revisit.

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
AUDIO_DIR = PROJECT_ROOT / 'benchmark' / 'test_audio'
DEFAULT_OUT_DIR = PROJECT_ROOT / 'docs' / 'diarization-vram-profile' / 'raw'

PYANNOTE_V4_MODEL = 'pyannote/speaker-diarization-community-1'
A6000_TOTAL_GB = 48.0
NVML_SAMPLE_INTERVAL_S = 0.1

logger = logging.getLogger('vram_probe')


@dataclass
class StagePeak:
    """Per-stage peak measurements captured via the pipeline hook callback."""

    stage: str
    t_rel_s: float
    torch_allocated_mb: float
    torch_reserved_mb: float
    torch_max_allocated_mb: float


@dataclass
class RunResult:
    """One measurement run, serialized to JSON."""

    timestamp: str
    file: str
    file_duration_s: float
    cap_gb: float | None
    embedding_batch_size_setting: int
    effective_batch_size_observed: int | None
    mixed_precision: str
    repeat_index: int
    wall_time_s: float
    model_load_time_s: float
    diarize_time_s: float
    num_speakers_detected: int
    num_segments: int
    num_chunks: int | None
    num_items: int | None
    device_peak_mb: float
    torch_peak_allocated_mb: float
    torch_peak_reserved_mb: float
    gpu_name: str
    gpu_total_mb: int
    device_baseline_mb: float = 0.0
    device_delta_mb: float = 0.0
    stage_peaks: list[dict[str, Any]] = field(default_factory=list)
    nvml_samples_count: int = 0
    error: str | None = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        '--file', default='0.5h_1899s.wav', help='Audio file name inside benchmark/test_audio/'
    )
    p.add_argument(
        '--embedding-batch-size',
        type=int,
        default=64,
        help='Value written to pipeline.embedding_batch_size. Values ≤32 will be auto-promoted by the fork.',
    )
    p.add_argument(
        '--mixed-precision',
        choices=['on', 'off'],
        default='off',
        help='Toggle pipeline._mixed_precision (wraps both segmentation and embedding in torch.amp.autocast fp16).',
    )
    p.add_argument(
        '--cap-gb',
        default='unlimited',
        help=(
            'Simulated GPU cap in GB (applied via torch.cuda.set_per_process_memory_fraction). '
            "'unlimited' disables the cap. Only applies when running on GPU 0 A6000."
        ),
    )
    p.add_argument(
        '--repeat-index',
        type=int,
        default=0,
        help='Repeat index for multi-run configs (informational).',
    )
    p.add_argument(
        '--num-speakers',
        type=int,
        default=None,
        help='Optional explicit speaker count passed to the pipeline.',
    )
    p.add_argument('--out', default=str(DEFAULT_OUT_DIR), help='Output directory for per-run JSON.')
    p.add_argument(
        '--smoke',
        action='store_true',
        help='Run the 2-config smoke precheck (0.5h × unlimited × batch=64 × fp32 vs fp16).',
    )
    p.add_argument(
        '--matrix', action='store_true', help='Run the full 96-run trimmed smoke matrix.'
    )
    p.add_argument(
        '--small-batch-sweep',
        action='store_true',
        help='Sweep small embedding batch sizes (1,4,8,16,32) across 2 files × 3 caps × 2 precisions. '
        'Uses PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE to bypass the fork auto-scaler.',
    )
    p.add_argument(
        '--der-sweep',
        action='store_true',
        help='Phase A.3: emit RTTMs for DER. 2 files × bs∈{1,4,8,16,32,64,128} × {fp32,fp16} × unlimited.',
    )
    p.add_argument('--log-level', default='INFO')
    return p.parse_args()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )


def require_container() -> None:
    """Refuse to run outside the celery-worker container.

    Running torch from the host venv against a production GPU caused a driver
    fault on 2026-04-19 (see plan A.0.0). The container's CUDA stack is the
    only one that is safe to use against the shared GPU on this machine.
    """
    if Path('/.dockerenv').exists() or os.environ.get('OPENTRANSCRIBE_IN_CONTAINER') == '1':
        return
    sys.stderr.write(
        '\n'.join(
            [
                'ERROR: vram-probe-diarization.py must run inside the celery-worker container.',
                'Running from host venv against a production GPU wedged the A6000 driver on',
                '2026-04-19 (plan A.0.0). The host and container CUDA runtimes differ and the',
                'driver refuses to reconcile two contexts on the same slot.',
                '',
                'Correct invocation:',
                '  docker compose run --rm --entrypoint "" celery-worker \\',
                '      python /app/scripts/vram-probe-diarization.py --smoke \\',
                '      --out /app/docs/diarization-vram-profile/raw/',
                '',
                'To bypass (ONLY if you really know what you are doing, e.g. running on a',
                'dedicated benchmark host with no other GPU users):',
                '  OPENTRANSCRIBE_IN_CONTAINER=1 python scripts/vram-probe-diarization.py ...',
                '',
            ]
        )
    )
    sys.exit(2)


def apply_cap(cap_gb: str) -> float | None:
    """Apply simulated VRAM cap via PyTorch allocator fraction on device 0.

    Returns the cap in GB, or None if unlimited.
    """
    import torch

    if cap_gb == 'unlimited':
        logger.info('No VRAM cap applied (unlimited)')
        return None
    cap_val = float(cap_gb)
    if not torch.cuda.is_available():
        logger.warning('CUDA unavailable — cap ignored')
        return cap_val
    frac = cap_val / A6000_TOTAL_GB
    if frac <= 0 or frac > 1:
        raise ValueError(f'cap_gb={cap_val} out of range for A6000 ({A6000_TOTAL_GB} GB)')
    torch.cuda.set_per_process_memory_fraction(frac, 0)
    logger.info(f'Applied VRAM cap: {cap_val} GB ({frac:.4f} of A6000 total)')
    return cap_val


class _NvmlMem(ctypes.Structure):
    _fields_ = [
        ('total', ctypes.c_ulonglong),
        ('free', ctypes.c_ulonglong),
        ('used', ctypes.c_ulonglong),
    ]


def _load_nvml() -> Any:
    """Load libnvidia-ml.so.1 via ctypes and init. Returns the library handle or None."""
    try:
        lib = ctypes.CDLL('libnvidia-ml.so.1')
        lib.nvmlInit_v2()
        return lib
    except Exception as e:
        logger.debug(f'NVML ctypes load failed: {e}')
        return None


def _nvml_device_handle(lib: Any, device: int = 0) -> Any:
    handle = ctypes.c_void_p()
    lib.nvmlDeviceGetHandleByIndex(device, ctypes.byref(handle))
    return handle


def get_gpu_info() -> tuple[str, int]:
    """Return (gpu_name, total_vram_mb) via NVML (ctypes), or ('unknown', 0) on failure."""
    try:
        lib = _load_nvml()
        if lib is None:
            raise RuntimeError('libnvidia-ml.so.1 not available')
        handle = _nvml_device_handle(lib, 0)
        name_buf = ctypes.create_string_buffer(96)
        lib.nvmlDeviceGetName(handle, name_buf, ctypes.c_uint(96))
        mem = _NvmlMem()
        lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem))
        return name_buf.value.decode(errors='replace'), int(mem.total / (1024**2))
    except Exception as e:
        logger.warning(f'NVML ctypes unavailable: {e}; falling back to nvidia-smi')
        try:
            out = subprocess.run(
                ['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.splitlines()[0]
            parts = [p.strip() for p in out.split(',')]
            return parts[0], int(parts[1])
        except Exception as e2:
            logger.error(f'nvidia-smi fallback also failed: {e2}')
            return 'unknown', 0


class NVMLSampler:
    """Background thread polling device-used memory at 100 ms cadence (ctypes NVML)."""

    def __init__(self) -> None:
        self._samples: list[tuple[float, int]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0
        self._lib: Any = None
        self._handle: Any = None

    def start(self) -> None:
        self._lib = _load_nvml()
        if self._lib is None:
            logger.warning('NVMLSampler disabled (libnvidia-ml.so.1 not loadable)')
            return
        try:
            self._handle = _nvml_device_handle(self._lib, 0)
        except Exception as e:
            logger.warning(f'NVMLSampler disabled (device handle failed): {e}')
            self._lib = None
            return
        self._t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._run, daemon=True, name='nvml-sampler')
        self._thread.start()

    def _run(self) -> None:
        assert self._lib is not None and self._handle is not None
        mem = _NvmlMem()
        while not self._stop.wait(NVML_SAMPLE_INTERVAL_S):
            try:
                self._lib.nvmlDeviceGetMemoryInfo(self._handle, ctypes.byref(mem))
                self._samples.append((time.perf_counter() - self._t0, int(mem.used)))
            except Exception:
                continue

    def stop(self) -> tuple[list[tuple[float, int]], float]:
        """Stop sampler and return (samples, peak_used_mb)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        peak_used_mb = 0.0
        if self._samples:
            peak_used_mb = max(s[1] for s in self._samples) / (1024**2)
        return self._samples, peak_used_mb


def probe_audio_duration(wav_path: Path) -> float:
    """Return duration in seconds of a WAV file. Uses wave module (pure stdlib)."""
    import wave

    with wave.open(str(wav_path), 'rb') as f:
        return f.getnframes() / f.getframerate()


def load_audio(wav_path: Path) -> dict[str, Any]:
    """Load mono 16 kHz audio as a pyannote-compatible dict.

    Returns {"waveform": torch.Tensor[1, T], "sample_rate": 16000, "uri": name}.
    """
    import wave

    import numpy as np
    import torch

    with wave.open(str(wav_path), 'rb') as wf:
        sr = wf.getframerate()
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
    if sampwidth != 2:
        raise ValueError(f'Expected 16-bit PCM, got sampwidth={sampwidth} for {wav_path.name}')
    if sr != 16000:
        raise ValueError(f'Expected 16kHz audio, got {sr}Hz for {wav_path.name}')
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        data = data.reshape(-1, n_channels).mean(axis=1)
    waveform = torch.from_numpy(np.ascontiguousarray(data)).unsqueeze(0)
    return {'waveform': waveform, 'sample_rate': sr, 'uri': wav_path.stem}


def build_stage_hook(stage_peaks: list[StagePeak], t0: float) -> Any:
    """Return a pipeline hook that captures per-stage PyTorch allocator peaks."""
    import torch

    stage_set = {
        'segmentation',
        'vram_cleanup_post_segmentation',
        'speaker_counting',
        'binarization',
        'embedding_inference_start',
        'embeddings',
        'vram_cleanup_post_embedding',
        'clustering_start',
    }

    def hook(stage_name: str, *_args: Any, **_kwargs: Any) -> None:
        if stage_name not in stage_set:
            return
        if torch.cuda.is_available():
            alloc = torch.cuda.memory_allocated() / (1024**2)
            reserved = torch.cuda.memory_reserved() / (1024**2)
            peak = torch.cuda.max_memory_allocated() / (1024**2)
        else:
            alloc = reserved = peak = 0.0
        stage_peaks.append(
            StagePeak(
                stage=stage_name,
                t_rel_s=time.perf_counter() - t0,
                torch_allocated_mb=alloc,
                torch_reserved_mb=reserved,
                torch_max_allocated_mb=peak,
            )
        )
        # Reset peak so each stage gets an isolated window
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()

    return hook


def run_one(args: argparse.Namespace) -> RunResult:
    """Execute a single probe run and return the result."""
    import torch

    wav_path = AUDIO_DIR / args.file
    if not wav_path.exists():
        raise FileNotFoundError(f'Audio file not found: {wav_path}')

    file_duration_s = probe_audio_duration(wav_path)
    cap_gb_applied = apply_cap(args.cap_gb)
    gpu_name, gpu_total_mb = get_gpu_info()

    logger.info(
        f'Run: file={args.file} cap={args.cap_gb}GB batch={args.embedding_batch_size} '
        f'mp={args.mixed_precision} repeat={args.repeat_index}'
    )

    # Clean slate
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()

    hf_token = os.environ.get('HUGGINGFACE_TOKEN')
    if not hf_token:
        raise RuntimeError('HUGGINGFACE_TOKEN not set')

    from pyannote.audio import Pipeline

    t_start = time.perf_counter()
    t_load_start = time.perf_counter()
    pipeline = Pipeline.from_pretrained(PYANNOTE_V4_MODEL, token=hf_token)
    if pipeline is None:
        raise RuntimeError(f'Pipeline.from_pretrained returned None for {PYANNOTE_V4_MODEL}')
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    pipeline.to(device)
    pipeline.embedding_batch_size = args.embedding_batch_size
    # Force-override the fork's auto-scaler for batch sizes ≤ 32 so the probe
    # measures the exact requested batch (Phase A small-batch characterization).
    if args.embedding_batch_size <= 32:
        os.environ['PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE'] = str(args.embedding_batch_size)
    else:
        os.environ.pop('PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE', None)
    pipeline._mixed_precision = args.mixed_precision == 'on'
    t_load = time.perf_counter() - t_load_start

    audio = load_audio(wav_path)
    stage_peaks: list[StagePeak] = []
    # Capture idle GPU baseline BEFORE sampling: accounts for other processes on
    # the host GPU (desktop compositor, unrelated workloads). Pipeline footprint
    # is device_peak_mb − device_baseline_mb. See user question 2026-04-20.
    _baseline_lib = _load_nvml()
    device_baseline_mb = 0.0
    if _baseline_lib is not None:
        try:
            _bh = _nvml_device_handle(_baseline_lib, 0)
            _bm = _NvmlMem()
            _baseline_lib.nvmlDeviceGetMemoryInfo(_bh, ctypes.byref(_bm))
            device_baseline_mb = _bm.used / (1024**2)
        except Exception as e:
            logger.debug(f'baseline NVML query failed: {e}')
    sampler = NVMLSampler()
    sampler.start()

    t_diarize_start = time.perf_counter()
    err: str | None = None
    annotation = None
    try:
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        hook = build_stage_hook(stage_peaks, t_start)
        kwargs: dict[str, Any] = {'hook': hook}
        if args.num_speakers is not None:
            kwargs['num_speakers'] = args.num_speakers
        output = pipeline(audio, **kwargs)
        # Pipeline returns DiarizeOutput (v4) or Annotation (legacy).
        annotation = getattr(output, 'speaker_diarization', output)
    except Exception as e:  # capture errors per-run instead of aborting the matrix
        err = f'{type(e).__name__}: {e}'
        logger.error(f'Run failed: {err}', exc_info=True)
    t_diarize = time.perf_counter() - t_diarize_start

    # Final torch peak (max over entire diarize call)
    if torch.cuda.is_available():
        torch_peak_alloc = torch.cuda.max_memory_allocated() / (1024**2)
        torch_peak_reserved = torch.cuda.memory_reserved() / (1024**2)
    else:
        torch_peak_alloc = torch_peak_reserved = 0.0

    samples, device_peak_mb = sampler.stop()
    t_total = time.perf_counter() - t_start

    # Derive effective batch and chunk counts from stage hook data if we can
    num_chunks: int | None = None
    num_items: int | None = None
    effective_batch: int | None = None
    # The hook receives "embeddings" with total=batch_count, but our hook signature
    # drops kwargs. We can still back-compute from num_speakers and annotation.

    num_speakers = 0
    num_segments = 0
    if annotation is not None:
        try:
            labels = annotation.labels()
            num_speakers = len(set(labels))
            num_segments = sum(1 for _ in annotation.itertracks())
        except Exception as e:
            logger.warning(f'Could not summarize annotation: {e}')
        # Dump RTTM alongside the JSON result for DER computation (Phase A.3).
        try:
            rttm_dir = Path(args.out) / 'rttm'
            rttm_dir.mkdir(parents=True, exist_ok=True)
            cap_tag = 'unl' if args.cap_gb == 'unlimited' else str(args.cap_gb).replace('.', 'p')
            rttm_path = rttm_dir / (
                f'{Path(args.file).stem}__cap-{cap_tag}__bs-{args.embedding_batch_size}__'
                f'mp-{args.mixed_precision}__r{args.repeat_index}.rttm'
            )
            with open(rttm_path, 'w') as f:
                annotation.write_rttm(f)
        except Exception as e:
            logger.warning(f'Could not write RTTM: {e}')

    # Release the pipeline so the next run starts clean
    del pipeline
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    return RunResult(
        timestamp=datetime.now(UTC).isoformat(),
        file=args.file,
        file_duration_s=round(file_duration_s, 2),
        cap_gb=cap_gb_applied,
        embedding_batch_size_setting=args.embedding_batch_size,
        effective_batch_size_observed=effective_batch,
        mixed_precision=args.mixed_precision,
        repeat_index=args.repeat_index,
        wall_time_s=round(t_total, 3),
        model_load_time_s=round(t_load, 3),
        diarize_time_s=round(t_diarize, 3),
        num_speakers_detected=num_speakers,
        num_segments=num_segments,
        num_chunks=num_chunks,
        num_items=num_items,
        device_peak_mb=round(device_peak_mb, 1),
        device_baseline_mb=round(device_baseline_mb, 1),
        device_delta_mb=round(max(0.0, device_peak_mb - device_baseline_mb), 1),
        torch_peak_allocated_mb=round(torch_peak_alloc, 1),
        torch_peak_reserved_mb=round(torch_peak_reserved, 1),
        gpu_name=gpu_name,
        gpu_total_mb=gpu_total_mb,
        stage_peaks=[asdict(sp) for sp in stage_peaks],
        nvml_samples_count=len(samples),
        error=err,
    )


def write_result(result: RunResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = (
        f'{Path(result.file).stem}__cap-{result.cap_gb or "unl"}__'
        f'bs-{result.embedding_batch_size_setting}__mp-{result.mixed_precision}__'
        f'r{result.repeat_index}.json'
    )
    out_path = out_dir / slug
    with out_path.open('w', encoding='utf-8') as f:
        json.dump(asdict(result), f, indent=2)
    logger.info(f'Wrote {out_path}')
    return out_path


def run_smoke(args: argparse.Namespace) -> int:
    """Two-run precheck: 0.5h × unlimited × batch=64 × fp32 vs fp16."""
    out_dir = Path(args.out)
    configs = [
        {
            'file': '0.5h_1899s.wav',
            'cap_gb': 'unlimited',
            'embedding_batch_size': 64,
            'mixed_precision': 'off',
        },
        {
            'file': '0.5h_1899s.wav',
            'cap_gb': 'unlimited',
            'embedding_batch_size': 64,
            'mixed_precision': 'on',
        },
    ]
    for i, cfg in enumerate(configs):
        sub_args = argparse.Namespace(**{**vars(args), **cfg, 'repeat_index': 0})
        result = run_one(sub_args)
        write_result(result, out_dir)
        if result.error:
            logger.error(f'Smoke run {i + 1}/{len(configs)} failed: {result.error}')
            return 1
        logger.info(
            f'Smoke run {i + 1}/{len(configs)}: wall={result.wall_time_s}s '
            f'device_peak={result.device_peak_mb}MB torch_peak={result.torch_peak_allocated_mb}MB '
            f'speakers={result.num_speakers_detected} segments={result.num_segments}'
        )
    return 0


def run_matrix(args: argparse.Namespace) -> int:
    """Full trimmed 96-run matrix: 2 files × 3 caps × 4 batch × 2 precision × 2 repeats."""
    out_dir = Path(args.out)
    files = ['0.5h_1899s.wav', '2.2h_7998s.wav']
    caps = ['4', '8', 'unlimited']
    batches = [64, 128, 192, 256]
    precisions = ['off', 'on']
    repeats = 2

    total = len(files) * len(caps) * len(batches) * len(precisions) * repeats
    run_idx = 0
    fails = 0
    t_matrix_start = time.perf_counter()
    for file in files:
        for cap in caps:
            for bs in batches:
                for mp in precisions:
                    for r in range(repeats):
                        run_idx += 1
                        logger.info(f'=== Matrix run {run_idx}/{total} ===')
                        sub_args = argparse.Namespace(
                            **{
                                **vars(args),
                                'file': file,
                                'cap_gb': cap,
                                'embedding_batch_size': bs,
                                'mixed_precision': mp,
                                'repeat_index': r,
                            }
                        )
                        result = run_one(sub_args)
                        write_result(result, out_dir)
                        if result.error:
                            fails += 1
                        elapsed = time.perf_counter() - t_matrix_start
                        logger.info(
                            f'Matrix progress: {run_idx}/{total} fails={fails} '
                            f'elapsed={elapsed / 60:.1f}min'
                        )
    logger.info(f'Matrix complete: {run_idx} runs, {fails} failures, elapsed {elapsed / 60:.1f}min')
    return 0 if fails == 0 else 2


def run_small_batch_sweep(args: argparse.Namespace) -> int:
    """Characterize throughput + VRAM at sub-64 batch sizes via env-var force."""
    out_dir = Path(args.out)
    files = ['0.5h_1899s.wav', '2.2h_7998s.wav']
    caps = ['4', '8', 'unlimited']
    batches = [1, 4, 8, 16, 32]
    precisions = ['off', 'on']
    total = len(files) * len(caps) * len(batches) * len(precisions)
    run_idx = 0
    fails = 0
    t_start = time.perf_counter()
    for file in files:
        for cap in caps:
            for bs in batches:
                for mp in precisions:
                    run_idx += 1
                    logger.info(f'=== Small-batch sweep {run_idx}/{total} ===')
                    sub = argparse.Namespace(
                        **{
                            **vars(args),
                            'file': file,
                            'cap_gb': cap,
                            'embedding_batch_size': bs,
                            'mixed_precision': mp,
                            'repeat_index': 0,
                        }
                    )
                    result = run_one(sub)
                    write_result(result, out_dir)
                    if result.error:
                        fails += 1
                    elapsed = time.perf_counter() - t_start
                    logger.info(
                        f'Sweep progress: {run_idx}/{total} fails={fails} '
                        f'elapsed={elapsed / 60:.1f}min'
                    )
    logger.info(f'Sweep complete: {run_idx} runs, {fails} failures, {elapsed / 60:.1f}min')
    return 0 if fails == 0 else 2


def run_der_sweep(args: argparse.Namespace) -> int:
    """Phase A.3: emit RTTMs for DER computation.

    2 files × bs ∈ {1, 4, 8, 16, 32, 64, 128} × fp32/fp16 × unlimited cap.
    = 28 runs. Reuses existing RTTMs by filename; will rewrite if present.
    """
    out_dir = Path(args.out)
    files = ['0.5h_1899s.wav', '2.2h_7998s.wav']
    batches = [1, 4, 8, 16, 32, 64, 128]
    precisions = ['off', 'on']
    total = len(files) * len(batches) * len(precisions)
    run_idx = 0
    fails = 0
    t_start = time.perf_counter()
    for file in files:
        for bs in batches:
            for mp in precisions:
                run_idx += 1
                logger.info(f'=== DER sweep {run_idx}/{total} ===')
                sub = argparse.Namespace(
                    **{
                        **vars(args),
                        'file': file,
                        'cap_gb': 'unlimited',
                        'embedding_batch_size': bs,
                        'mixed_precision': mp,
                        'repeat_index': 0,
                    }
                )
                result = run_one(sub)
                write_result(result, out_dir)
                if result.error:
                    fails += 1
                elapsed = time.perf_counter() - t_start
                logger.info(
                    f'DER sweep progress: {run_idx}/{total} fails={fails} '
                    f'elapsed={elapsed / 60:.1f}min'
                )
    logger.info(f'DER sweep complete: {run_idx} runs, {fails} failures, {elapsed / 60:.1f}min')
    return 0 if fails == 0 else 2


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    require_container()
    if args.smoke:
        return run_smoke(args)
    if args.matrix:
        return run_matrix(args)
    if args.small_batch_sweep:
        return run_small_batch_sweep(args)
    if args.der_sweep:
        return run_der_sweep(args)
    result = run_one(args)
    write_result(result, Path(args.out))
    return 0 if result.error is None else 1


if __name__ == '__main__':
    sys.exit(main())
