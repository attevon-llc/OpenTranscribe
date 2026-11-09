"""WhisperX batch_size vs VRAM diagnostic — Phase B study.

Sweeps batch_size across a range for a given model and audio file, recording
peak VRAM, stable VRAM, wall time, and real-time factor (RTF) at each setting.

VRAM is measured via NVML (libnvidia-ml) which captures true device-level
allocations including CTranslate2 — torch.cuda.memory_allocated() misses
these because faster-whisper uses its own CUDA allocator.

Mirrors the diarization Phase A study methodology.  Results inform the
WHISPER_VRAM_BUDGET_MB table in hardware_detection.py.

Run inside the celery-worker container (has GPU + loaded torch):

    docker exec -it opentranscribe-celery-worker \\
        python -m app.scripts.whisper_batch_diag \\
            --audio /app/benchmark/test_audio/2.2h_7998s.wav \\
            --model large-v3-turbo \\
            --batch-sizes 2,4,8,12,16,24,32 \\
            --output /tmp/whisper_batch_a6000.csv

Save results to docs/whisper-vram-profile/ after each GPU run.
"""

from __future__ import annotations

import argparse
import csv
import gc
import logging
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

FIELDS = [
    "model",
    "compute_type",
    "batch_size",
    "audio_file",
    "audio_duration_s",
    "gpu_name",
    "gpu_total_vram_mb",
    "vram_baseline_mb",
    "vram_stable_before_mb",
    "vram_peak_mb",
    "vram_stable_after_mb",
    "wall_time_s",
    "rtf",
    "status",
    "note",
]


