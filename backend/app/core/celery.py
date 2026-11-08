# Telemetry opt-outs must be in os.environ before anything can import pyannote or
# huggingface_hub (both read them at import). Keep this the first import.
from app.core import privacy_env  # noqa: F401  # isort: skip

# isort: split

# Skip heavy AI imports during testing - speeds up test startup significantly
import logging
import os
import socket
import ssl

from app.core import celery_worker_lifecycle
from app.core import worker_metrics
from app.core.env import env_bool
from app.utils.nltk_offline import ensure_nltk_data_private

# Issue #1161: before anything imports prometheus_client, so a worker with WORKER_METRICS_PORT
# set builds every collector in multiprocess mode and its prefork children inherit that.
worker_metrics.configure()

logger = logging.getLogger(__name__)

# nltk >= 3.10.4 refuses a group/world-writable data tree (a bind-mounted models/nltk_data
# made under umask 002 is exactly that), which every consumer reads as "corpus absent".
# Every NLTK-using task runs in a process that imported this module, so fix it once here.
ensure_nltk_data_private()

_SKIP_CELERY = env_bool("SKIP_CELERY")


def _patch_torch_load(torch_module) -> None:
    """PyTorch 2.6+ compat: default ``weights_only=False`` for trusted HuggingFace models."""
    original = torch_module.load

    def _patched_torch_load(*args, **kwargs):
        # Handle both missing weights_only AND weights_only=None (which PyTorch 2.8 treats as True)
        if kwargs.get("weights_only") is None:
            kwargs["weights_only"] = False
        return original(*args, **kwargs)

    torch_module.load = _patched_torch_load


class _TorchLoadPatchFinder:
    """meta_path hook: patch ``torch.load`` the moment anything first imports torch.

    Importing torch eagerly here made every process that imports the Celery app (beat,
    Flower, download/NLP workers) pay hundreds of MB and seconds for a library it never
    uses. Patching on first import keeps the ordering guarantee — the patch is applied
    when torch's own import finishes, before pyannote/whisperx (which cache
    ``torch.load`` at import time) get the module back.
    """

    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        if fullname != "torch":
            return None
        import importlib.machinery
        import sys

        if cls in sys.meta_path:
            sys.meta_path.remove(cls)
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        loader = spec.loader if spec is not None else None
        if loader is None or not hasattr(loader, "exec_module"):
            return spec
        original_exec = loader.exec_module

        def exec_module(module):
            original_exec(module)
            _patch_torch_load(module)

        vars(loader)["exec_module"] = exec_module
        return spec


if not _SKIP_CELERY:
    import sys as _sys

    if "torch" in _sys.modules:
        _patch_torch_load(_sys.modules["torch"])
    else:
        _sys.meta_path.insert(0, _TorchLoadPatchFinder)

    # Note: WhisperX 3.8.1 has native PyAnnote v4 support — no patches needed

# Imports must come after torch.load patch to prevent caching issues
from celery import Celery  # noqa: E402
from celery.signals import before_task_publish  # noqa: E402
from celery.signals import setup_logging  # noqa: E402
from celery.signals import task_postrun  # noqa: E402
from celery.signals import task_prerun  # noqa: E402
from celery.signals import worker_process_shutdown  # noqa: E402
from celery.signals import worker_ready  # noqa: E402
from celery.signals import worker_shutdown  # noqa: E402
from celery.signals import worker_shutting_down  # noqa: E402

from app.core.celery_beat import BEAT_SCHEDULE  # noqa: E402
from app.core.celery_routing import CELERY_QUEUES  # noqa: E402
from app.core.celery_routing import INTENTIONALLY_UNROUTED_TASKS  # noqa: E402
from app.core.celery_routing import TASK_ROUTES  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.env import env_float  # noqa: E402 - after ensure_nltk_data_private()
from app.core.env import env_int  # noqa: E402 - after ensure_nltk_data_private()

