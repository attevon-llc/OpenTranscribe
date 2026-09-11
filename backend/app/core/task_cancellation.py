"""Per-task cooperative cancellation for a user-initiated stop (issue #823).

``cancel_active_task`` called ``celery_app.control.revoke(..., terminate=True)`` and then
flipped the file to ``CANCELLED``. Neither half did what it claimed:

* ``terminate=True`` signals the *pool worker* running the task. Every GPU queue (``gpu``,
  ``gpu-transcribe``, ``gpu-diarize``) and the redaction queue run ``--pool=threads``, and
  CPython cannot deliver a signal to an arbitrary thread — so for those queues it is a no-op
  and the decode keeps running, holding VRAM.
* ⚠️ **It is a no-op on a PREFORK worker too**, which is a second, independent defect #823's
  body does not mention: ``MediaFile.active_task_id`` holds the *application* task id
  (``dispatch.py`` generates a ``uuid.uuid4()`` and threads it through all three stages as
  ``preprocess_context["task_id"]``), while ``pipeline.apply_async()`` is called with **no**
  ``task_id=`` and celery mints its own ids. So the id being revoked has never belonged to a
  celery message on any queue, for any pool type. ``revoke()`` was removed rather than
  "fixed" — plumbing celery's ids through would buy only the prefork legs, and this module
  already covers every leg.

So the DB said CANCELLED while the GPU carried on, and on a single-GPU host that silently
blocks the user's own next job.

**This is #809's mechanism, aimed at a different trigger.** Issue #809 built cooperative abort
for *worker shutdown* for the identical reason (a threads pool cannot be preempted), and #823
routes user-cancel through the same checkpoints rather than building a second abort mechanism.
:func:`stand_down_if_requested` is that single checkpoint — it consults both triggers, and
``app/transcription/engine/stages.py`` plus ``transcriber.py`` call it where they used to call
``raise_if_shutting_down`` directly.

Three things differ from #809, all deliberate:

**The signal is PER TASK, not process-wide.** ``worker_shutdown._SHUTDOWN`` is one
``threading.Event`` meaning "this whole worker is going away"; reusing it here would abort
every transcription on the box whenever any one of them was cancelled. The flag is a Redis key
named after the run (:data:`CANCEL_KEY`) and the running task is identified by a
:class:`~contextvars.ContextVar` scope (:func:`cancellation_scope`) that each task binds around
its own body. Under ``--pool=threads`` every task runs in its own pool thread and a
``ContextVar`` is per thread, so two concurrent jobs read two different scopes and neither can
see the other's flag.

**Redis, not an in-memory flag, because the two ends are different PROCESSES.** The API sets
the flag; the Celery worker reads it. Same split — and same key-in-Redis answer — as
``services/search/reindex_cancel.py``.

**The outcome is NOT #809's requeue.** A shutdown abort means "another worker should finish
this", so ``context.requeue_after_abort`` rejects with ``requeue=True``. A user cancel means
"do not run this at all"; requeueing it would resurrect the job the user just stopped, which is
worse than doing nothing — the UI would say cancelled and the file would go back to processing.
:class:`TranscriptionCancelledError` is therefore a **separate** exception (see its docstring
for why it must not subclass ``TranscriptionAbortedError``) handled by
``tasks/transcription/cancellation.finish_cancelled``, which acks the message and stops.

⚠️ **The flag is keyed on a fresh ``uuid4`` per dispatch**, so — unlike ``reindex_cancel``'s
per-*owner* key, which needed #691's "the flag names the run" treatment — a leftover flag can
never be inherited by a later run: no later run will ever carry that id. The TTL is a
housekeeping bound, not a correctness one.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.core.worker_shutdown import raise_if_shutting_down

logger = logging.getLogger(__name__)

#: Cancellation flag for one pipeline run. The value is the file UUID (for the worker's log
#: line); only the key's EXISTENCE is load-bearing.
CANCEL_KEY = "transcription_cancel:{task_id}"

#: How long the checkpoint may serve a cached "not cancelled" before polling Redis again.
#:
#: The checkpoint sits in ``transcriber.transcribe``'s decode loop, which iterates once per
#: decoded segment — thousands of times on a long file — so an unconditional Redis round trip
#: per call would put the broker in the ASR hot path. 2 s bounds the added cancel latency to
#: roughly one poll interval on top of the loop's own granularity, while capping the traffic at
#: one ``EXISTS`` per running task per 2 s regardless of file length.
#:
#: Not tunable by design: a larger value only makes cancel feel broken, and a smaller one buys
#: latency the enclosing decode batch does not deliver anyway.
CANCEL_POLL_INTERVAL_S = 2.0


class TranscriptionCancelledError(Exception):
    """A pipeline stage stood down because the USER cancelled this specific file (#823).

    Deliberately **not** a subclass of ``worker_shutdown.TranscriptionAbortedError``, and that
    is load-bearing rather than taxonomy: all three transcription tasks carry an
    ``except TranscriptionAbortedError`` clause that calls ``requeue_after_abort`` — a subclass
    would be caught there and the cancelled job would be rejected back onto the broker and run
    again on the next worker. The user would see "cancelled", then see it processing again.

    Like its #809 sibling it is a plain exception rather than a celery one: the transcription
    engine must stay importable on a CPU-only worker and in a bare pytest process, so the TASK
    layer owns the translation into an outcome.
    """


@dataclass
class _CancelScope:
    """The run a thread is currently executing, plus its poll state.

    One instance per task, bound to a :class:`~contextvars.ContextVar` for the duration of that
    task's body. Holding the poll state *here* rather than in a module global is what keeps two
    concurrent tasks independent — a shared cache would let one job's poll answer for another.
    """

    task_id: str
    file_uuid: str
    #: Once a cancellation is observed it never un-observes: the stage is about to raise, and a
    #: flag that expired between the observation and the raise must not resurrect the job.
    latched: bool = False
    #: ``monotonic()`` deadline before which :meth:`cancelled` serves the cached answer. 0.0 so
    #: the FIRST call always polls — an entry checkpoint must not be blind for 2 s.
    next_poll_at: float = 0.0

    def cancelled(self) -> bool:
        """Whether this run has been cancelled, polling Redis at most every poll interval."""
        if self.latched:
            return True
        now = time.monotonic()
        if now < self.next_poll_at:
            return False
        self.next_poll_at = now + CANCEL_POLL_INTERVAL_S
        if _flag_is_set(self.task_id):
            self.latched = True
        return self.latched


#: The run executing on THIS thread, or ``None`` outside a transcription task.
#:
#: A ``ContextVar`` rather than a module-level dict keyed by thread: celery's threads pool
#: REUSES threads, so the binding has to be unwound when the task ends or the next task on that
#: thread would inherit a stale run id. :func:`cancellation_scope` resets it in a ``finally``.
_CURRENT_SCOPE: ContextVar[_CancelScope | None] = ContextVar(
    "opentranscribe_cancel_scope", default=None
)


def _cancel_ttl_seconds() -> int:
    """Housekeeping bound on a cancellation flag, taken from the broker's visibility timeout.

    A flag must outlive the run it names, and ``CELERY_VISIBILITY_TIMEOUT`` (6 h) is already
    this deployment's answer to "how long may one pipeline message legitimately be in flight"
    — raising a file-length limit has to move that value, and this follows it rather than
    declaring a second number that would silently disagree.

    ⚠️ Read from the ENVIRONMENT, exactly as ``core/celery.py``'s ``broker_transport_options``
    does, **not** from ``Settings``: it is not a declared ``Settings`` field, so
    ``settings.CELERY_VISIBILITY_TIMEOUT`` does not exist and a ``getattr`` default here would
    be a fixed constant wearing a derivation's clothes.
    """
    import os

    try:
        return int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "21600"))
    except ValueError:
        return 21600


def _flag_is_set(task_id: str) -> bool:
    """Whether a cancellation flag exists for ``task_id``.

    Fails **OPEN** — an unreachable Redis reads as "not cancelled" — for the same reason
    ``reindex_cancel.cancel_target`` does: a Redis blip must not stand down a transcription
    that nobody asked to stop. The opposite direction would turn a momentary outage into every
    in-flight job on the host cancelling itself.

    ``get_redis`` is imported inside the body, not at module scope, so a test patching
    ``app.core.redis.get_redis`` is patching the object this actually calls.
    """
    from app.core.redis import get_redis

    try:
        return bool(get_redis().exists(CANCEL_KEY.format(task_id=task_id)))
    except Exception as e:
        logger.warning("Could not read the cancellation flag for task %s: %s", task_id, e)
        return False


def request_cancel(task_id: str, file_uuid: str) -> bool:
    """Arm the cooperative stop for one pipeline run. Called from the API process.

    Args:
        task_id: The application task id — ``MediaFile.active_task_id``, the same value every
            stage of the chain carries as ``preprocess_context["task_id"]``.
        file_uuid: Recorded as the value so the worker's stand-down log names the file.

    Returns:
        True when the flag is armed. **False means no cooperative stop is pending** and the
        caller must not report the work as stopping — ``cancel_active_task`` uses exactly that
        to decide whether the file may sit in ``CANCELLING``.
    """
    from app.core.redis import get_redis

    try:
        get_redis().setex(
            CANCEL_KEY.format(task_id=task_id), _cancel_ttl_seconds(), file_uuid or "1"
        )
        return True
    except Exception as e:
        logger.error(
            "Could not arm the cooperative cancellation for task %s (file %s): %s",
            task_id,
            file_uuid,
            e,
            exc_info=True,
        )
        return False


def cancel_requested(task_id: str) -> bool:
    """Uncached read of one run's cancellation flag, for callers outside a scope.

    The reconciliation task uses this; the hot-path checkpoint does not (it goes through
    :meth:`_CancelScope.cancelled`, which throttles).
    """
    return _flag_is_set(task_id)


def clear_cancel(task_id: str) -> None:
    """Drop a run's cancellation flag once the stop has been carried out or superseded."""
    from app.core.redis import get_redis

    try:
        get_redis().delete(CANCEL_KEY.format(task_id=task_id))
    except Exception as e:
        logger.warning("Could not clear the cancellation flag for task %s: %s", task_id, e)


@contextmanager
def cancellation_scope(task_id: str, file_uuid: str = "") -> Iterator[None]:
    """Bind ``task_id`` as the run executing on this thread, for the enclosed body.

    Every task that can reach :func:`stand_down_if_requested` must wrap its body in this, or
    the checkpoint has no run to ask about and silently never fires.

    The ``finally`` reset is mandatory, not tidiness: celery's threads pool reuses threads, so
    a leaked binding would make the *next* task on that thread poll the previous task's flag.
    """
    token = _CURRENT_SCOPE.set(_CancelScope(task_id=task_id, file_uuid=file_uuid))
    try:
        yield
    finally:
        _CURRENT_SCOPE.reset(token)


def current_cancellation_task_id() -> str | None:
    """The run bound to this thread, or ``None`` outside a scope. Diagnostics and tests."""
    scope = _CURRENT_SCOPE.get()
    return scope.task_id if scope else None


def raise_if_cancelled(where: str) -> None:
    """Cooperative-cancel checkpoint for the run bound to this thread.

    A **no-op when no scope is bound**, and that is the correct reading rather than a
    fail-open compromise: code running outside a transcription task has no run to cancel, and
    making it poll Redis would put a broker round trip on every caller of the engine (the
    benchmark harness, the eval runner, a unit test).

    Args:
        where: Names the checkpoint in the log and in the exception, so a stand-down says
            which stage stopped rather than only that one did.

    Raises:
        TranscriptionCancelledError: if this run has been cancelled by the user.
    """
    scope = _CURRENT_SCOPE.get()
    if scope is None or not scope.cancelled():
        return
    logger.info(
        "cooperative cancel at %s — task %s (file %s) was cancelled by the user",
        where,
        scope.task_id,
        scope.file_uuid or "unknown",
    )
    raise TranscriptionCancelledError(f"cancelled by user; stood down at {where}")


def stand_down_if_requested(where: str) -> None:
    """THE cooperative checkpoint. Call at a bounded point in a long GPU/decode path.

    One function for both triggers so a new checkpoint cannot accidentally honour only one of
    them — which is the whole reason #823 extends #809's call sites instead of adding its own
    beside them.

    ⚠️ **Cancel is checked FIRST, and the order is a decision.** When a worker is shutting down
    *and* the user has cancelled this file, honouring the shutdown would
    ``Reject(requeue=True)`` and hand the cancelled job to the next worker, which would then
    stand it down again — churn, and a window where the file reads as processing after the
    user stopped it. Cancel wins: the job ends, for good, and the shutdown proceeds.

    Args:
        where: Checkpoint name, threaded into whichever exception is raised.

    Raises:
        TranscriptionCancelledError: the user cancelled THIS file — do not requeue (#823).
        TranscriptionAbortedError: the worker is shutting down — requeue (#809).
    """
    raise_if_cancelled(where)
    raise_if_shutting_down(where)
