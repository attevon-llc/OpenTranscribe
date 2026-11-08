"""Run-scoped state of one embedding-consistency repair: its lease and its batch plan.

The repair is one orchestrator (``speaker_embedding_consistency``) fanning out to many GPU
batch tasks (``embedding_consistency_repair``). Everything the two halves must agree on
about a run lives here.

Batch messages carry identifiers for their OWN files and nothing else. Celery's task guide
("Performance and Strategies": *Data locality* and *State*,
https://docs.celeryq.dev/en/stable/userguide/tasks.html) is to keep the data a task needs
close to the worker and pass references to it, not the data itself. Every batch used to
carry the run's WHOLE missing-speaker set: on a library with ~100,000 affected files that was
~4,000 messages of ~117 MB each, Redis peaked at 52 GB and request-path Redis calls timed
out with no user doing anything (measured by the #1337 load test).

The run flag is a LEASE named by the run's id (``app/utils/redis_lease.py``): the
orchestrator holds it with a heartbeat while it detects and dispatches, each batch renews it
per file with redis-py's owner-checked ``reacquire``, and only the batch that completes THIS
run releases it. A batch of an earlier run whose lease lapsed finds the lease is not its
own and stands down. Before, any batch that saw "processed >= its total" deleted the flag
-- including a left-over batch of an earlier run, counting into the current run's progress
-- so the run ended early and the next 10-minute beat re-dispatched everything.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field

import redis
from redis.lock import Lock

from app.core.pipeline_constants import FILES_PER_BATCH_TASK
from app.utils.redis_lease import adopt_lease_lock

logger = logging.getLogger(__name__)

#: Most speaker UUIDs one batch message may name (per index list). A batch is at most
#: ``FILES_PER_BATCH_TASK`` files AND at most this many speakers, so the message size is a
#: constant (~40 bytes a UUID) no matter how large the library is. A single file with more
#: missing speakers than this is split across consecutive batches.
MAX_SPEAKERS_PER_BATCH = 400


@dataclass
class RepairBatch:
    """The kwargs one repair batch message carries."""

    file_uuids: list[str] = field(default_factory=list)
    missing_v3_uuids: list[str] = field(default_factory=list)
    missing_v4_uuids: list[str] = field(default_factory=list)
    _speakers: int = 0

    def has_room(self, speakers: int) -> bool:
        """True if *speakers* more fit without breaking either bound."""
        return (
            len(self.file_uuids) < FILES_PER_BATCH_TASK
            and self._speakers + speakers <= MAX_SPEAKERS_PER_BATCH
        )

    def add(self, file_uuid: str, speakers: list[str], v3: set[str], v4: set[str]) -> None:
        """Add one file (or one slice of an oversized file's speakers)."""
        self.file_uuids.append(file_uuid)
        self._speakers += len(speakers)
        self.missing_v3_uuids.extend(s for s in speakers if s in v3)
        self.missing_v4_uuids.extend(s for s in speakers if s in v4)


def plan_repair_batches(
    grouped: Mapping[str, list[str]], missing_v3: set[str], missing_v4: set[str]
) -> list[RepairBatch]:
    """Split ``{file_uuid: [missing speaker uuid, ...]}`` into bounded batches.

    Each batch names only the missing speakers of its own files, so a worker writes exactly
    what it would have written when it was handed the whole set (its writers already
    ignore every speaker outside the file they are processing).

    Args:
        grouped: Missing speakers per file (``_group_by_file``).
        missing_v3: Speakers missing from the main index.
        missing_v4: Speakers missing from the v4 index (empty when v4 is not active).

    Returns:
        The batches, in a deterministic order. A file appears more than once only when it
        alone has more than ``MAX_SPEAKERS_PER_BATCH`` missing speakers.
    """
    batches: list[RepairBatch] = []
    current = RepairBatch()
    for file_uuid in sorted(grouped):
        speakers = sorted(grouped[file_uuid])
        for start in range(0, len(speakers), MAX_SPEAKERS_PER_BATCH):
            chunk = speakers[start : start + MAX_SPEAKERS_PER_BATCH]
            if not current.has_room(len(chunk)):
                batches.append(current)
                current = RepairBatch()
            current.add(file_uuid, chunk, missing_v3, missing_v4)
    if current.file_uuids:
        batches.append(current)
    return batches


# ---------------------------------------------------------------------------
# The run's lease
# ---------------------------------------------------------------------------

#: The run flag. Its VALUE is the id of the run that holds it, so every batch can tell
#: whether it still belongs to the current run.
REDIS_LOCK_KEY = "embedding_consistency_running"
#: Lease TTL. The orchestrator renews it from a heartbeat while it detects and dispatches;
#: each batch renews it on every file. It must outlast the wait between two batches on a
#: busy GPU queue; a run that stops renewing (crashed, or stopped) loses it after this.
LOCK_TTL = 7200
#: Longest the orchestrator's heartbeat renews the lease (a wedge backstop).
ORCHESTRATOR_MAX_HOLD = 6 * 3600


def run_lease(client: redis.Redis, run_id: str) -> Lock:
    """The lease of the run named *run_id*, as that run holds it.

    The orchestrator acquires it with ``lease.acquire(token=run_id)``; a batch adopts the
    same token. Renewal and release are redis-py's own owner-checked scripts, so a run that
    lost the lease (it lapsed and a newer run took it) can neither keep nor end the newer one.
    """
    return adopt_lease_lock(client, REDIS_LOCK_KEY, LOCK_TTL, run_id)


def renew_run_lease(lease: Lock) -> bool:
    """Reset the lease TTL; False once the lease is not this run's (lapsed, stopped, taken)."""
    try:
        lease.reacquire()
    except redis.exceptions.LockNotOwnedError:
        return False
    return True


def release_run_lease(lease: Lock) -> bool:
    """End the run: delete the flag only if this run still holds it."""
    try:
        lease.release()
    except redis.exceptions.LockError as exc:
        logger.warning("Consistency run lease was no longer ours at release: %s", exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Files that failed
# ---------------------------------------------------------------------------

#: File uuids whose repair failed in a run (unreadable audio, or no speaker produced a
#: usable embedding). A SCHEDULED check leaves them out for ``FAILED_FILES_TTL``: they
#: stay missing, so without this every 10-minute beat queued them on the GPU again,
#: forever. A manual Repair clears the set and retries them.
FAILED_FILES_KEY = "embedding_consistency:failed_files"
FAILED_FILES_TTL = 86400
#: How many failed file uuids the progress record and its events list. ``failed_count`` is
#: exact; the list used to hold every failure and was rewritten, and sent to the browser,
#: on every file of the run.
FAILED_FILES_SAMPLE = 50


def deferred_failed_files(client: redis.Redis) -> set[str]:
    """Files a scheduled check should leave out (they failed within the last day)."""
    return {
        m.decode() if isinstance(m, bytes) else str(m) for m in client.smembers(FAILED_FILES_KEY)
    }


def forget_failed_files(client: redis.Redis) -> None:
    """A manual repair retries every file."""
    client.delete(FAILED_FILES_KEY)