# kombu refuses to connect over `rediss://` unless an SSL context is supplied — it does
# NOT fall back to a default. config.py builds a rediss:// URL whenever REDIS_USE_TLS is
# set, so without this every worker dies at startup on a TLS Redis (issue #284 A1.17).
# CERT_REQUIRED (the default) verifies the server certificate; set
# CELERY_REDIS_SSL_CERT_REQS=optional|none only for a self-signed broker you control.
_REDIS_SSL_MODES = {
    "required": ssl.CERT_REQUIRED,
    "optional": ssl.CERT_OPTIONAL,
    "none": ssl.CERT_NONE,
}
_redis_uses_tls = str(settings.CELERY_BROKER_URL).startswith("rediss://")
_redis_ssl_options = (
    {
        "ssl_cert_reqs": _REDIS_SSL_MODES.get(
            os.getenv("CELERY_REDIS_SSL_CERT_REQS", "required").lower(), ssl.CERT_REQUIRED
        )
    }
    if _redis_uses_tls
    else None
)

# Dead-connection detection for every Redis socket (broker AND result backend) — issue #1144.
#
# A worker<->Redis flow can vanish without a FIN/RST ever reaching the worker (NAT/conntrack
# expiry, firewall or network-policy reload, idle reset). Before this, such a worker stopped
# consuming FOREVER while the process stayed alive:
#
#   * kombu passes socket_keepalive=None to redis-py, which turns OFF redis-py's keepalive
#     default. The consumer's BRPOP waits in epoll for a reply on a dead socket and nothing
#     ever wakes it.
#   * When some other timer did hit an RST, the consumer's reconnect path ran
#     Channel.close(), which drains the outstanding BRPOP with a BLOCKING recv(). With
#     socket_timeout=None that recv never returned on the half-open socket: the main loop
#     wedged inside its own reconnect, with one "Connection to broker lost" line and then
#     silence.
#
# Keepalive makes the kernel error a dead socket in ~KEEPIDLE + KEEPINTVL*KEEPCNT seconds
# (30 + 10*3 = 60 s), which wakes epoll; TCP_USER_TIMEOUT covers the case keepalive
# cannot (it is suspended while unacknowledged data is in flight). socket_timeout bounds
# every blocking read, including that drain.
#
# socket_timeout MUST stay well above kombu's BRPOP wait (Transport.brpop_timeout = 1 s,
# or polling_interval if one is ever set): the drain in Channel.close() may legitimately
# wait that long, and a timeout under it would turn every idle poll into a reconnect.
# 30 s also leaves room for a slow-but-alive managed Redis under load.
_REDIS_SOCKET_TIMEOUT = env_float("CELERY_REDIS_SOCKET_TIMEOUT", 30.0)
_REDIS_CONNECT_TIMEOUT = env_float("CELERY_REDIS_CONNECT_TIMEOUT", 10.0)
_REDIS_HEALTH_CHECK_INTERVAL = 25  # kombu's own default, pinned so it cannot drift to 0
_REDIS_KEEPALIVE_OPTIONS = {
    opt: value
    for name, value in (
        ("TCP_KEEPIDLE", 30),
        ("TCP_KEEPINTVL", 10),
        ("TCP_KEEPCNT", 3),
        ("TCP_USER_TIMEOUT", 60_000),  # milliseconds
    )
    # Linux names; skip any the platform lacks rather than fail at import.
    if (opt := getattr(socket, name, None)) is not None
}

