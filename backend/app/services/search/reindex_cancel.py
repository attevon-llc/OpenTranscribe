"""Cancelling a reindex that fanned out across every owner (issue #691).

``POST /search/reindex`` dispatches one ``reindex_transcripts_task`` coordinator
per owner (#627), but ``POST /search/reindex/stop`` wrote a single
``reindex_cancel:{user_id}`` key — the caller's own — and each coordinator reads
the key belonging to the *file's* owner. So an admin could start a
deployment-wide re-embed and cancel only their own share of it; every other
owner's coordinator ran to completion. Both routes are ``get_current_admin_user``
gated, so that was a scope mismatch between start and stop rather than an
authorization gap — and it was *worse* than before #627, when start and stop were
consistently per-owner and the button therefore did what it appeared to.

Two things close it, and they are separable:

**The dispatch records what it dispatched.** ``dispatch_reindex_for_every_owner``
handed its ``{owner id: coordinator task id}`` mapping back to its caller and kept
no record, so ``stop`` had nothing to enumerate — which is why #627 documented the
gap instead of fixing it. The mapping is now also written to
``reindex_fanout:{admin id}``, keyed on the admin who triggered the run so two
admins' concurrent fan-outs are separate records that cancel separately.

**The cancel flag names the run it cancels.** ``reindex_cancel:{user_id}`` used to
hold ``"1"``; it now holds the coordinator task id being cancelled. That is what
makes stopping a *queued* coordinator work, and the queued coordinator is the case
that matters most: the fan-out dispatches the caller first, so on any deployment
whose worker pool is smaller than the owner count the caller's coordinator runs
while owners B..N sit in the broker queue. A flag that carried only ``"1"`` would
be erased the moment B's coordinator started — every coordinator clears the flag
on entry, because a flag left behind by an *earlier* run would otherwise abort the
next legitimate reindex after its first file. Naming the run is what lets a
coordinator tell those two cases apart: :func:`consume_pending_cancel` aborts when
the flag names *this* run and clears-and-proceeds when it names any other.

That also answers "what if a second run starts while one is cancelling": the
second run's coordinators carry different task ids, so the first run's flags read
as stale to them and are cleared, exactly as they always were. No run can inherit
another run's cancellation.

**Who clears what.** The flags stay **per owner** — there is deliberately no single
shared run flag, because the first coordinator of a fan-out to finish would then
have to either delete it (leaving the other N−1 uncancellable mid-run) or leave it
(poisoning the next run). Each owner's coordinator clears only its own key: once on
entry (:func:`consume_pending_cancel`) and once in the completion handler
(:func:`clear_cancel`). ``stop`` deletes the fan-out record it has just acted on.

**If a coordinator dies without clearing**, three things bound the damage, in
order: the leftover flag names a run that no longer exists, so no other run
honours it (:func:`cancel_requested` compares); ``app/main.py``'s startup sweep
clears ``reindex_cancel:*`` on every API restart; and both keys carry
``reindex_lock``'s one-hour TTL. ``reindex_fanout:*`` is deliberately **not** in
that sweep — the API process restarts independently of the Celery workers still
running the fan-out, and deleting the record would take the only handle ``stop``
has on those coordinators with it. A record left over from a finished run is
harmless for the same reason the flags are: the ids in it name nothing.

⚠️ Every function here imports ``get_redis`` inside its body rather than at module
scope. That is the same pattern ``search.py``'s ``_check_reindex_task_active``
uses and for the same reason: a module-level ``from app.core.redis import
get_redis`` binds the name at import time, and a test that patches
``app.core.redis.get_redis`` would then be pointing at a client nothing here
consults.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

#: Per-owner cancellation flag. The value names the coordinator run being
#: cancelled (see the module docstring); ``"1"`` is the legacy/unknown-run value.
CANCEL_KEY = "reindex_cancel:{user_id}"

#: The owner set one admin's fan-out dispatched, as ``{owner id: task id}``.
FANOUT_KEY = "reindex_fanout:{admin_id}"

#: Matches ``reindex_lock``'s expiry: a flag or record that outlives the run it
#: describes is the staleness the run-id comparison exists to survive, and an
#: hour bounds how long one can sit around being wrong.
CANCEL_TTL_SECONDS = 3600
FANOUT_TTL_SECONDS = 3600

#: Written when ``stop`` knows an owner is being cancelled but not which run —
#: a coordinator dispatched by ``search_index_maintenance``, or any run that
#: predates the fan-out record. Truthy, so the batch workers of a *running*
#: coordinator still stop; never equal to a task id, so a *queued* coordinator
#: treats it as stale and proceeds, which is the pre-#691 behaviour unchanged.
LEGACY_CANCEL_VALUE = "1"


def _decode(value: object) -> str | None:
    """Redis returns bytes (``get_redis()`` sets no ``decode_responses``)."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def cancel_target(user_id: int) -> str | None:
    """The coordinator run id this owner's pending cancellation names.

    Returns:
        The stored value — a task id, ``LEGACY_CANCEL_VALUE``, or ``None`` when
        no cancellation is pending.
    """
    from app.core.redis import get_redis

    try:
        return _decode(get_redis().get(CANCEL_KEY.format(user_id=user_id)))
    except Exception as e:
        # Fails OPEN, deliberately and as it always has: an unreachable Redis
        # reads as "no cancellation" so a reindex keeps working rather than
        # aborting mid-corpus. The opposite direction would let a Redis blip
        # silently truncate a re-embed.
        logger.warning(f"Could not read the reindex cancellation flag: {e}")
        return None


