"""Worker process lifecycle hooks: fork init, database-role check, model preload.

Wired to celery's signals by ``connect_signals()``, which ``core/celery.py`` calls at the
point in its registration order where these receivers originally sat (``worker_ready``
receivers run in connection order: the inert-recycling warning, then ``preload_models``,
then the metrics listener).

Nothing here may import ``celery_app``: this module is imported by ``core/celery.py``.
"""

import logging
import os
import socket

from app.core.env import env_bool
from app.core.env import env_int

logger = logging.getLogger(__name__)


def connect_signals() -> None:
    """Connect the lifecycle receivers to celery's signals."""
    from celery import signals

    signals.beat_init.connect(start_watch_source_fs_events)
    signals.worker_process_init.connect(init_worker_process)
    signals.worker_init.connect(verify_database_role)
    signals.worker_ready.connect(preload_models)


# The watch-source FS-event layer needs a long-lived process, and beat is the
# one service that is single-instance by design — running two would double every
# scheduled task, so nobody scales it. Starting it here (and NOT on the workers)
# gives exactly one observer set per deployment. It never raises: any failure
# logs and leaves the every-minute `watch_source.scan_all` poll as the sole
# mechanism, which is the pre-#294 behaviour.
def start_watch_source_fs_events(**kwargs):
    """Start the watchdog-based watch-source observer inside celery-beat."""
    try:
        from app.services.watch_sources.fs_events import start_supervisor

        start_supervisor()
    except Exception as e:  # noqa: BLE001 - beat must start regardless
        logger.error(f"Watch-source FS-event supervisor could not be started: {e}")


def publish_hf_token_to_environment() -> bool:
    """Make the server-environment Hugging Face token visible to ``huggingface_hub``.

    Runs inside every forked prefork child against celery's ``worker_proc_alive_timeout``
    kill timer (issue #631), so it is a dict write and nothing else: no network, no DB, no
    locks. That is why it publishes only the ENVIRONMENT token
    (``services.huggingface_token.publish_env_token_for_fork``); a token saved in Settings
    is resolved, and published the same way, by ``get_hf_token()`` at the first model load.

    ⚠️ **Never call ``huggingface_hub.login()`` from here.** ``login()`` -> ``whoami()``
    issues an HTTPS request through ``requests`` **with no ``timeout=``**, so a degraded
    network path blocks for as long as the kernel's TCP/DNS retries take (23-30 s measured),
    which is what made issue #631 unrecoverable. Exporting ``HF_TOKEN`` gives every
    un-parameterised Hub call the same credential at the cost of one dict write.

    Returns:
        True when this call exported the token, False when there was nothing to export
        or the operator had already published one.
    """
    from app.services.huggingface_token import publish_env_token_for_fork

    return publish_env_token_for_fork()


def _pool_concurrency() -> int:
    """Best-effort prefork concurrency of this worker, from its own argv/env."""
    import sys

    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg.startswith("--concurrency="):
            value = arg.split("=", 1)[1]
        elif arg in ("--concurrency", "-c") and i + 1 < len(argv):
            value = argv[i + 1]
        else:
            continue
        try:
            return max(1, int(value))
        except ValueError:
            break
    return max(1, env_int("CELERY_WORKER_CONCURRENCY", 1))