celery_app = Celery(
    "transcribe_app",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.transcription",
        "app.tasks.transcription.core",
        "app.tasks.transcription.preprocess",
        "app.tasks.transcription.postprocess",
        "app.tasks.transcription.enrich",
        "app.tasks.transcription.dispatch",
        "app.tasks.transcription.pipeline_error",
        "app.tasks.transcription.cancellation",
        "app.tasks.waveform",
        "app.tasks.waveform_generation",
        "app.tasks.playback_rendition",
        "app.tasks.summarization",
        "app.tasks.analytics",
        "app.tasks.cleanup",
        "app.tasks.utility",
        "app.tasks.recovery",
        "app.tasks.youtube_processing",
        "app.tasks.media_playlist_processing",
        "app.tasks.media_download",
        "app.tasks.transcript_document",
        "app.tasks.local_model_download",
        # Re-export shim for legacy imports; task names are explicit strings, so it
        # does not affect routing or registration.
        "app.tasks.speaker_tasks",
        "app.tasks.speaker_embedding_reassignment_task",
        "app.tasks.speaker_identification_task",
        "app.tasks.speaker_update_task",
        "app.tasks.speaker_merge_task",
        "app.tasks.speaker_embedding_task",
        "app.tasks.speaker_attribute_task",
        "app.tasks.topic_extraction",
        "app.tasks.ingest_artifacts_task",
        "app.tasks.reindex_task",
        "app.tasks.search_maintenance_task",
        "app.tasks.speaker_profile_sync_task",
        "app.tasks.opensearch_integrity_task",
        "app.tasks.tenant_backfill_task",
        "app.tasks.storage_orphan_sweep_task",
        "app.tasks.search_indexing_task",
        "app.tasks.search_file_field_tasks",
        "app.tasks.search_index_sweep_task",
        "app.tasks.search_reembed_task",
        "app.tasks.rename_propagation_task",
        "app.tasks.rename_digest_task",
        "app.tasks.redaction_task",
        "app.tasks.chat_retention",
        "app.tasks.legal_retention",
        "app.tasks.audit_retention",
        "app.tasks.audit_outbox_relay",
        "app.tasks.voiceprint_retention",
        "app.tasks.trash",
        "app.tasks.search_availability_task",
        "app.tasks.erasure_reconciliation",
        "app.tasks.erasure_receipt_reverify",
        "app.tasks.privacy_purge_task",
        "app.tasks.privacy_label_resync_task",
        "app.tasks.telemetry",
        "app.tasks.thumbnail",
        "app.tasks.thumbnail_migration",
        "app.tasks.embedding_migration_v4",
        "app.tasks.embedding_migration_v4_finalize",
        "app.tasks.imohash_recompute",
        "app.tasks.watch_source_tasks",
        "app.tasks.auth_mail_tasks",
        "app.tasks.speaker_embedding_migration",
        "app.tasks.baseline_export",
        "app.tasks.rediarize_task",
        "app.tasks.speaker_clustering",
        "app.tasks.auto_labeling",
        "app.tasks.speaker_attribute_migration_task",
        "app.tasks.combined_speaker_analysis_task",
        "app.tasks.speaker_embedding_consistency",
        "app.tasks.embedding_consistency_repair",
        "app.tasks.recovery_tasks",
        "app.tasks.backup_tasks",
        "app.tasks.directory_sync_task",
        "app.tasks.account_lifecycle",
        "app.tasks.session_cap",
    ],
)

