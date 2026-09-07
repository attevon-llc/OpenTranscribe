"""Bounded, offline-aware loading of HuggingFace Hub model weights.

Every ``from_pretrained``/model-construction call in this codebase makes a Hub
metadata round trip before falling back to a local cache, and that round trip
carries no wall-clock bound of its own. On a degraded or blocked network path
(observed: DNS resolution to huggingface.co retrying for ~23-30s per cycle) it
sits doing nothing for the rest of the caller's budget — indistinguishable from
a hung process until something else (a Celery hard time limit, a request
timeout) kills it.

Two independent defenses, because neither alone is sufficient:

1. ``hf_offline_requested()`` + an explicit ``local_files_only=True`` kwarg at
   each call site. Setting ``HF_HUB_OFFLINE=1`` in the environment is NOT
   enough by itself: ``huggingface_hub.constants.HF_HUB_OFFLINE`` is computed
   ONCE, at the first time that module is imported anywhere in the process —
   which typically happens well before any of our loaders run, as a side
   effect of importing ``torch``/``transformers``. A caller checking
   ``os.getenv("HF_HUB_OFFLINE")`` at call time and threading the answer
   through an explicit keyword argument sidesteps that import-order trap.
2. ``load_with_timeout()`` bounds the load regardless of the offline flag —
   the dev default ships ``HF_HUB_OFFLINE=0`` (``.env.example``), so a stall is
   the norm to defend against, not the exception.

Pattern lifted from ``app/tasks/speaker_attribute_task.py``'s
``_load_models_with_timeout``, which predates this module and is now a thin
wrapper around it.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

logger = logging.getLogger(__name__)


def hf_offline_requested() -> bool:
    """Whether the operator has asserted the model is already cached locally.

    Mirrors ``huggingface_hub``'s own env-var contract (``HF_HUB_OFFLINE=1``)
    without depending on when that library was first imported in this process
    — see the module docstring.
    """
    return os.getenv("HF_HUB_OFFLINE") == "1"


@contextlib.contextmanager
def force_offline_if_requested() -> Iterator[None]:
    """Make ``huggingface_hub`` actually honor ``HF_HUB_OFFLINE=1`` for this call.

    For loaders with no ``local_files_only`` kwarg of their own (``pyannote.audio``'s
    ``Pipeline.from_pretrained`` is the motivating case), this is the only lever
    available: it mutates ``huggingface_hub.constants.HF_HUB_OFFLINE`` directly and
    drops the module's cached per-thread HTTP sessions so a NEW session — built the
    next time one is requested — mounts the library's own ``OfflineAdapter``, which
    raises instantly instead of attempting a request. Restores both on exit.

    A no-op when ``HF_HUB_OFFLINE`` is not set to ``"1"`` — callers should still wrap
    the load in :func:`load_with_timeout` for the (default) online case.

    ⚠️ This is process-wide, not thread-local: while active, any OTHER thread's Hub
    call also goes offline. That is intentional and safe here — the whole point of
    an operator setting ``HF_HUB_OFFLINE=1`` is that the deployment is offline, so
    making that true for the duration of a load (rather than only for THIS call)
    matches the declared intent rather than fighting it. Use only around a load
    call, never as a long-lived toggle.
    """
    if not hf_offline_requested():
        yield
        return

    import huggingface_hub.constants as hf_constants
    from huggingface_hub.utils import reset_sessions

    previous = hf_constants.HF_HUB_OFFLINE
    hf_constants.HF_HUB_OFFLINE = True
    reset_sessions()
    try:
        yield
    finally:
        hf_constants.HF_HUB_OFFLINE = previous
        reset_sessions()


def load_with_timeout[T](loader: Callable[[], T], *, timeout: float, label: str) -> T:
    """Run ``loader`` in a throwaway thread with a hard wall-clock budget.

    Args:
        loader: Zero-arg callable performing the (potentially blocking) load.
        timeout: Wall-clock budget in seconds.
        label: Human-readable name of the thing being loaded, used only in the
            timeout error message.

    Returns:
        Whatever ``loader`` returns.

    Raises:
        TimeoutError: ``loader`` did not complete within ``timeout``.
        Exception: Whatever ``loader`` itself raised.
    """
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hf-model-load")
    try:
        future = pool.submit(loader)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            raise TimeoutError(
                f"{label} did not complete within {timeout}s (possibly a stalled "
                "HuggingFace Hub network call — set HF_HUB_OFFLINE=1 if the model "
                "is already cached locally)"
            ) from exc
    finally:
        # Not `wait=True`: a wedged from_pretrained() call would otherwise block
        # shutdown forever too. The loader thread dies daemon; the model may
        # finish loading in the background, but the caller has already failed
        # loudly by then.
        pool.shutdown(wait=False, cancel_futures=True)
