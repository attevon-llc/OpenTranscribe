"""Run-scoped state of one embedding-consistency repair: its batch plan.

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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field

from app.core.pipeline_constants import FILES_PER_BATCH_TASK

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