celery_app.conf.update(
    broker_use_ssl=_redis_ssl_options,
    redis_backend_use_ssl=_redis_ssl_options,
    # Result-backend half of the dead-connection fix (see _REDIS_SOCKET_TIMEOUT). The
    # backend client does not accept keepalive tunables, so redis-py's own keepalive
    # defaults apply once socket_keepalive is on.
    redis_socket_keepalive=True,
    redis_socket_timeout=_REDIS_SOCKET_TIMEOUT,
    redis_socket_connect_timeout=_REDIS_CONNECT_TIMEOUT,
    redis_retry_on_timeout=True,
    redis_backend_health_check_interval=_REDIS_HEALTH_CHECK_INTERVAL,
    # Keep reconnecting through any broker outage. The default of 100 retries makes a
    # worker give up and sit idle after a long one.
    broker_connection_retry=True,
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=None,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # GPU-safe default: one task at a time. This is a GLOBAL setting and, as of
    # #892's audit, NO worker in docker-compose.yml passes --prefetch-multiplier
    # to override it — every worker (cpu/nlp/download/utility included) runs at
    # this same global 1, round-tripping the broker between tasks (issue #284
    # A1.8's per-worker override was never actually wired up). Keep the GPU
    # worker at 1 regardless — it must never hold a second task it cannot start
    # — and see backend/app/core/celery_metrics.py for why the *effective*
    # prefetch also bounds the cost of reading Redis's `unacked` hash.
    worker_prefetch_multiplier=1,
    # Prefork child recycling for any launcher (Helm, systemd, bare `celery worker`), not
    # just compose. 0/unset = no recycling. Explicit CLI flags override these. Prefork
    # only; the threads pool ignores both. Memory is in KiB and recycles a child AFTER
    # the task that pushed its RSS over the limit finishes.
    worker_max_tasks_per_child=env_int("CELERY_WORKER_MAX_TASKS_PER_CHILD", 0) or None,
    worker_max_memory_per_child=env_int("CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB", 0) or None,
    # Global task time limits (issue #284 A1.2). There were NONE, so a hung CUDA call
    # held the single GPU slot forever and no later transcription could start.
    #
    # Deliberately GENEROUS rather than tight. Media is capped at 4 h and hybrid/CPU
    # transcription of a long file is legitimately slow, so a tight global limit would
    # truncate real work — and getting per-task overrides wrong on even one long task
    # silently kills valid jobs. A 3 h ceiling still converts "wedged forever" into
    # "recovered in 3 h", which is the actual failure this addresses. Tasks needing more
    # override per-task (see combined_speaker_analysis_task for the pattern).
    #
    # Kept under visibility_timeout (6 h) so a timeout kill happens before redelivery,
    # not after — otherwise the two mechanisms would fight and duplicate work.
    #
    # CAVEAT: soft_time_limit is delivered via SIGALRM, which is unreliable under
    # --pool=threads — which is exactly what the GPU workers use. Treat these as a
    # backstop for the prefork/CPU queues; on GPU the real protection is
    # visibility_timeout plus the DB-status crash recovery in tasks/recovery.py.
    task_soft_time_limit=env_int("CELERY_SOFT_TIME_LIMIT", 10800),  # 3 h
    task_time_limit=env_int("CELERY_HARD_TIME_LIMIT", 11700),  # 3 h 15 m
    # How long a freshly FORKED prefork child has to send its WORKER_UP message
    # before the parent SIGKILLs it and forks a replacement
    # (celery/concurrency/asynpool.py: `verify_process_alive` ->
    # `error('Timed out waiting for UP message from %r'); os.kill(pid, 9)`).
    #
    # Celery's default is 4.0 s and NOTHING overrode it, which is the amplifier in
    # issue #631: any `worker_process_init` work that overruns the budget is killed
    # before it can finish, so the replacement child runs the SAME slow code and is
    # killed too — a self-sustaining fork/SIGKILL loop that consumes ZERO tasks and
    # ends only when the underlying slowness does. Observed: 69,231 kills over
    # 10 h 46 m on `cpu-processor`, at almost exactly `concurrency / 4.0` per second.
    #
    # 30 s is not a fix for a slow initializer (see `init_worker_process`, which no
    # longer does I/O) — it is defence in depth so ordinary contention on a loaded
    # host cannot start the loop at all. It stays FINITE deliberately: a genuinely
    # dead fork must still be reaped.
    #
    # ⚠️ THE TIMER COVERS RESPAWNS, NOT THE INITIAL POOL. `verify_process_alive` is armed
    # from `on_process_up` via `hub.call_later`, so it only exists once the worker has a
    # hub. The children forked when the pool is first populated predate that, and a hub
    # blocked in a long-running callback cannot fire it either (see `preload_models`).
    # Consequence for whoever greps logs during the next incident: a fork stall AT STARTUP
    # produces **no** `Timed out waiting for UP message` line at all — the worker simply
    # sits there. Absence of the signature is not absence of the problem; check
    # `init_worker_process`'s own "fork init started/finished" pairs, which are emitted
    # regardless of the hub.
    worker_proc_alive_timeout=env_float("CELERY_PROC_ALIVE_TIMEOUT", 30.0),
    # --- Worker-loss and shutdown behaviour (see app/core/broker_orphans.py) -------------
    # Soft shutdown (Celery 5.5+): on a COLD shutdown (SIGQUIT, or SIGTERM when the worker
    # runs with REMAP_SIGTERM=SIGQUIT) wait this long for running tasks, then cancel the rest
    # so kombu returns their unacked messages to the HEAD of their queues before the process
    # exits, instead of leaving them to the visibility timeout. It does nothing on a warm
    # shutdown (plain SIGTERM), which waits for running tasks with no limit; there a SIGKILL
    # at the end of the container's stop grace period is answered by the orphan reaper.
    worker_soft_shutdown_timeout=env_float("CELERY_WORKER_SOFT_SHUTDOWN_TIMEOUT", 30.0),
    # Also wait (and so also requeue cleanly) when the worker holds only reserved or
    # ETA-scheduled messages and runs nothing.
    worker_enable_soft_shutdown_on_idle=env_bool("CELERY_WORKER_SOFT_SHUTDOWN_ON_IDLE", True),
    # Explicitly OFF. On Redis an unacked message survives a dropped connection in the
    # broker's `unacked` hash and the worker can still ack it after reconnecting; turning
    # this on would kill every running transcription on a transient broker blip and leave
    # its message unacked -- creating the orphan it is meant to prevent.
    worker_cancel_long_running_tasks_on_connection_loss=False,
    # A REDELIVERED acks_late message whose task already succeeded (per the result backend)
    # is acked and skipped instead of run again. The stage ownership check
    # (tasks/transcription/run_ownership.py) covers the rest of the duplicate cases.
    worker_deduplicate_successful_tasks=True,
    worker_send_task_events=True,  # Enable real-time task events for Flower
    task_send_sent_event=True,  # Fire event when task is dispatched to queue
    result_expires=86400,  # Expire results after 24h (prevent Redis bloat)
    # Enable Redis priority queues: lower number = higher priority (runs first).
    # Priorities are PER-QUEUE — GPUPriority.X is independent of CPUPriority.X.
    # Named constants defined in app.core.queues: GPUPriority, CPUPriority, etc.
    # GPU queue:  0=speaker-reassign  1=embed-extract  2=transcription-retry
    #             3=transcription  4=rediarize  5=recluster  7=admin-migration-batches
    # CPU queue:  1=pipeline-retry  2=pipeline-critical  4=user-triggered  5=system
    #             6=admin  8=maintenance
    # NLP queue:  3=user-triggered  5=auto-pipeline  7=admin-batch  9=background
    # Download:   3=single-url  6=playlist
    # Embedding:  2=pipeline-critical
    # Utility:    1=emergency  3=operational  5=routine  7=background  9=dev-tools
    task_queues=CELERY_QUEUES,
    task_create_missing_queues=False,  # Catch queue name typos at dispatch time
    broker_transport_options={
        "priority_steps": list(range(10)),
        "queue_order_strategy": "priority",
        # visibility_timeout MUST exceed the longest task (issue #284 A1.1).
        #
        # With Redis as the broker, an un-acked message is redelivered to another
        # worker once this elapses. The transcription tasks are acks_late=True, so a
        # run that outlives it is handed to a SECOND worker while the first is still
        # going — the same file transcribed twice, on the GPU, concurrently.
        #
        # Nothing set this before, so the kombu default of 3600s applied: ANY
        # transcription over one hour was silently duplicated. A 4-hour media limit
        # makes that reachable with an ordinary file, not just a pathological one.
        #
        # 21600 (6h) covers the longest supported job with headroom. Do NOT set it
        # absurdly high "to be safe" -- and do NOT lower it to speed up recovery
        # either. Redis has no per-message lease (nothing like SQS's
        # ChangeMessageVisibility), so this one value applies to every acks_late task
        # however long it legitimately runs, counted from DELIVERY: lowering it below
        # the longest run plus its time held in a worker re-runs live work. Worker
        # loss is recovered in minutes WITHOUT it: transcription stages hold an app
        # lease and the orphan reaper (app/core/broker_orphans.py) returns a dead
        # holder's message to the head of its queue as soon as the lease lapses. This
        # timeout is only the backstop for acks_late tasks that hold no lease.
        "visibility_timeout": int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "21600")),
        # Dead-connection detection — see _REDIS_SOCKET_TIMEOUT above (issue #1144).
        "socket_keepalive": True,
        "socket_keepalive_options": _REDIS_KEEPALIVE_OPTIONS,
        "socket_timeout": _REDIS_SOCKET_TIMEOUT,
        "socket_connect_timeout": _REDIS_CONNECT_TIMEOUT,
        "health_check_interval": _REDIS_HEALTH_CHECK_INTERVAL,
        # Deliberately False for the BROKER (the result backend below sets it True).
        # It gives every redis-py connection Retry(NoBackoff(), 1), and that one retry
        # swallows the ConnectionError a dead fanout/pubsub socket raises: redis-py
        # reconnects and re-issues PubSub.parse_response(block=True), which reads with
        # timeout=None by design — so the main loop wedges again, now waiting on a
        # healthy socket for a broadcast that may never come. Reproduced with py-spy.
        # Letting the error reach kombu makes the consumer rebuild its connection.
        "retry_on_timeout": False,
    },
    task_routes=TASK_ROUTES,
    beat_schedule=BEAT_SCHEDULE,
)

