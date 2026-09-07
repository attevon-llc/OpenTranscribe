"""Collect speaker renames made deep inside a service and dispatch them after commit.

Issue #432, the follow-up to #405. The API rename paths #405 fixed each rename
**one** speaker and know its file's UUID on the spot, so they can call
``dispatch_speaker_rename`` inline. The service-layer writers cannot:

* ``SpeakerMatchingService._handle_speaker_match`` renames from inside a loop the
  caller commits.
* ``SpeakerClusteringService`` renames **many** speakers across **many** files in
  one pass — cluster promotion and batch verify both do — and holds
  ``media_file_id``, not the UUID the chunk index is keyed by.

Dispatching per row would mean one ``update_by_query`` per speaker per file; each
would rewrite the same file-level ``speakers`` array and lose to the next on
version conflict. So renames are *recorded* as they happen and flushed once, after
the commit that makes them real — resolving every UUID in a single query and
handing ``dispatch_speaker_rename`` the whole batch so it can coalesce per file.

**Record before the overwrite.** The old name is what the chunks were indexed
with — resolved via ``canonical_speaker_label_for_row``
(``app/utils/speaker_labels.py``), the SAME resolver the chunk-index writers use,
not the ad hoc ``display_name or name`` chain this module used before issue #605.
Once the new value is committed, Postgres cannot say what it used to be.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class SpeakerRenameTracker:
    """Accumulates ``(media_file_id, old_name, new_name)`` renames for one pass.

    Not thread-safe and not meant to be: one tracker belongs to one service
    instance, which belongs to one session.
    """

    def __init__(self) -> None:
        self._pending: list[tuple[int, str, str]] = []

    def record(self, media_file_id: int | None, old_name: str | None, new_name: str | None) -> None:
        """Note that ``old_name`` in ``media_file_id`` just became ``new_name``.

        Call this **before** the write. Entries that cannot rewrite anything
        (missing file, missing name, or a rename to the name already indexed) are
        dropped here rather than queuing a task that would no-op.
        """
        if not media_file_id or not old_name or not new_name or old_name == new_name:
            return
        self._pending.append((int(media_file_id), str(old_name), str(new_name)))

    @property
    def pending(self) -> list[tuple[int, str, str]]:
        """The unflushed renames — for assertions and logging, not mutation."""
        return list(self._pending)

    def discard(self) -> None:
        """Forget everything recorded so far — the transaction rolled back.

        Without this a later flush on the same service instance would propagate
        a name Postgres never kept, leaving the index the only place it exists.
        """
        self._pending = []

    def flush(self, db: Session) -> int:
        """Queue chunk-plane propagation for everything recorded so far.

        Call **after** the commit: a rolled-back rename must never reach the
        index. Clears the buffer first, so a failed dispatch cannot re-queue the
        same renames on the next flush.

        Best-effort by design, exactly like the #405 call sites: the rename is
        already durable in Postgres and an unreachable broker must not turn it
        into a caller-visible failure. The chunk plane then stays stale until the
        next reindex — the pre-#405 behaviour.

        Args:
            db: Session used to resolve ``media_file_id`` -> ``uuid``.

        Returns:
            Number of files a propagation task was queued for.
        """
        pending = self._pending
        self._pending = []
        if not pending:
            return 0

        try:
            from app.models.media import MediaFile
            from app.tasks.rename_propagation_task import dispatch_speaker_rename

            file_ids = {file_id for file_id, _, _ in pending}
            uuids = {
                int(row[0]): str(row[1])
                for row in db.query(MediaFile.id, MediaFile.uuid)
                .filter(MediaFile.id.in_(file_ids))
                .all()
            }

            # Grouped by the NEW name because dispatch_speaker_rename applies one
            # name to the whole batch. A clustering pass renaming two people in
            # the same file is two groups, and each still coalesces per file.
            by_new_name: dict[str, list[tuple[str | None, str | None]]] = {}
            for file_id, old_name, new_name in pending:
                by_new_name.setdefault(new_name, []).append((uuids.get(file_id), old_name))

            queued = 0
            for new_name, renames in by_new_name.items():
                queued += dispatch_speaker_rename(renames, new_name)
            if queued:
                logger.info(f"Queued chunk speaker-rename propagation for {queued} file(s)")
            return queued
        except Exception as exc:  # noqa: BLE001 — a rename must not fail on dispatch
            logger.warning(f"Could not queue chunk-plane speaker rename propagation: {exc}")
            return 0