def _cap_torch_threads_to_cpu_quota() -> None:
    """Divide the container CPU quota across prefork children for torch intra-op threads.

    Without this every child defaults to one thread per HOST core. Cheap and I/O-free:
    torch is only touched if something already imported it (never imported here), and an
    explicit OMP_NUM_THREADS / TORCH_NUM_THREADS wins.
    """
    import sys

    torch_mod = sys.modules.get("torch")
    if torch_mod is None or os.getenv("OMP_NUM_THREADS") or os.getenv("TORCH_NUM_THREADS"):
        return
    from app.utils.cpu_budget import effective_cpu_count

    threads = max(1, effective_cpu_count() // _pool_concurrency())
    try:
        torch_mod.set_num_threads(threads)
    except Exception as exc:  # noqa: BLE001 - never block fork init
        logger.debug("Could not cap torch threads: %s", exc)
        return
    logger.info("torch intra-op threads set to %d (CPU quota / pool concurrency)", threads)


# Signal handlers for proper database connection management
def init_worker_process(**kwargs):
    """Initialize a forked prefork child: publish the HF token, drop inherited DB sockets.

    ⚠️ **Everything in this function runs against a hard kill timer** (issue #631).
    ``worker_process_init`` fires inside the forked child from
    ``billiard.pool.Worker.after_fork()``, which runs *before*
    ``on_loop_start()`` puts the ``WORKER_UP`` message on the out-queue. The parent armed
    ``asynpool.verify_process_alive`` at fork time; if UP does not arrive within
    ``worker_proc_alive_timeout`` the child is ``SIGKILL``ed and a replacement is forked,
    which then runs this same function. Anything slow here is therefore not "a slow
    start" — it is an unbounded fork/kill loop that consumes no tasks and cannot recover
    on its own.

    So: **no network calls, no locks, no model loads, no unbounded I/O in here.** The two
    steps below are a dict write and a connection-pool reset, both microseconds.
    """
    import time
    import warnings

    started = time.monotonic()
    pid = os.getpid()
    # Issue #631: an entry line with no matching "finished" line, repeating, identifies
    # a stuck fork initializer directly from the container logs. Reconstructing that
    # from `asynpool`'s SIGKILL lines alone took an after-the-fact investigation.
    logger.info("Celery fork init started (pid=%s)", pid)

    # Suppress benign warnings from AI libraries (community models, library compat)
    warnings.filterwarnings("ignore", message=".*loss function.*", category=UserWarning)
    warnings.filterwarnings("ignore", message=".*not writable.*", category=UserWarning)
    warnings.filterwarnings("ignore", module="pytorch_lightning")
    warnings.filterwarnings("ignore", module="lightning_fabric")

    if publish_hf_token_to_environment():
        logger.debug("HuggingFace token published to HF_TOKEN for gated model access")

    from app.db.base import engine

    # close=False: the inherited connections share their sockets with the parent, and
    # closing one here sends the server a Terminate that ends the PARENT's session.
    # SQLAlchemy docs, "Using Connection Pools with Multiprocessing or os.fork()".
    engine.dispose(close=False)

    _cap_torch_threads_to_cpu_quota()

    logger.info(
        "Celery fork init finished (pid=%s, elapsed=%.3fs)", pid, time.monotonic() - started
    )


def verify_database_role(**kwargs):
    """Refuse to start a worker whose database role defeats the requested role split.

    Runs once in the main process (``worker_init``), not in the forked children, so it is
    outside the ``worker_process_init`` kill timer. A worker started with an old ``.env``
    would otherwise silently keep running as the table owner.
    """
    from app.db.base import engine
    from app.db.role_check import enforce_runtime_role

    try:
        enforce_runtime_role(engine)
    except RuntimeError as e:
        logger.critical("Audit record protection requested but not effective: %s", e)
        raise SystemExit(1) from e
    finally:
        engine.dispose()


def _pool_is_threads() -> bool:
    """Whether this worker runs celery's threads pool, from its argv (or GPU_WORKER_POOL)."""
    import sys

    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg.startswith("--pool="):
            return arg.split("=", 1)[1] == "threads"
        if arg in ("--pool", "-P") and i + 1 < len(argv):
            return argv[i + 1] == "threads"
    return os.getenv("GPU_WORKER_POOL", "threads") == "threads"


def reconcile_gpu_concurrent_requests() -> None:
    """Keep a GPU worker's engine slots in step with its thread count (issue #1072).

    ``celery --pool=threads --concurrency=N`` runs N transcriptions at once in this process;
    ``GPU_CONCURRENT_REQUESTS`` sets CTranslate2's ``num_workers`` and switches the stage
    logic between single-request and concurrent behaviour. Left at 1 with N > 1, all N
    threads queue on one CTranslate2 worker (Whisper is serialised) while every stage acts
    as if it had the card to itself.

    Unset on a GPU threads worker -> default it to the pool concurrency. Set and different
    -> one WARNING naming both; the operator's value is kept. Prefork is left alone: each
    child there loads its own models, so engine slots are per process by construction.
    """
    if not env_bool("PRELOAD_GPU_MODELS", False) or not _pool_is_threads():
        return
    threads = _pool_concurrency()
    configured = os.environ.get("GPU_CONCURRENT_REQUESTS", "").strip()
    if not configured:
        os.environ["GPU_CONCURRENT_REQUESTS"] = str(threads)
        logger.info(
            "GPU_CONCURRENT_REQUESTS unset; matched to the worker's thread concurrency (%d)",
            threads,
        )
        return
    from app.transcription.config import TranscriptionConfig

    slots = TranscriptionConfig._resolve_concurrent_requests()
    if slots != threads:
        logger.warning(
            "GPU_CONCURRENT_REQUESTS=%s gives %d engine slot(s) but this worker runs "
            "--concurrency=%d threads. Set them to the same value: fewer slots than threads "
            "serialises Whisper, more slots than threads loads CTranslate2 workers nothing uses.",
            configured,
            slots,
            threads,
        )


def configure_gpu_host_admission() -> None:
    """Cap a GPU threads worker's concurrent tasks by host memory (issue #1073).

    Prefork is left alone: there each child is its own process running one task at a time,
    so a per-process gate has nothing to count.
    """
    if not env_bool("PRELOAD_GPU_MODELS", False) or not _pool_is_threads():
        return
    from app.transcription import host_memory_admission

    host_memory_admission.configure(_pool_concurrency())


# Wall-clock bound on the CPU-lightweight Whisper warm-up (issue #631). Matched to
# celery-cpu-worker's `start_period: 120s` in docker-compose.yml, which is both the
# allowance the deployment declares for this load AND the window in which a frozen
# MainProcess goes unnoticed — see the call site for why those being the same number is
# the point.
_CPU_WHISPER_PRELOAD_TIMEOUT_S = 120.0


def preload_models(**kwargs):
    """Preload AI models at worker startup.

    ⚠️ This runs on ``worker_ready``, in the **MainProcess main thread** — the thread that
    also runs celery's event loop, and therefore ``asynpool.verify_process_alive``. A model
    load that stalls here does not merely delay startup: it freezes the loop, so a forked
    child that never signals UP is neither SIGKILLed nor logged, and the respawn-storm
    signature (``Timed out waiting for UP message``) does not appear at all. Every load
    below must be bounded. See ``init_worker_process`` for the fork side of issue #631.

    GPU workers: Load Whisper + PyAnnote into VRAM (shared across threads).
    CPU-transcribe workers: Load lightweight Whisper model into RAM.
    Other CPU workers: No model preloading needed.
    """
    import os

    # GPU model preloading — ONLY on GPU workers (PRELOAD_GPU_MODELS=true).
    # Other workers (cpu-processor, search-indexer, etc.) must NOT load models,
    # even if CUDA is available, to avoid wasting 15+ GB of GPU memory.
    # Set via docker-compose.yml environment for gpu worker containers only.
    is_gpu_worker = env_bool("PRELOAD_GPU_MODELS", False)

    try:
        if is_gpu_worker:
            from app.transcription.config import TranscriptionConfig

            # Before the config is built: it reads GPU_CONCURRENT_REQUESTS, which sets
            # CTranslate2's num_workers at load time (issue #1072).
            reconcile_gpu_concurrent_requests()
            configure_gpu_host_admission()
            config = TranscriptionConfig.from_environment()
            if config.device == "cuda":
                import torch

                from app.transcription import vram_budget
                from app.transcription.model_manager import ModelManager

                ModelManager.get_instance().ensure_models_loaded(config)
                # Measure the card now that the models are resident, so their footprint
                # (and anything else sharing the GPU) is outside the admission budget.
                vram_budget.configure_from_device(config.device_index)

                # Enable TF32 AFTER model loading. PyAnnote's fix_reproducibility()
                # disables TF32 during Pipeline.from_pretrained(). Re-enabling here
                # gives Whisper ~15-20% speedup on Ampere+ GPUs (RTX 3000+, A-series).
                # pipeline.py also re-enables after each diarization run.
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
                logger.info("TF32 enabled for Tensor Core acceleration")

                # Pin the model name so subsequent tasks use the loaded model,
                # even if the admin changes the DB setting before restarting.
                TranscriptionConfig.pin_model(config.model_name)

                # Say which model this worker now serves, so the Settings page can confirm a
                # model switch instead of guessing (#1246). One bounded Redis SET; never raises.
                from app.services.local_models import worker_report

                worker_report.report_loaded_model(
                    getattr(kwargs.get("sender"), "hostname", None) or socket.gethostname(),
                    config.model_name,
                )

                logger.info(
                    "GPU models preloaded and pinned "
                    f"(model={config.model_name}, "
                    f"concurrent_requests={config.concurrent_requests})"
                )
        else:
            logger.info("Skipping GPU model preload (PRELOAD_GPU_MODELS not set)")
    except Exception as e:
        logger.debug(f"GPU model preloading skipped: {e}")

    # CPU lightweight model preloading
    if env_bool("PRELOAD_CPU_WHISPER", False):
        try:
            from app.transcription.config import TranscriptionConfig as CpuTranscriptionConfig

            cpu_config = CpuTranscriptionConfig.for_cpu_lightweight()
            logger.info(
                f"Preloading CPU lightweight model '{cpu_config.model_name}' "
                f"(compute_type={cpu_config.compute_type})..."
            )
            import faster_whisper

            from app.utils.hf_hub_offline import hf_offline_requested
            from app.utils.hf_hub_offline import load_with_timeout

            # NOT named `kwargs`: this function's own signature already binds that name.
            load_kwargs: dict[str, bool] = {}
            if hf_offline_requested():
                load_kwargs["local_files_only"] = True

            # Load model to warm the cache — subsequent loads are instant.
            #
            # ⚠️ BOUNDED, and not merely for tidiness (issue #631). `WhisperModel(...)`
            # resolves the model through the HuggingFace Hub, and **nothing on that path
            # sets a timeout** — the duration of a failure is whatever the client's retry
            # chain happens to produce for that particular fault. It runs on
            # `worker_ready`, in the **MainProcess main thread**, so a stall here freezes
            # the worker's event loop — and that loop is what runs
            # `asynpool.verify_process_alive`. A frozen loop means a forked child that
            # misses its UP message is neither killed nor logged, so the respawn-storm
            # signature never appears. Bounding this is part of making that detection
            # trustworthy, not a separate cleanup.
            #
            # ⚠️ Be precise about what the bound buys. Measured in the prod image against a
            # blackholed huggingface.co, the UNBOUNDED call returned on its own after
            # 140.0 s and the bounded one after 125.7 s — so for *that* fault it saves
            # about 14 s. Its value is that it is a **declared** ceiling: a TCP blackhole
            # happens to terminate, whereas an endpoint that accepts the connection and
            # never answers does not (there is no read timeout here either), and neither
            # does a resolver that keeps retrying.
            #
            # 120 s is not a round number: it is celery-cpu-worker's own `start_period` in
            # docker-compose.yml. That is the window during which a frozen MainProcess is
            # INVISIBLE — outside it, a worker that cannot answer `inspect stats` already
            # fails the healthcheck. Matching the two means the freeze cannot outlive the
            # window in which nothing would notice it. Failing is cheap either way: this
            # only warms a cache, and the first task loads the model anyway.
            _model = load_with_timeout(
                lambda: faster_whisper.WhisperModel(
                    cpu_config.model_name,
                    device="cpu",
                    compute_type="int8",
                    **load_kwargs,
                ),
                timeout=_CPU_WHISPER_PRELOAD_TIMEOUT_S,
                label=f"CPU lightweight Whisper model ({cpu_config.model_name})",
            )
            del _model
            logger.info(f"CPU lightweight model '{cpu_config.model_name}' preloaded successfully")
        except Exception as e:
            logger.warning(f"CPU lightweight model preloading failed: {e}")

    # Content-redaction model preloading — ONLY on the dedicated celery-redaction
    # worker (PRELOAD_REDACTION_MODELS=true). Loads Presidio+GLiNER (PII) and the
    # toxicity classifier once, shared across the threads pool. No GPU.
    if env_bool("PRELOAD_REDACTION_MODELS", False):
        try:
            # Cap PyTorch intra-op threads so a single inference can't peg every core
            # on a shared single machine (env OMP_NUM_THREADS is the primary control;
            # this is a runtime fallback). Background redaction yields to user-facing work.
            _rt = os.getenv("OMP_NUM_THREADS") or os.getenv("REDACTION_TORCH_THREADS")
            if _rt:
                try:
                    import torch

                    torch.set_num_threads(int(_rt))
                    logger.info("Redaction worker: torch intra-op threads capped at %s", _rt)
                except (ImportError, ValueError, RuntimeError) as _te:
                    logger.debug("Could not cap torch threads: %s", _te)

            from app.services.redaction.detectors import pii_presidio
            from app.services.redaction.detectors import toxicity

            ok_pii = pii_presidio.preload()
            ok_tox = toxicity.preload()

            # Bring the PII pool's workers up now. Spawned workers each import the app and
            # load spaCy, so a cold pool makes the first scan slower than the sequential
            # path it replaces — the cost belongs at worker start, not in a user's scan.
            from app.services.redaction import pii_pool

            ok_pool = pii_pool.warm()
            logger.info(
                "Redaction models preloaded (pii=%s, toxicity=%s, pii_pool=%s)",
                ok_pii,
                ok_tox,
                ok_pool,
            )
        except Exception as e:
            logger.warning(f"Redaction model preloading failed: {e}")