# Make this app the process-wide default, not just this thread's (issue #485).
#
# `Celery.__init__` sets only the CREATING thread's `celery._state._tls.current_app`;
# it never sets the module-global `default_app`. Anything that resolves a task through
# `get_current_app()` — every `@shared_task` proxy — therefore reads
# `_tls.current_app or default_app`, and on a thread that did not construct the app both
# are None. Celery then mints a fallback `Celery('default')` with NO broker configured and
# caches it globally, so the dispatch goes to the amqp:// class default and is refused.
#
# The API hits this on every request: FastAPI/Starlette runs each sync `def` endpoint on a
# `run_in_threadpool` worker thread, which is never the import thread. That made every admin
# "Run now" action 500 intermittently, while workers (which import this module on their own
# main thread) were unaffected.
#
# `set_default()` is what populates `default_app`, so the fallback resolves to the real,
# configured app from any thread and any process. Keep this even though the `@shared_task`
# decorators below were converted to `@celery_app.task` — it is the cheap general guard, and
# without it a single reintroduced `shared_task` silently brings the bug back.
celery_app.set_default()


# Apply our logging config (text/JSON per settings.LOG_FORMAT) to Celery.
# Connecting setup_logging ALSO disables Celery's root-logger hijack
# (worker_hijack_root_logger), so the configuration survives worker startup.
# We deliberately use setup_logging — NOT worker_process_init, which fires only in
# prefork child processes and so would miss the --pool=threads workers entirely.
# ⚠️ The inverse is ALSO false: this comment used to claim `--pool=threads` was "this
# app's default" and that `worker_process_init` therefore never fires. FIVE of the
# eleven worker services in docker-compose.yml pass no `--pool=` flag and are prefork
# (celery-download-worker, celery-cpu-worker, celery-cloud-asr-worker,
# celery-nlp-worker, celery-embedding-worker); threads is the GPU/redaction family's
# opt-in. That misreading is very likely why the fork path went unexamined for the
# whole of issue #631 — see `init_worker_process` below, which DOES fire, on those five.
@setup_logging.connect
def configure_celery_logging(**kwargs):
    """Configure structured/text logging for Celery workers and the beat."""
    from app.core.logging_config import configure_logging

    configure_logging()