class _PeakVramPoller:
    """Background thread that polls NVML VRAM at 100 ms intervals."""

    def __init__(self) -> None:
        self._peak: float = 0.0
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        from app.utils.nvml_monitor import get_used_mb

        self._peak = get_used_mb()
        self._running = True
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def _poll(self) -> None:
        from app.utils.nvml_monitor import get_used_mb

        while self._running:
            used = get_used_mb()
            if used > self._peak:
                self._peak = used
            time.sleep(0.1)

    def stop(self) -> float:
        """Stop polling and return peak VRAM in MB."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        return self._peak


def run_sweep(
    audio_path: Path,
    model_name: str,
    batch_sizes: list[int],
    compute_type: str,
    output_csv: Path,
) -> None:
    import numpy as np
    import torch

    if not torch.cuda.is_available():
        print("ERROR: CUDA not available — run inside the GPU worker container.", file=sys.stderr)
        sys.exit(1)

    from app.utils.nvml_monitor import get_gpu_memory
    from app.utils.nvml_monitor import get_used_mb

    gpu_name = torch.cuda.get_device_name(0)
    gpu_info = get_gpu_memory()
    gpu_total_mb = (
        gpu_info.total_mb
        if gpu_info
        else torch.cuda.get_device_properties(0).total_memory / 1024**2
    )
    print(f"GPU : {gpu_name}  ({gpu_total_mb:.0f} MB total NVML)")
    print(f"Model: {model_name}  compute_type: {compute_type}")
    print(f"Audio: {audio_path.name}")

    # Load audio once — 16 kHz mono numpy array
    from app.transcription.audio import load_audio

    audio: np.ndarray = load_audio(str(audio_path))
    audio_duration = len(audio) / 16000.0
    print(f"Duration: {audio_duration:.1f}s  ({audio_duration / 3600:.2f}h)")

    # Baseline VRAM before any model is loaded
    gc.collect()
    baseline_mb = get_used_mb()
    print(f"Baseline VRAM (NVML, no model): {baseline_mb:.0f} MB")
    print()
    print(
        f"{'batch':>6}  {'stable_before MB':>16}  {'peak MB':>9}  "
        f"{'stable_after MB':>15}  {'wall s':>8}  {'RTF':>7}  status"
    )
    print("-" * 80)

    rows: list[dict] = []

    for bs in batch_sizes:
        gc.collect()

        from app.transcription.config import TranscriptionConfig
        from app.transcription.transcriber import Transcriber

        cfg = TranscriptionConfig(
            model_name=model_name,
            compute_type=compute_type,
            batch_size=bs,
            beam_size=5,
            device="cuda",
            device_index=0,
        )

        transcriber = Transcriber(cfg)
        transcriber.load_model()

        # Settle after model load, then record stable-before
        time.sleep(0.5)
        stable_before = get_used_mb()

        # Start NVML poller, run transcription, stop and get peak
        poller = _PeakVramPoller()
        poller.start()

        t0 = time.perf_counter()
        status = "ok"
        note = ""
        peak_mb = stable_after = rtf = wall_time = None
        try:
            transcriber.transcribe(audio)
            wall_time = round(time.perf_counter() - t0, 2)
            peak_mb = round(poller.stop(), 1)
            time.sleep(0.3)
            stable_after = round(get_used_mb(), 1)
            rtf = round(wall_time / audio_duration, 4)
        except torch.cuda.OutOfMemoryError as exc:
            poller.stop()
            status = "OOM"
            note = str(exc)[:120]
        except Exception as exc:
            poller.stop()
            status = "ERROR"
            note = str(exc)[:120]
            logger.error("batch size %s failed: %s", bs, exc, exc_info=True)

        print(
            f"{bs:>6}  {stable_before:>16.0f}  {peak_mb or 0:>9.0f}  "
            f"{stable_after or 0:>15.0f}  {wall_time or 0:>8.1f}  "
            f"{rtf or 0:>7.3f}  {status}"
        )

        rows.append(
            {
                "model": model_name,
                "compute_type": compute_type,
                "batch_size": bs,
                "audio_file": audio_path.name,
                "audio_duration_s": round(audio_duration, 1),
                "gpu_name": gpu_name,
                "gpu_total_vram_mb": round(gpu_total_mb, 0),
                "vram_baseline_mb": baseline_mb,
                "vram_stable_before_mb": stable_before,
                "vram_peak_mb": peak_mb,
                "vram_stable_after_mb": stable_after,
                "wall_time_s": wall_time,
                "rtf": rtf,
                "status": status,
                "note": note,
            }
        )

        del transcriber
        gc.collect()
        time.sleep(1.0)  # let CTranslate2 release VRAM before next run

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nSaved: {output_csv}")
    print()
    _print_summary(rows, gpu_total_mb)


def _print_summary(rows: list[dict], gpu_total_mb: float) -> None:
    print("=== VRAM budget headroom (80% rule: peak <= 0.80 x total) ===")
    print(f"{'batch':>6}  {'peak MB':>9}  {'80% threshold MB':>17}  {'safe?':>6}  {'RTF':>7}")
    threshold = gpu_total_mb * 0.80
    for r in rows:
        if r["status"] != "ok":
            print(f"{r['batch_size']:>6}  {'OOM/ERR':>9}  {threshold:>17.0f}  {'NO':>6}  —")
            continue
        safe = "YES" if r["vram_peak_mb"] <= threshold else "NO "
        print(
            f"{r['batch_size']:>6}  {r['vram_peak_mb']:>9.0f}  {threshold:>17.0f}  "
            f"{safe:>6}  {r['rtf']:>7.3f}"
        )


def main() -> None:
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(description="WhisperX batch_size vs VRAM diagnostic")
    parser.add_argument("--audio", required=True, help="Path to WAV file (16 kHz preferred)")
    parser.add_argument("--model", default="large-v3-turbo", help="Whisper model name")
    parser.add_argument(
        "--batch-sizes",
        default="2,4,8,12,16,24,32",
        help="Comma-separated batch sizes to test (default: 2,4,8,12,16,24,32)",
    )
    parser.add_argument(
        "--compute-type",
        default="int8_float16",
        help="CTranslate2 compute type (default: int8_float16)",
    )
    parser.add_argument("--output", default="/tmp/whisper_batch_results.csv", help="Output CSV")  # noqa: S108 - default scratch path for a diagnostic tool  # nosec B108
    args = parser.parse_args()

    audio_path = Path(args.audio)
    if not audio_path.exists():
        print(f"ERROR: audio file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    batch_sizes = [int(b.strip()) for b in args.batch_sizes.split(",")]

    run_sweep(
        audio_path=audio_path,
        model_name=args.model,
        batch_sizes=batch_sizes,
        compute_type=args.compute_type,
        output_csv=Path(args.output),
    )


if __name__ == "__main__":
    main()
