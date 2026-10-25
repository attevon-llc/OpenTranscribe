"""Per-file post-processing task states: the honest "is this file finished?" signal.

Everything the system does to a file after its transcript is committed (search indexing,
facts, analytics, redaction, speaker attributes / suggestions / clustering, summary, its
index, topics) reports through this one module, keyed by
:class:`~app.core.enums.PostProcessingTask`:

* ``QUEUED`` / ``SKIPPED`` where the task is, or is not, dispatched. The enrichment fan-out
  sends them together as a **plan** (:func:`report_plan`), which also opens a new run;
  a task queued later by another task (LLM speaker suggestions after attribute detection,
  the summary's index after the summary) is declared by its dispatcher with :func:`report`
  before the parent's own terminal frame, so a client never sees a gap in which "everything
  known is finished" is true but false.
* ``STARTED`` / ``DONE`` / ``FAILED`` / ``SKIPPED`` from the task itself, through
  :func:`tracks_post_processing`. A Celery retry reports nothing (the task stays started).

Each report is written to a Redis hash (``post_processing:{file_id}``, last write wins by
server timestamp, entries older than the current run's ``run_at`` dropped) and pushed as one
``post_processing_status`` WebSocket frame to the file's OWNER through
``send_ws_event_for_file`` (so a quarantined file is never named to a non-admin). The hash
backs ``GET /api/files/{uuid}/post-processing``, which a client reads when it may have missed
frames. Frames carry the file UUID, task keys, states and timestamps only: never a filename,
a title or transcript text.

Every function here is best-effort: reporting must never fail the work it reports on.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import time
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import TypeVar
from typing import cast

from app.core.constants import NOTIFICATION_TYPE_POST_PROCESSING_STATUS
from app.core.constants import POST_PROCESSING_STATUS_TTL_S
from app.core.enums import PostProcessingState
from app.core.enums import PostProcessingTask

logger = logging.getLogger(__name__)

_KEY = "post_processing:{file_id}"
_RUN_FIELD = "_run_at"

_FAILED_STATUSES = frozenset({"error", "failed"})
_SKIPPED_STATUSES = frozenset({"skipped", "disabled", "not_configured", "not_found"})

# Last write wins per task, and a write older than the run it would land in is dropped.
_REPORT_LUA = """
local run_at = tonumber(redis.call('HGET', KEYS[1], '_run_at') or '0')
local at = tonumber(ARGV[3])
if at < run_at then return 0 end
local prev = tonumber(redis.call('HGET', KEYS[1], ARGV[1] .. ':at') or '-1')
if at < prev then return 0 end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2], ARGV[1] .. ':at', ARGV[3])
if ARGV[5] ~= '' then redis.call('HSET', KEYS[1], ARGV[1] .. ':started_at', ARGV[5]) end
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""

