"""Test-only resets and observers of process-wide singletons and caches in ``app``.

Production never resets these (each is built once per process, or self-expires), so the reset
lives with the tests that need isolation between cases instead of as an ``app`` function that
only tests call. Each function restores exactly the state its former ``app`` helper did.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.transcription.host_memory_admission import SlotGate
    from app.transcription.vram_budget import VramBudget


def reset_resolvers() -> None:
    """Restore the community tenant-limit resolvers."""
    from app.core import tenant_limits as tl

    tl._retention_resolver = tl._community_retention_resolver
    tl._min_retention_resolver = tl._community_min_retention_resolver
    tl._upload_limits_resolver = tl._community_upload_limits_resolver
    tl._chat_limits_resolver = tl._community_chat_limits_resolver
    tl._allowed_models_resolver = tl._community_allowed_models_resolver
    tl._redaction_floor_resolver = tl._community_redaction_floor_resolver


def reset_tenancy_mode_cache() -> None:
    """Forget the cached tenancy-mode detection."""
    from app.services import platform_access as pa

    with pa._lock:
        pa._multi_latched = False
        pa._single_until = 0.0


def reset_readiness_cache() -> None:
    """Clear the diarization sidecar ``sidecar_ready()`` TTL cache and status cache."""
    from app.transcription import diar_sidecar_status as ds

    with ds._ready_cache_lock:
        ds._ready_cache.clear()
        ds._status_cache.clear()


def reset_readiness_state() -> None:
    """Forget the health probe's cached snapshot and the schema result."""
    from app.api import health
    from app.db import probe_engine

    health._snapshot = None
    probe_engine._schema_ok_at = None


def reset_reranker_state() -> None:
    """Clear every cached reranker, cooldown and the scoring pool."""
    from app.services.chat import reranker
    from app.services.chat import reranker_threads

    with reranker._lock:
        reranker._rerankers.clear()
        reranker._retry_after.clear()
    with reranker_threads._pool_lock:
        pool, reranker_threads._pool = reranker_threads._pool, None
        reranker_threads._pool_unavailable = False
    if pool is not None:
        pool.shutdown(wait=True)


def reset_sentence_splitter_state() -> None:
    """Clear the latched splitter decision and the tokenizer cache."""
    from app.services.search import sentence_splitter as ss

    ss._nltk_load_failed = False
    ss._nltk_tokenizers.clear()


def reset_chat_leg_executor() -> None:
    """Drop the chat-leg executor singleton so a test can rebuild it with a different size."""
    from app.services.chat import legs

    with legs._executor_lock:
        if legs._executor is not None:
            legs._executor.shutdown(wait=False, cancel_futures=True)
        legs._executor = None


def reset_cuda_health() -> None:
    """Clear the poisoned-context flag and the recent-error window."""
    from app.transcription import cuda_health

    cuda_health._POISONED.clear()
    with cuda_health._recent_lock:
        cuda_health._recent_errors.clear()


def reset_host_memory_gate() -> None:
    """Drop the host-memory admission gate singleton."""
    from app.transcription import host_memory_admission as ham

    with ham._lock:
        ham._gate = None


def reset_vram_budget() -> None:
    """Drop the VRAM budget singleton and its configured flag."""
    from app.transcription import vram_budget

    with vram_budget._config_lock:
        vram_budget._budget, vram_budget._configured = None, False


def clear_discovery_caches() -> None:
    """Drop cached OIDC discovery metadata and JWKS."""
    from app.auth.oidc import discovery

    with discovery._lock:
        discovery._discovery_cache.clear()
        discovery._jwks_cache.clear()


def clear_bucket_owner_cache() -> None:
    """Forget every resolved AWS bucket-owner account id."""
    from app.services.asr import aws_provider

    with aws_provider._owner_cache_lock:
        aws_provider._owner_cache.clear()


def current_vram_budget() -> VramBudget | None:
    """The configured VRAM budget, without configuring one."""
    from app.transcription import vram_budget

    return vram_budget._budget


def current_host_memory_gate() -> SlotGate | None:
    """The host-memory admission gate, without creating one."""
    from app.transcription import host_memory_admission as ham

    return ham._gate


def cuda_context_poisoned() -> bool:
    """Whether this process has been marked as holding a poisoned CUDA context."""
    from app.transcription import cuda_health

    return cuda_health._POISONED.is_set()


def current_cancellation_task_id() -> str | None:
    """The run bound to this thread's cancellation scope, or ``None`` outside one."""
    from app.core import task_cancellation

    scope = task_cancellation._CURRENT_SCOPE.get()
    return scope.task_id if scope else None
