"""Did a file's speaker labels or tags change while a full index of it was running?

A full index (``index_transcript_search_task``, ``reindex_batch``) reads a file's speaker
labels and tags from Postgres, then spends seconds to minutes chunking and embedding before
its bulk write lands. A rename or a tag change committed in that window is rewritten into the
index by its own ``update_by_query`` (``propagate_speaker_rename``, ``update_file_tags_index``),
but that rewrite matches the documents that exist at the time: the old ones, which the bulk
then replaces with the snapshot. The change is in Postgres and nowhere in search, and nothing
re-examines the file until somebody reindexes it.

The indexer reads :class:`IndexInputs` before its snapshot and calls
:func:`requeue_if_inputs_moved` after its write. Postgres is compared with Postgres, read the
same way both times, so a file whose inputs did not move is never requeued (a rebuild cannot
requeue itself forever). When they moved, the file's debounced rebuild is queued through
``reindex_dispatch.queue_transcript_reindex``, which reads every field again when it runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.services.search import reindex_dispatch

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexInputs:
    """The per-file values every chunk document carries that a user edits in place."""

    speaker_labels: frozenset[str]
    tags: tuple[str, ...]


def read_index_inputs(
    db: Any, file_id: int, *, voiceprint_names: bool | None = None
) -> IndexInputs:
    """The file's speaker labels (as the policy implies them now) and tag names, from Postgres.

    Read this BEFORE the indexer's own snapshot: a change landing between the two reads then
    shows up as moved (one extra rebuild) instead of being missed. ``voiceprint_names`` is the
    file's decision when the caller already asked the policy (``None``: asked here).
    """
    from app.models.tag import FileTag
    from app.models.tag import Tag
    from app.services.privacy.label_reindex import expected_speaker_labels

    decided = None if voiceprint_names is None else {file_id: voiceprint_names}
    labels = expected_speaker_labels(db, [file_id], decided=decided).get(file_id, set())
    tags = (
        db.query(Tag.name)
        .join(FileTag, FileTag.tag_id == Tag.id)
        .filter(FileTag.media_file_id == file_id)
        .all()
    )
    return IndexInputs(frozenset(labels), tuple(sorted(str(name) for (name,) in tags)))


def requeue_if_inputs_moved(
    *, file_id: int, file_uuid: str, user_id: int, before: IndexInputs
) -> reindex_dispatch.ReindexDispatch | None:
    """Queue the file's rebuild when its labels or tags differ from ``before``.

    Never raises: the index write already happened, and an unreadable Postgres here is logged
    rather than turned into a failed index.

    Returns:
        ``None`` when nothing moved (or Postgres could not be read), else the dispatch outcome.
    """
    from app.db.session_utils import session_scope

    try:
        with session_scope() as db:
            now = read_index_inputs(db, file_id)
    except SQLAlchemyError:
        logger.exception(f"Could not re-read the index inputs of file {file_uuid}")
        return None
    if now == before:
        return None
    outcome = reindex_dispatch.queue_transcript_reindex(
        file_id=file_id, file_uuid=file_uuid, user_id=user_id
    )
    logger.info(
        f"Speaker labels or tags of file {file_uuid} changed during its index; "
        f"rebuild {outcome.value}"
    )
    return outcome
