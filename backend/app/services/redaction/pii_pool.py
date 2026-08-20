"""A persistent worker pool for PII analysis.

Presidio's analyze() is CPU-bound NER plus a recognizer sweep, and it runs once per transcript
segment — 1077 calls for a 2.9 h file, 13.7 s of wall time and 82% of a redaction scan. It does
not thread: measured 0.72x at 4 threads and 0.58x at 8, because spaCy holds the GIL for most of
the work. Processes do help — 7x at 12 workers, with byte-identical results.

Two constraints shape what follows:

*Forkserver, not fork or spawn.* The redaction worker runs ``--pool=threads``, and forking a
multi-threaded process clones only the calling thread — any lock another thread holds
(allocator, logging, the DB pool) can deadlock the child, and a live CUDA context makes it
worse. Plain ``spawn`` is safe from that but re-imports the parent's ``__main__``, which under
Celery is the worker CLI: every child tried to boot a second worker and the pool broke
immediately. ``forkserver`` starts one clean server process and forks children from *that*, so
children inherit neither the threads nor the CUDA context, and nothing re-executes ``__main__``.

*Persistent, not per-scan.* Workers each import the app and load spaCy, which costs more than a
single scan saves — a per-scan pool measured 21 s against 13.7 s sequential. Created once and
reused, the same pool settles at ~2 s per scan.

The pool is optional in the strictest sense: if it cannot be built, callers fall back to scanning
in-process, which is slower but identical.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import threading
from concurrent.futures import ProcessPoolExecutor

logger = logging.getLogger(__name__)

#: Worker count. Beyond ~12 the gain flattens while memory grows (each worker holds its own
#: spaCy model), so this is capped rather than scaled to a 48-core host.
DEFAULT_WORKERS = 8
MAX_WORKERS = 12

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_pool_failed = False

# Set in each worker process by _init_worker; never touched in the parent.
_analyzer = None


def _mp_context():
    """A start method whose children are safe to create from a threaded Celery worker.

    ``forkserver`` is preferred; the server is forked once, before the children, so they carry
    neither the parent's threads nor its CUDA context. ``spawn`` is the fallback for platforms
    without it, and is only correct where ``__main__`` is importable.

    The forkserver server is a fresh interpreter, so it needs ``/app`` on ``PYTHONPATH`` -- the
    image sets it; see the note beside ``ENV PYTHONPATH`` in ``backend/Dockerfile.*``.
    """
    try:
        ctx = mp.get_context("forkserver")
        # Load the analyzer's imports in the server so each child does not re-do them.
        ctx.set_forkserver_preload(["app.services.redaction.detectors.pii_presidio"])
        return ctx
    except (ValueError, AttributeError):
        return mp.get_context("spawn")


def _worker_count() -> int:
    env = os.environ.get("REDACTION_PII_WORKERS")
    if env:
        try:
            return max(1, min(MAX_WORKERS, int(env)))
        except ValueError:
            logger.warning("REDACTION_PII_WORKERS=%r is not an integer; using default", env)
    cores = os.cpu_count() or 2
    return max(1, min(DEFAULT_WORKERS, MAX_WORKERS, cores - 1))


def _init_worker() -> None:
    """Build one analyzer per worker process, once.

    Hides the GPU first. These workers run Presidio on the CPU and never touch CUDA, but the
    app import pulls in torch, and each child was creating its own CUDA context: 294 MiB apiece,
    2.35 GiB across eight workers, on a 12 GB card that already carries a 7.5 GiB model floor.
    This must run before the first app import, because torch caches the device list on first use.
    """
    global _analyzer
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    try:
        from app.services.redaction.detectors import pii_presidio

        _analyzer = pii_presidio._get_analyzer(False)  # noqa: SLF001 — same package
    except Exception:
        logger.exception("PII worker failed to build an analyzer")
        _analyzer = None


def _analyze_shard(texts: list[str]) -> list[list[tuple]]:
    """Analyze a shard, returning plain tuples — RecognizerResult does not pickle cleanly.

    Returning ``[]`` for a text the analyzer could not examine would be indistinguishable from
    "examined and found nothing", which is exactly the confusion a redaction system must not
    make. A worker with no analyzer raises instead, and the caller falls back in-process.
    """
    if _analyzer is None:
        raise RuntimeError("PII analyzer unavailable in worker")
    out = []
    for text in texts:
        results = _analyzer.analyze(text=text, language="en")
        out.append([(r.entity_type, r.start, r.end, float(r.score)) for r in results])
    return out


def get_pool() -> ProcessPoolExecutor | None:
    """The shared pool, built on first use. ``None`` once construction has failed."""
    global _pool, _pool_failed
    if _pool is not None or _pool_failed:
        return _pool
    with _pool_lock:
        if _pool is not None or _pool_failed:
            return _pool
        try:
            workers = _worker_count()
            ctx = _mp_context()
            ctx_name = ctx.get_start_method()
            _pool = ProcessPoolExecutor(
                max_workers=workers,
                initializer=_init_worker,
                mp_context=ctx,
            )
            logger.info("PII analysis pool started: %d workers via %s", workers, ctx_name)
        except Exception:
            logger.exception("PII analysis pool could not start; scans run in-process")
            _pool_failed = True
            _pool = None
    return _pool


def warm() -> bool:
    """Force the workers up so the first real scan does not pay for spaCy loading.

    Called from the worker-ready hook. Without it the first scan absorbs the whole startup and
    looks no faster than the sequential path it replaced.
    """
    pool = get_pool()
    if pool is None:
        return False
    try:
        workers = _worker_count()
        # One task per worker, each long enough not to be handed to an already-warm process.
        list(pool.map(_analyze_shard, [["warm up"] for _ in range(workers)]))
        logger.info("PII analysis pool warm")
        return True
    except Exception:
        # A pool that broke during warm-up stays broken, so drop it rather than caching a
        # dead handle — the next scan rebuilds it from an ordinary task context, which is
        # where it works. Building during worker bootstrap is what fails.
        logger.warning(
            "PII pool warm-up failed during bootstrap; it will be rebuilt on first use",
            exc_info=True,
        )
        shutdown()
        return False


def analyze_texts(texts: list[str]) -> list[list[tuple]] | None:
    """Analyze many texts in parallel, or ``None`` if the caller should do it in-process.

    Shards round-robin rather than in contiguous blocks so one long stretch of dialogue does not
    land entirely on a single worker.
    """
    if not texts:
        return []
    pool = get_pool()
    if pool is None:
        return None
    workers = _worker_count()
    if len(texts) < workers * 2:
        return None  # too small to be worth the round trip
    try:
        shards = [texts[i::workers] for i in range(workers)]
        results = list(pool.map(_analyze_shard, shards))
        # Undo the round-robin.
        out: list[list[tuple] | None] = [None] * len(texts)
        for shard_idx, shard in enumerate(results):
            for pos, value in enumerate(shard):
                out[shard_idx + pos * workers] = value
        if any(v is None for v in out):
            logger.warning("PII pool returned an incomplete result set; falling back in-process")
            return None
        return out  # type: ignore[return-value]
    except Exception:
        logger.exception("PII pool analysis failed; falling back in-process")
        return None


def shutdown() -> None:
    """Release the workers (worker shutdown hook)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None
