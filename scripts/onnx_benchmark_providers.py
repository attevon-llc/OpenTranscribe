#!/usr/bin/env python3
"""Benchmark ORT providers (CPU, CUDA, TensorRT) on the Phase 6.2 ONNX artifacts.

Measures per-batch wall time for segmentation + embedding graphs across each
ORT provider available in the current environment. Used during Phase 6.2
integration to quantify the "Memcpy-inserted" slowdown on CUDA EP when ops
without CUDA kernels (LSTM, If, Sin/Cos) fall back to CPU.

Run inside the backend Docker image with the fork + models/onnx mounted:

    docker run --rm --gpus '"device=0"' --entrypoint "" \\
        --env-file .env \\
        -v /mnt/nvm/repos/transcribe-app/models/onnx:/onnx:ro \\
        -v /mnt/nvm/repos/transcribe-app/scripts:/app/scripts:ro \\
        opentranscribe-backend:latest \\
        python /app/scripts/onnx_benchmark_providers.py --models-dir /onnx
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import numpy as np

# IMPORTANT: torch must be imported BEFORE onnxruntime on Linux. PyTorch's
# import adds its bundled CUDA libraries (libcublas.so.12, libcublasLt.so.12,
# libcudart.so.12, ...) to the dynamic loader's search path. ONNX Runtime's
# CUDAExecutionProvider + TensorrtExecutionProvider both dlopen those same
# libraries. Without torch-first, ORT silently falls back to CPU EP.
import torch  # noqa: E402,F401


def _run(sess, feeds, iters: int = 10) -> float:
    """Return mean ms/call across ``iters`` calls after a 3-call warmup."""
    for _ in range(3):
        sess.run(None, feeds)
    t0 = time.perf_counter()
    for _ in range(iters):
        sess.run(None, feeds)
    return (time.perf_counter() - t0) * 1000.0 / iters


def bench_segmentation(onnx_path: Path, providers: list, tag: str) -> float | None:
    import onnxruntime as ort

    try:
        sess = ort.InferenceSession(str(onnx_path), providers=providers)
    except Exception as exc:
        print(f'  [{tag}] session init FAILED: {exc}')
        return None
    actual = sess.get_providers()
    x = np.random.randn(32, 1, 80000).astype(np.float32)
    # First call may include TRT build — time it separately
    t0 = time.perf_counter()
    _ = sess.run(None, {'input_values': x})
    first_ms = (time.perf_counter() - t0) * 1000.0
    mean_ms = _run(sess, {'input_values': x})
    print(
        f'  [{tag}] actual={actual[0]}  first={first_ms:.0f} ms  '
        f'warm={mean_ms:.1f} ms/batch (batch=32 × 5s)'
    )
    return mean_ms


def bench_embedding(onnx_path: Path, providers: list, tag: str) -> float | None:
    import onnxruntime as ort

    try:
        sess = ort.InferenceSession(str(onnx_path), providers=providers)
    except Exception as exc:
        print(f'  [{tag}] session init FAILED: {exc}')
        return None
    actual = sess.get_providers()
    fbank = np.random.randn(16, 200, 80).astype(np.float32)
    weights = np.ones((16, 200), dtype=np.float32)
    mean_ms = _run(sess, {'fbank_features': fbank, 'weights': weights})
    print(f'  [{tag}] actual={actual[0]}  warm={mean_ms:.1f} ms/batch (batch=16)')
    return mean_ms


def bench_eager(hf_token: str, device: str) -> dict:
    """PyTorch eager baseline on same shapes."""
    from pyannote.audio import Model

    out = {}
    dev = torch.device(device)
    seg = Model.from_pretrained('pyannote/segmentation-3.0', token=hf_token).eval().to(dev)
    x = torch.randn(32, 1, 80000, device=dev)
    with torch.no_grad():
        for _ in range(3):
            seg(x)
    if dev.type == 'cuda':
        torch.cuda.synchronize()
    t0 = time.perf_counter()
    with torch.no_grad():
        for _ in range(10):
            seg(x)
    if dev.type == 'cuda':
        torch.cuda.synchronize()
    out['segmentation'] = (time.perf_counter() - t0) * 100.0
    print(f'  PyTorch eager {device}: seg={out["segmentation"]:.1f} ms/batch')

    emb = (
        Model.from_pretrained(
            'pyannote/wespeaker-voxceleb-resnet34-LM',
            token=hf_token,
        )
        .eval()
        .to(dev)
    )
    fbank = torch.randn(16, 200, 80, device=dev)
    weights = torch.ones(16, 200, device=dev)
    with torch.no_grad():
        for _ in range(3):
            emb.resnet(fbank, weights=weights)
    if dev.type == 'cuda':
        torch.cuda.synchronize()
    t0 = time.perf_counter()
    with torch.no_grad():
        for _ in range(10):
            emb.resnet(fbank, weights=weights)
    if dev.type == 'cuda':
        torch.cuda.synchronize()
    out['embedding'] = (time.perf_counter() - t0) * 100.0
    print(f'  PyTorch eager {device}: emb={out["embedding"]:.1f} ms/batch')
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        '--models-dir',
        type=Path,
        required=True,
        help='Directory containing segmentation.onnx + embedding.onnx',
    )
    ap.add_argument(
        '--skip-eager',
        action='store_true',
        help='Skip the PyTorch eager baseline (needs HF_TOKEN).',
    )
    ap.add_argument('--trt-cache', default='/tmp/trt_cache')
    args = ap.parse_args()

    seg_path = args.models_dir / 'segmentation.onnx'
    emb_path = args.models_dir / 'embedding.onnx'
    assert seg_path.exists() and emb_path.exists(), f'artifacts missing in {args.models_dir}'

    # CPU EP
    print('\n== Segmentation (batch=32 × 5s chunks × 16 kHz) ==')
    bench_segmentation(seg_path, ['CPUExecutionProvider'], 'CPU EP')
    bench_segmentation(
        seg_path,
        [
            ('CUDAExecutionProvider', {'device_id': 0}),
            'CPUExecutionProvider',
        ],
        'CUDA EP',
    )
    Path(args.trt_cache).mkdir(parents=True, exist_ok=True)
    bench_segmentation(
        seg_path,
        [
            (
                'TensorrtExecutionProvider',
                {
                    'device_id': 0,
                    'trt_engine_cache_enable': True,
                    'trt_engine_cache_path': args.trt_cache,
                    'trt_fp16_enable': False,
                },
            ),
            ('CUDAExecutionProvider', {'device_id': 0}),
            'CPUExecutionProvider',
        ],
        'TRT EP',
    )

    print('\n== Embedding backbone (batch=16 × 200 frames × 80 mels) ==')
    bench_embedding(emb_path, ['CPUExecutionProvider'], 'CPU EP')
    bench_embedding(
        emb_path,
        [
            ('CUDAExecutionProvider', {'device_id': 0}),
            'CPUExecutionProvider',
        ],
        'CUDA EP',
    )
    bench_embedding(
        emb_path,
        [
            (
                'TensorrtExecutionProvider',
                {
                    'device_id': 0,
                    'trt_engine_cache_enable': True,
                    'trt_engine_cache_path': args.trt_cache,
                    'trt_fp16_enable': False,
                },
            ),
            ('CUDAExecutionProvider', {'device_id': 0}),
            'CPUExecutionProvider',
        ],
        'TRT EP',
    )

    if not args.skip_eager:
        tok = os.environ.get('HF_TOKEN') or os.environ.get('HUGGINGFACE_TOKEN')
        if not tok:
            print('\nSkipping eager baseline — HF_TOKEN / HUGGINGFACE_TOKEN unset')
        else:
            print('\n== PyTorch eager baseline ==')
            bench_eager(tok, 'cuda' if __import__('torch').cuda.is_available() else 'cpu')


if __name__ == '__main__':
    main()
