"""PyTorch compatibility patches the downloader applies before any ML library import."""


def guard_mps_empty_cache(torch_module):
    """Make ``torch.mps.empty_cache()`` a no-op when MPS is not available (issue #1003).

    The pinned pyannote fork's ``SpeakerDiarization._gpu_empty_cache()`` falls through to
    ``torch.mps.empty_cache()`` whenever CUDA is unavailable, checking only that the
    attribute exists. On a CPU-only Linux host (e.g. a ``docker build`` stage baking
    models) that call raises ``RuntimeError: Cannot execute emptyCache() without MPS
    backend`` and aborts the diarization download. With no MPS backend there is no MPS
    cache to release, so a no-op is exactly the correct behaviour; on a host that does
    have MPS nothing is touched.

    Args:
        torch_module: The imported ``torch`` module.

    Returns:
        True if ``empty_cache`` was replaced, False if it was left alone.
    """
    mps = getattr(torch_module, 'mps', None)
    if mps is None or not hasattr(mps, 'empty_cache'):
        return False
    backends_mps = getattr(torch_module.backends, 'mps', None)
    if backends_mps is not None and backends_mps.is_available():
        return False
    mps.empty_cache = lambda: None
    return True


def apply_torch_patches():
    """Default ``torch.load`` to ``weights_only=False`` and guard the MPS cache call.

    PyTorch 2.6+ compatibility fix - MUST run BEFORE any ML library imports. Patches
    ``torch.load`` for trusted HuggingFace models; mirrors the fix in
    ``backend/app/core/celery.py``. A no-op when torch is not installed yet.
    """
    try:
        import torch
    except ImportError:
        # torch not available yet, will be imported later
        return

    original_torch_load = torch.load

    def patched_torch_load(*args, **kwargs):
        # Handle both missing weights_only AND weights_only=None (which PyTorch 2.8 treats as True)
        if kwargs.get('weights_only') is None:
            kwargs['weights_only'] = False
        return original_torch_load(*args, **kwargs)

    torch.load = patched_torch_load
    print('ℹ️  PyTorch 2.6+ compatibility: Patched torch.load for weights_only=False')
    if guard_mps_empty_cache(torch):
        print('ℹ️  No MPS backend: torch.mps.empty_cache() made a no-op for this run')