def cancel_requested(user_id: int, run_id: str | None = None) -> bool:
    """Whether a cancellation is pending for this owner's reindex run.

    ⚠️ **Pass ``run_id`` wherever you have one.** A flag naming a *different* run
    belongs to a run that is over, and honouring it would let one run inherit
    another's cancellation. That is reachable now that ``stop`` flags several
    owners at once: a fan-out record that outlives its coordinators (it carries a
    1-hour TTL) plus a later ``stop`` would otherwise write flags that abort the
    per-owner runs ``search_index_maintenance`` had since started on its own
    schedule.

    Args:
        user_id: The owner whose reindex is asking.
        run_id: The asking coordinator run's task id. ``None`` means the caller
            has no run identity, in which case any pending flag counts — the
            pre-#691 behaviour.

    Returns:
        True when the caller should stop after the file it is on.
    """
    target = cancel_target(user_id)
    if target is None:
        return False
    if run_id is None:
        return True
    return target in (run_id, LEGACY_CANCEL_VALUE)


def clear_cancel(user_id: int) -> None:
    """Drop this owner's cancellation flag."""
    from app.core.redis import get_redis

    try:
        get_redis().delete(CANCEL_KEY.format(user_id=user_id))
    except Exception as e:
        logger.warning(f"Could not clear the reindex cancellation flag: {e}")


def consume_pending_cancel(user_id: int, run_id: str) -> bool:
    """Was *this* coordinator run cancelled before it managed to start?

    Called once, on coordinator entry, in place of the unconditional clear that
    used to live there. The clear still happens either way — a flag that outlives
    its run must not abort the next one — but a flag naming ``run_id`` is a stop
    that landed while this coordinator sat in the queue, and answering it by
    clearing the flag and re-embedding the corpus anyway is the whole of #691 on
    a single-worker deployment.

    Args:
        user_id: The owner whose coordinator is starting.
        run_id: That coordinator's own task id.

    Returns:
        True when the pending cancellation names this run, i.e. the caller should
        release its lock and stop without indexing anything.
    """
    target = cancel_target(user_id)
    clear_cancel(user_id)
    return target is not None and target == run_id


def record_fanout(triggered_by: int, task_ids: dict[int, str]) -> None:
    """Persist the owner set one admin's fan-out just dispatched.

    Best-effort on purpose: the coordinators are already queued by the time this
    runs, so raising here would report a re-index that is genuinely under way as
    a failure. A record that could not be written degrades ``stop`` to its
    pre-#691 per-owner reach, which is why the failure is logged rather than
    swallowed.

    Args:
        triggered_by: The admin whose ``stop`` must be able to find this run.
        task_ids: ``{owner id: coordinator task id}``, as returned by
            ``dispatch_reindex_for_every_owner``.
    """
    from app.core.redis import get_redis

    if not task_ids:
        return
    try:
        get_redis().setex(
            FANOUT_KEY.format(admin_id=triggered_by),
            FANOUT_TTL_SECONDS,
            json.dumps({str(uid): tid for uid, tid in task_ids.items()}),
        )
    except Exception as e:
        logger.warning(
            f"Could not record the reindex fan-out for admin {triggered_by}; a stop "
            f"request will only reach their own coordinator: {e}",
            exc_info=True,
        )


def read_fanout(triggered_by: int) -> dict[int, str]:
    """The owner set this admin's most recent fan-out dispatched.

    Redis errors are **not** caught: the caller (``POST /reindex/stop``) cannot
    write cancellation flags either if Redis is down, and reporting that as a 500
    is honest where an empty mapping would look like "you started nothing".

    Returns:
        ``{owner id: coordinator task id}``, empty when no run is recorded.
    """
    from app.core.redis import get_redis

    raw = _decode(get_redis().get(FANOUT_KEY.format(admin_id=triggered_by)))
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
        return {int(uid): str(task_id) for uid, task_id in stored.items()}
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning(f"Discarding an unreadable reindex fan-out record: {e}")
        return {}


def clear_fanout(triggered_by: int) -> None:
    """Forget this admin's fan-out record, once it has been cancelled."""
    from app.core.redis import get_redis

    try:
        get_redis().delete(FANOUT_KEY.format(admin_id=triggered_by))
    except Exception as e:
        logger.warning(f"Could not clear the reindex fan-out record: {e}")


def request_cancel(targets: dict[int, str]) -> list[int]:
    """Flag every owner in ``targets`` for cancellation.

    Raises rather than degrading: a stop that reached only some of the run is
    indistinguishable from one that reached all of it, and reporting success for
    a corpus-wide re-embed that is still running is the silence #691 is about.

    Args:
        targets: ``{owner id: the coordinator run id to cancel}``.

    Returns:
        The owner ids flagged, in ascending order.
    """
    from app.core.redis import get_redis

    redis_client = get_redis()
    for user_id, run_id in sorted(targets.items()):
        redis_client.setex(CANCEL_KEY.format(user_id=user_id), CANCEL_TTL_SECONDS, run_id)
    return sorted(targets)