# A newer run drops every entry reported before it, then takes the plan's entries unless a
# task already reported something newer (a task can start before the plan is written).
_PLAN_LUA = """
local run_at = tonumber(ARGV[1])
local prev_run = tonumber(redis.call('HGET', KEYS[1], '_run_at') or '0')
if run_at < prev_run then return 0 end
local flat = redis.call('HGETALL', KEYS[1])
for i = 1, #flat, 2 do
  local f = flat[i]
  if string.sub(f, -3) == ':at' then
    local task = string.sub(f, 1, -4)
    if tonumber(flat[i + 1]) < run_at then
      redis.call('HDEL', KEYS[1], task, f, task .. ':started_at')
    end
  end
end
redis.call('HSET', KEYS[1], '_run_at', ARGV[1])
for i = 3, #ARGV, 2 do
  local prev = tonumber(redis.call('HGET', KEYS[1], ARGV[i] .. ':at') or '-1')
  if prev < run_at then
    redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1], ARGV[i] .. ':at', ARGV[1])
  end
end
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""


@dataclass(frozen=True)
class FileRef:
    """The three facts a report needs about its file."""

    id: int
    uuid: str
    owner_id: int


def resolve_file(*, file_id: int | None = None, file_uuid: str | None = None) -> FileRef | None:
    """Look the file up by id or UUID in a short session of its own; None if it is gone."""
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile

    if (file_id is None) == (file_uuid is None):
        raise TypeError("resolve_file needs exactly one of file_id, file_uuid")
    try:
        with session_scope() as db:
            query = db.query(MediaFile.id, MediaFile.uuid, MediaFile.user_id)
            row = (
                query.filter(MediaFile.id == file_id).first()
                if file_id is not None
                else query.filter(MediaFile.uuid == str(file_uuid)).first()
            )
    except Exception as exc:  # noqa: BLE001 — reporting must never break the task
        logger.debug("post-processing status: file lookup failed (%s)", exc)
        return None
    if row is None:
        return None
    return FileRef(id=int(row[0]), uuid=str(row[1]), owner_id=int(row[2]))


def _fmt(ts: float) -> str:
    return repr(float(ts))


def _publish(ref: FileRef, payload: dict[str, Any]) -> bool:
    from app.utils.websocket_notify import send_ws_event_for_file

    return send_ws_event_for_file(
        ref.owner_id, NOTIFICATION_TYPE_POST_PROCESSING_STATUS, payload, file_id=ref.id
    )


def report(
    task: PostProcessingTask,
    state: PostProcessingState,
    *,
    ref: FileRef | None = None,
    file_id: int | None = None,
    file_uuid: str | None = None,
    started_at: float | None = None,
    at: float | None = None,
) -> bool:
    """Record and push one task's state for one file.

    Pass ``ref`` when the caller already resolved the file, else one of ``file_id`` /
    ``file_uuid``. ``started_at`` (server epoch seconds) travels with a terminal state so the
    client can time a task whose start frame it missed. Returns True when the frame was
    published; never raises.
    """
    try:
        if ref is None:
            ref = resolve_file(file_id=file_id, file_uuid=file_uuid)
        if ref is None:
            return False
        now = time.time() if at is None else at
        started = now if state is PostProcessingState.STARTED else started_at
        try:
            from app.core.redis import get_redis

            get_redis().eval(
                _REPORT_LUA,
                1,
                _KEY.format(file_id=ref.id),
                task.value,
                state.value,
                _fmt(now),
                str(POST_PROCESSING_STATUS_TTL_S),
                "" if started is None else _fmt(started),
            )
        except Exception as exc:  # noqa: BLE001 — the frame still goes out
            logger.debug("post-processing status: Redis write failed (%s)", exc)
        entry: dict[str, Any] = {"state": state.value, "at": now}
        if started is not None:
            entry["started_at"] = started
        return _publish(ref, {"file_id": ref.uuid, "tasks": {task.value: entry}})
    except Exception as exc:  # noqa: BLE001 — reporting must never break the task
        logger.debug("post-processing status: report failed (%s)", exc)
        return False


def report_plan(
    ref: FileRef,
    decisions: Mapping[PostProcessingTask, PostProcessingState],
    *,
    run_at: float,
) -> bool:
    """Open a new run for ``ref`` with the fan-out's dispatch decisions.

    ``run_at`` is when the fan-out began: every report of this run is later than it, every
    report of an earlier run is older, which is how both Redis and the client tell runs apart
    without a run id on every task. Never raises.
    """
    try:
        args: list[str] = [_fmt(run_at), str(POST_PROCESSING_STATUS_TTL_S)]
        for task, state in decisions.items():
            args += [task.value, state.value]
        try:
            from app.core.redis import get_redis

            get_redis().eval(_PLAN_LUA, 1, _KEY.format(file_id=ref.id), *args)
        except Exception as exc:  # noqa: BLE001 — the frame still goes out
            logger.debug("post-processing status: Redis plan write failed (%s)", exc)
        tasks = {t.value: {"state": s.value, "at": run_at} for t, s in decisions.items()}
        return _publish(ref, {"file_id": ref.uuid, "run_at": run_at, "tasks": tasks})
    except Exception as exc:  # noqa: BLE001
        logger.debug("post-processing status: plan failed (%s)", exc)
        return False


def read_status(file_id: int) -> dict[str, Any]:
    """The stored states for one file: ``{"run_at": float | None, "tasks": {...}}``.

    ``run_at`` is None when no run is known (nothing reported, or the entry expired); the
    caller must then say it does not know, not that the file is finished.
    """
    from app.core.redis import get_redis

    raw = cast("dict[bytes, bytes]", get_redis().hgetall(_KEY.format(file_id=file_id)))
    fields = {k.decode(): v.decode() for k, v in raw.items()}
    run_at = float(fields[_RUN_FIELD]) if _RUN_FIELD in fields else None
    valid = {t.value for t in PostProcessingTask}
    tasks: dict[str, dict[str, Any]] = {}
    for name, value in fields.items():
        if name not in valid or f"{name}:at" not in fields:
            continue
        entry: dict[str, Any] = {"state": value, "at": float(fields[f"{name}:at"])}
        if f"{name}:started_at" in fields:
            entry["started_at"] = float(fields[f"{name}:started_at"])
        tasks[name] = entry
    return {"run_at": run_at, "tasks": tasks}


def outcome_of(result: object) -> PostProcessingState:
    """Map a task's returned ``{"status": ...}`` onto a terminal state.

    A task that returned normally without a recognised failure or skip status is DONE.
    """
    status = result.get("status") if isinstance(result, Mapping) else None
    text = str(status).lower() if status is not None else ""
    if text in _FAILED_STATUSES:
        return PostProcessingState.FAILED
    if text in _SKIPPED_STATUSES:
        return PostProcessingState.SKIPPED
    return PostProcessingState.DONE


def _will_autoretry(exc: BaseException) -> bool:
    """True when Celery's ``autoretry_for`` is about to turn ``exc`` into a retry."""
    try:
        from celery import current_task

        task = current_task
        autoretry = tuple(getattr(task, "autoretry_for", ()) or ())
        if not autoretry or not isinstance(exc, autoretry):
            return False
        max_retries = getattr(task, "max_retries", None)
        return max_retries is None or int(task.request.retries) < int(max_retries)
    except Exception:  # noqa: BLE001
        return False