# Correlate background-task logs with the HTTP request that spawned them: the
# request_id rides in the task headers and is adopted into the audit ContextVar
# for the duration of the task (reset in close_session_after_task below). Tasks
# that spawn sub-tasks propagate automatically — publish happens inside the
# task context, so before_task_publish re-reads the now-set ContextVar.
#
# The same handler stamps the publish time the queue-wait metrics read (issue #1172,
# app/core/queue_wait.py). One receiver for both: they are two headers on the same message.
# It also counts the publish for the inertness checker (issue #898, app/core/inert.py),
# first, so a failure in the header work cannot hide a publish from the count.
@before_task_publish.connect
def inject_request_id_header(sender=None, headers=None, **kwargs):
    """Count the publish, then stamp the request_id (if any) and publish time onto headers."""
    from app.core.inert import record_task_published
    from app.core.queue_wait import stamp_published_at
    from app.middleware.audit import get_request_id

    record_task_published(sender)
    stamp_published_at(headers)
    request_id = get_request_id()
    if request_id and headers is not None:
        headers["request_id"] = request_id


@task_prerun.connect
def record_replayable_task(task=None, task_id=None, args=None, kwargs=None, **_):
    """Record an idempotent task so it can be replayed if its worker dies (issue #1067)."""
    if task is None or task_id is None:
        return
    from app.core.task_replay import on_task_start

    on_task_start(task, task_id, args, kwargs)


