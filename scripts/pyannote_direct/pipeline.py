"""Resolve the device and load the stock or optimized PyAnnote pipeline."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from pyannote_direct.config import DEFAULT_MODEL, DEFAULT_OPTIMIZED_SRC, HF_TOKEN, logger


def resolve_device_str(device: str, gpu_index: int) -> str:
    if device == 'mps':
        return 'mps'
    if device == 'cuda':
        return f'cuda:{gpu_index}'
    return device


def load_pipeline(
    variant: str,
    device: str = 'cuda',
    gpu_index: int = 0,
    onnx_cpu: bool = False,
    model_id: str = DEFAULT_MODEL,
    optimized_src: Path | None = None,
    torch_compile: bool = False,
    segmentation_step: float | None = None,
):
    """Load a PyAnnote pipeline for the given variant.

    Parameters
    ----------
    torch_compile : bool
        If True, wrap the segmentation and embedding models in torch.compile()
        after pipeline construction. Measures the Phase 2.1 visibility fix
        impact without touching the pipeline's __init__ default.
    segmentation_step : float | None
        If not None, override the pipeline's segmentation sliding-window step
        (default is typically 0.1 = 90% overlap). Higher values (0.2-0.3) make
        segmentation run on fewer chunks at a DER cost — measured in Phase 4.
    """
    import torch

    if variant in ('optimized', 'optimized_cpu'):
        opt_src = str(optimized_src or DEFAULT_OPTIMIZED_SRC)
        if opt_src and Path(opt_src).exists() and opt_src not in sys.path:
            sys.path.insert(0, opt_src)
            # Force reimport from optimized path so editable changes are picked up
            for mod in list(sys.modules.keys()):
                if 'pyannote' in mod:
                    del sys.modules[mod]

    from pyannote.audio import Pipeline

    device_str = resolve_device_str(device, gpu_index)

    print(f'Loading PyAnnote pipeline ({variant}) on {device_str}: model={model_id}')
    t0 = time.perf_counter()
    try:
        pipeline = Pipeline.from_pretrained(
            model_id,
            use_auth_token=HF_TOKEN or None,
        )
    except TypeError:
        pipeline = Pipeline.from_pretrained(
            model_id,
            token=HF_TOKEN or None,
        )
    pipeline.to(torch.device(device_str))

    # Phase 4: sliding-window step override. pyannote's Inference object
    # stores the step on the underlying segmentation inference; the
    # SlidingWindow is rebuilt per-call so this is the simplest hook.
    if segmentation_step is not None and hasattr(pipeline, '_segmentation'):
        inner = pipeline._segmentation
        if hasattr(inner, 'step'):
            old_step = inner.step
            inner.step = float(segmentation_step)
            print(f'  segmentation_step: {old_step} -> {inner.step}')

    # Phase 2.1 follow-up: post-construction torch.compile. Bypasses the
    # pipeline's __init__ kwarg (which defaults False and isn't exposed by
    # Pipeline.from_pretrained) by compiling the resident models directly.
    if torch_compile and device_str != 'cpu':
        try:
            if hasattr(pipeline, '_segmentation') and hasattr(pipeline._segmentation, 'model'):
                pipeline._segmentation.model = torch.compile(
                    pipeline._segmentation.model, fullgraph=False
                )
            klustering = getattr(pipeline, 'klustering', None)
            if (
                hasattr(pipeline, '_embedding')
                and hasattr(pipeline._embedding, 'model_')
                and klustering != 'OracleClustering'
            ):
                pipeline._embedding.model_ = torch.compile(
                    pipeline._embedding.model_, mode='reduce-overhead'
                )
            print('  torch.compile enabled (segmentation + embedding)')
        except (RuntimeError, AttributeError, TypeError) as e:
            logger.warning('torch.compile failed', exc_info=True)
            print(f'  torch.compile failed: {e}')

    # Embedding batch size is now pinned to 16 by the fork. Override only if the
    # user explicitly set PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE (the fork respects it).
    if variant in ('optimized', 'optimized_cpu') and device_str != 'cpu':
        forced = os.environ.get('PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE')
        if forced and hasattr(pipeline, 'embedding_batch_size'):
            try:
                old_bs = pipeline.embedding_batch_size
                new_bs = int(forced)
                pipeline.embedding_batch_size = new_bs
                print(
                    f'  Embedding batch size: {old_bs} -> {new_bs} (PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE)'
                )
            except ValueError:
                logger.warning(
                    'ignoring non-integer PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE=%r',
                    forced,
                    exc_info=True,
                )

    if onnx_cpu and hasattr(pipeline, '_setup_onnx_cpu'):
        print('  Enabling ONNX CPU inference (quantized INT8)...')
        pipeline._setup_onnx_cpu(quantize=True, num_threads=0)
        if getattr(pipeline, '_onnx_cpu', False):
            print('  ONNX CPU mode active')
        else:
            print('  WARNING: ONNX setup failed, falling back to PyTorch CPU')

    load_time = time.perf_counter() - t0
    print(f'  Pipeline loaded in {load_time:.1f}s on {device_str}')

    mod_file = sys.modules.get('pyannote.audio.pipelines.speaker_diarization', None)
    if mod_file:
        print(f'  Module: {getattr(mod_file, "__file__", "unknown")}')

    return pipeline, load_time