F = TypeVar("F", bound=Callable[..., Any])


def tracks_post_processing(
    task: PostProcessingTask, *, file_param: str, announce_start: bool = True
) -> Callable[[F], F]:
    """Report a task body's start and outcome for the file named by ``file_param``.

    Place it directly above the function, under ``@celery_app.task``. ``file_param`` is
    ``"file_id"`` (an int) or ``"file_uuid"``. A Celery ``Retry`` (and an exception
    ``autoretry_for`` will retry) reports nothing, so the task stays started; any other
    exception reports FAILED and propagates; a return value is mapped by
    :func:`outcome_of`. ``announce_start=False`` is for a task that must first decide
    whether it will run at all (the LLM tasks check for a provider): it calls
    :func:`announce_started` itself, so a deployment without an LLM never shows the task
    as running.
    """
    from celery.exceptions import Retry

    by_uuid = file_param == "file_uuid"

    def decorator(func: F) -> F:
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ref: FileRef | None = None
            try:
                value = signature.bind_partial(*args, **kwargs).arguments.get(file_param)
                if value is not None:
                    ref = (
                        resolve_file(file_uuid=str(value))
                        if by_uuid
                        else resolve_file(file_id=int(value))
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("post-processing status: cannot resolve %s (%s)", file_param, exc)
            started_at = time.time()
            if ref is not None and announce_start:
                report(task, PostProcessingState.STARTED, ref=ref, at=started_at)
            try:
                result = func(*args, **kwargs)
            except Retry:
                raise
            except Exception as exc:
                if ref is not None and not _will_autoretry(exc):
                    report(task, PostProcessingState.FAILED, ref=ref, started_at=started_at)
                raise
            if ref is not None:
                report(task, outcome_of(result), ref=ref, started_at=started_at)
            return result

        return cast("F", wrapper)

    return decorator


@contextlib.contextmanager
def reporting_step(task: PostProcessingTask, ref: FileRef) -> Iterator[None]:
    """Report a step that runs inside another task's body: started, then done or failed.

    For work with no Celery task of its own (applying tag/collection suggestions inside
    topic extraction, or per file in a retroactive auto-label batch). The exception
    propagates after FAILED is reported.
    """
    started_at = time.time()
    report(task, PostProcessingState.STARTED, ref=ref, at=started_at)
    try:
        yield
    except Exception:
        report(task, PostProcessingState.FAILED, ref=ref, started_at=started_at)
        raise
    report(task, PostProcessingState.DONE, ref=ref, started_at=started_at)


def announce_started(
    task: PostProcessingTask, *, file_id: int | None = None, file_uuid: str | None = None
) -> bool:
    """The deferred start report of a task decorated with ``announce_start=False``."""
    return report(task, PostProcessingState.STARTED, file_id=file_id, file_uuid=file_uuid)
