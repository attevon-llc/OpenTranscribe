"""Measure the VRAM and host-RAM footprint of each GPU stage on this machine (issue #1081).

The admission budget in ``app/transcription/vram_budget.py`` reserves a per-stage estimate
above the resident models. This probe prints the numbers those estimates come from, so an
operator can tune ``GPU_STAGE_VRAM_MB_<STAGE>`` for their card and model instead of guessing.

Run ONE stage per process: CTranslate2 and torch both cache freed device memory, so a second
stage in the same process would measure the first one's cache rather than its own peak::

    python -m app.scripts.gpu_stage_vram_probe --audio /path/meeting.wav --stage models
    python -m app.scripts.gpu_stage_vram_probe --audio /path/meeting.wav --stage asr --batch-size 16
    python -m app.scripts.gpu_stage_vram_probe --audio /path/meeting.wav --stage diarization

Prints one JSON line. VRAM figures are DEVICE-WIDE (``cudaMemGetInfo`` sampled every 20 ms),
so run it on an otherwise idle card, or subtract what else is resident (``used_at_start_mb``).
Host figures are this process's RSS; ``host_peak_mb`` is the high-water mark during the stage.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time


def _rss_mb() -> float:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def _hwm_mb() -> float:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    return 0.0


def _reset_hwm() -> None:
    try:
        with open("/proc/self/clear_refs", "w") as fh:
            fh.write("5")
    except OSError:
        pass


class _DeviceSampler:
    """Track the minimum free device memory while a stage runs."""

    def __init__(self, torch_mod, index: int, interval_s: float = 0.02):
        self._torch = torch_mod
        self._index = index
        self._interval = interval_s
        self._stop = threading.Event()
        self.min_free_b: int | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            free_b, _ = self._torch.cuda.mem_get_info(self._index)
            if self.min_free_b is None or free_b < self.min_free_b:
                self.min_free_b = free_b
            time.sleep(self._interval)

    def __enter__(self) -> _DeviceSampler:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--audio", required=True)
    parser.add_argument("--stage", choices=("models", "asr", "diarization"), required=True)
    parser.add_argument("--batch-size", type=int, default=None)
    args = parser.parse_args()

    # Measure the stage itself: no admission waits, and the in-process diarizer (the
    # sidecar's memory lives in another process and is not this worker's to budget).
    os.environ["GPU_VRAM_ADMISSION"] = "false"
    os.environ.setdefault("ENGINE_DIARIZER_BACKEND", "pyannote")
    os.environ["GPU_OOM_MAX_HALVINGS"] = "0"

    import torch

    from app.transcription.audio import load_audio
    from app.transcription.config import TranscriptionConfig

    index = 0
    mib = 1024**2
    free_b, total_b = torch.cuda.mem_get_info(index)
    used_at_start = (total_b - free_b) / mib

    overrides = {}
    if args.batch_size:
        overrides["batch_size"] = args.batch_size
    tc = TranscriptionConfig.from_environment(**overrides)

    from app.transcription.diarizer import SpeakerDiarizer
    from app.transcription.transcriber import Transcriber

    transcriber = Transcriber(tc)
    transcriber.load_model()
    diarizer = SpeakerDiarizer(tc)
    diarizer.load_model()
    torch.cuda.synchronize()
    torch.cuda.empty_cache()
    free_b, _ = torch.cuda.mem_get_info(index)
    used_models = (total_b - free_b) / mib

    # Host baseline BEFORE the decode: the whole-file float32 array is part of what one task
    # costs in RAM (issue #1073), so it belongs in the delta.
    host_before = _rss_mb()
    _reset_hwm()
    audio = load_audio(args.audio)
    audio_s = len(audio) / 16000

    result: dict = {
        "gpu": torch.cuda.get_device_name(index),
        "model": tc.model_name,
        "compute_type": tc.compute_type,
        "concurrent_requests": tc.concurrent_requests,
        "total_mb": round(total_b / mib),
        "used_at_start_mb": round(used_at_start),
        "models_resident_mb": round(used_models - used_at_start),
        "audio_s": round(audio_s, 1),
        "stage": args.stage,
    }

    started = time.perf_counter()
    if args.stage != "models":
        with _DeviceSampler(torch, index) as sampler:
            if args.stage == "asr":
                transcriber.transcribe(audio)
                result["batch_size"] = tc.batch_size
            else:
                diarizer.diarize(audio)
            torch.cuda.synchronize()
        peak_used = (total_b - (sampler.min_free_b or free_b)) / mib
        result["stage_peak_above_models_mb"] = round(peak_used - used_models)
        result["elapsed_s"] = round(time.perf_counter() - started, 1)

    result["host_rss_models_mb"] = round(host_before)
    result["host_peak_mb"] = round(max(_hwm_mb(), _rss_mb()))
    result["host_stage_delta_mb"] = round(result["host_peak_mb"] - host_before)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