@task_postrun.connect
def clear_replayable_task(task=None, task_id=None, **_):
    """The run ended, whatever its outcome, so it no longer needs replaying."""
    if task is None or task_id is None:
        return
    from app.core.task_replay import on_task_end

    on_task_end(task, task_id)


@task_prerun.connect
def adopt_request_id(task=None, **kwargs):
    """Adopt the inbound request_id header into the task's ContextVar."""
    from app.middleware.audit import set_request_id

    request = getattr(task, "request", None)
    request_id = getattr(request, "request_id", None) if request is not None else None
    if request_id:
        set_request_id(request_id)


@worker_ready.connect
def warn_inert_max_tasks_per_child(**kwargs):
    """Warn when --max-tasks-per-child is configured but cannot take effect.

    ``--max-tasks-per-child`` is a PREFORK-pool feature: it recycles the child process
    after N tasks. Under ``--pool=threads`` there are no child processes, so Celery
    silently ignores it (issue #284 A1.7). The GPU workers default to threads —
    deliberately, so model weights stay pinned in VRAM between tasks — which means an
    operator lowering GPU_MAX_TASKS to bound a VRAM leak gets **no recycling at all**
    and no indication of that.

    We warn rather than switch pools: forcing prefork would reload the model on every
    task and destroy the performance characteristic threads exists to provide. An
    operator who genuinely needs recycling sets GPU_WORKER_POOL=prefork and accepts
    that cost.
    """
    import os
    import sys

    argv = " ".join(sys.argv)
    if "--pool=threads" not in argv and os.getenv("GPU_WORKER_POOL", "threads") != "threads":
        return

    memory_limit = celery_app.conf.worker_max_memory_per_child
    if "--max-memory-per-child" in argv or memory_limit:
        logger.warning(
            "A max-memory-per-child limit (%s) is set but this worker uses the threads pool, "
            "where Celery IGNORES it — there is no worker recycling on memory growth. "
            "Set GPU_WORKER_POOL=prefork if you need recycling (models reload per task).",
            memory_limit or "CLI flag",
        )

    configured = 0
    if "--max-tasks-per-child" in argv:
        try:
            configured = int(argv.split("--max-tasks-per-child=")[1].split()[0])
        except (IndexError, ValueError):
            configured = 0
    else:
        configured = int(celery_app.conf.worker_max_tasks_per_child or 0)
    # A deliberately huge value means "never recycle" and is not a misconfiguration.
    if configured <= 0 or configured >= 10000:
        return

    logger.warning(
        "--max-tasks-per-child=%d is set but this worker uses the threads pool, where "
        "Celery IGNORES it — there is no worker recycling and no VRAM-leak guard. "
        "Set GPU_WORKER_POOL=prefork if you need recycling (models reload per task).",
        configured,
    )


# Worker lifecycle receivers (fork init, database-role check, model preload) live in
# celery_worker_lifecycle. They are connected here so `worker_ready` keeps its order: the
# warning above, preload, task-route validation, then the metrics listener below.
celery_worker_lifecycle.connect_signals()


@worker_ready.connect
def validate_task_routes_on_ready(**kwargs):
    """Validate that all registered tasks have explicit queue routes, once preload is done."""
    _validate_task_routes()


def unrouted_task_names() -> list[str]:
    """Registered non-builtin tasks with neither a route nor an allowlist entry.

    Such a task lands on the default 'celery' queue whenever a caller does not pass
    ``queue=`` itself. Only tasks already registered are seen, so a caller that wants
    the complete answer must import the ``include=`` modules first
    (``celery_app.loader.import_default_modules()``); a started worker already has.
    """
    routed_names = set(celery_app.conf.task_routes.keys())
    return sorted(
        name
        for name in celery_app.tasks
        if not name.startswith("celery.")
        # Celery shares @app.task registrations across apps, so a test that builds its own
        # throwaway Celery app leaks its tasks (e.g. ``guard.errback``) into this registry
        # when a whole suite runs in one process. Only tasks this codebase defines count.
        and getattr(celery_app.tasks[name], "__module__", "").startswith("app.")
        and name not in INTENTIONALLY_UNROUTED_TASKS
        and name not in routed_names
    )


def _validate_task_routes():
    """Log a warning per registered task missing from task_routes (worker startup).

    The enforcing version of this check is ``tests/unit/test_task_routes_complete.py``;
    this log line is the backstop for a deployment that loads extra task modules.
    """
    unrouted = unrouted_task_names()
    for name in unrouted:
        logger.warning(
            f"Task '{name}' has no task_routes entry — will go to default 'celery' queue"
        )
    if not unrouted:
        logger.info(
            f"Task route validation passed: {len(celery_app.conf.task_routes)} routes, "
            f"{len(INTENTIONALLY_UNROUTED_TASKS)} intentionally dynamic"
        )


@task_postrun.connect
def close_session_after_task(**kwargs):
    """Clear the per-task request_id correlation.

    Deliberately does NOT call ``engine.dispose()`` (issue #284 A1.6). Disposing on
    every ``task_postrun`` tears down the whole connection pool after each task, so
    every task paid a fresh Postgres TCP + TLS + auth handshake and pooling did nothing.
    The post-fork dispose in ``worker_process_init`` is what actually matters — it stops
    a forked child inheriting the parent's sockets — and that is still in place.

    Sessions are closed by their own context managers (``session_scope`` /
    ``SessionLocal`` teardown), not here, so nothing leaks by removing this.
    """
    from app.middleware.audit import set_request_id

    # Clear the request_id adopted in task_prerun so it can't leak into the next
    # task that reuses this thread/process (threads pool reuses workers).
    set_request_id("")


# Graceful GPU worker shutdown (issue #782). Logic lives in app.core.worker_shutdown, not
# here, so it stays unit-testable without importing this module (and therefore torch) at
# all. These three shims are intentionally thin — the same shape preload_models() already
# uses toward ModelManager.
@worker_shutting_down.connect
def arm_worker_shutdown_flag(**kwargs):
    """Mark that a shutdown signal has arrived.

    ⚠️ Fires INSIDE celery's signal handler, in the MainProcess, BEFORE the task pool has
    drained the in-flight task — releasing a model here would free VRAM (and tear down the
    CUDA context) under a live CUDA kernel. Delegates to
    ``app.core.worker_shutdown.mark_shutting_down()``, which does nothing but set a flag.
    Do not add anything else to this handler.
    """
    from app.core.worker_shutdown import mark_shutting_down

    mark_shutting_down()


@worker_shutdown.connect
def release_gpu_resources_on_worker_shutdown(**kwargs):
    """Release the transcriber, diarizer, PII pool, and CUDA context before the worker
    process exits.

    Fires in the MainProcess AFTER ``_shutdown(warm=True)`` has joined the pool, so no
    task is still running — this is what makes releasing models here safe. See
    ``app.core.worker_shutdown.release_worker_resources`` for the release order and why
    each step is gated on ``sys.modules`` rather than an import.
    """
    from app.core.worker_shutdown import release_worker_resources

    release_worker_resources()


# Issue #1161: per-task counters and run times, served on WORKER_METRICS_PORT when set.
worker_metrics.connect_signals()


@worker_process_shutdown.connect
def release_embedding_cache_on_process_shutdown(**kwargs):
    """Per-forked-child cleanup at reap time.

    Only ``celery-embedding-worker`` (the sole PREFORK service that populates the
    embedding cache) has anything to release here — every other prefork child's copy is
    always empty, and the GPU/redaction threads-pool workers never fork at all.
    """
    from app.core.worker_shutdown import release_embedding_cache_only

    release_embedding_cache_only()
