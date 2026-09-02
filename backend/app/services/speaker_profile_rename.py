"""Apply a speaker-profile rename to the profile's member speakers (issue #675).

A ``SpeakerProfile`` is the corpus-wide identity; a ``Speaker`` is one file's
diarized voice linked to it. **The profile's name is not indexed anywhere.**
``transcript_chunks`` carries ``canonical_speaker_label_for_row(speaker)``
(``app/utils/speaker_labels.py``) — resolved from ``Speaker.display_name`` /
``suggested_name`` / ``name`` — and neither chunk-index writer
(``tasks/search_indexing_task.resolve_chunk_speaker_name``,
``tasks/reindex_task._resolve_reindex_speaker_name``) consults a profile at all.

A profile rename therefore reaches the index only through the invariant every
other linking path already upholds: **a speaker linked to a profile carries the
profile's name as its display name.** ``SpeakerClusteringService`` writes
``speaker.display_name = profile.name`` on cluster promotion and on a batch
``assign``; ``speakers.py``'s ``update_profile`` action rewrites every member on
rename. So re-applying the name and dispatching the chunk-plane propagation are
one unit: dispatching without the rewrite would push a name into the index that
Postgres does not hold, and the next reindex would revert it.

This module is that unit's single implementation. It exists because there are
**two** endpoints that rename a profile — ``PUT /speakers/{uuid}`` with
``profile_action="update_profile"`` and ``PUT /speaker-profiles/profiles/{uuid}``
— and only the first had it (issue #675). A second copy is how the two would
drift; note that the first one had already drifted once, in issue #605, over
which resolver computes the old name.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.media import Speaker
from app.utils.speaker_labels import canonical_speaker_label_for_row

logger = logging.getLogger(__name__)


def apply_profile_name_to_speakers(
    db: Session,
    profile_id: int,
    new_name: str,
    *,
    restrict_to_user_id: int | None = None,
) -> list[tuple[str, str]]:
    """Rewrite every member speaker's display name, reporting the names it replaced.

    **Collects before it overwrites.** The returned old names are what the chunk
    documents were indexed with, and this is the last moment they exist anywhere:
    once the caller commits, Postgres cannot say what they were (issue #405).
    They are resolved with ``canonical_speaker_label_for_row`` — the SAME resolver
    the chunk-index writers use — because a member with no ``display_name`` but a
    confident ``suggested_name`` is indexed under the *suggestion*, and a member
    with neither is indexed under the raw diarizer label. Keying on the profile's
    own previous name would match nothing for either (issue #605).

    Writes only; the caller owns the commit **and** the dispatch, because a
    rolled-back rename must never reach the index.

    Args:
        db: Open session. The profile itself is not read here — the caller has
            already resolved and authorized it.
        profile_id: The renamed profile.
        new_name: Its new name, which becomes every member's display name.
        restrict_to_user_id: When set, only that user's speakers are rewritten.
            A profile can be shared, so its members may belong to other accounts;
            ``None`` (admin) sweeps all of them. This mirrors the authorization
            the ``update_profile`` action has applied since #284.

    Returns:
        ``(file_uuid, old_canonical_label)`` pairs ready for
        ``dispatch_speaker_rename``, which coalesces them per file. Empty when the
        profile has no members — distinct from "no such profile", which the caller
        decides.
    """
    linked_query = db.query(Speaker).filter(Speaker.profile_id == profile_id)
    if restrict_to_user_id is not None:
        linked_query = linked_query.filter(Speaker.user_id == restrict_to_user_id)
    linked_speakers = linked_query.all()

    # One grouped lookup, not a lazy `speaker.media_file.uuid` per row: a
    # well-used profile spans dozens of files.
    file_uuids: dict[int, str] = {}
    media_file_ids = {int(s.media_file_id) for s in linked_speakers if s.media_file_id}
    if media_file_ids:
        file_uuids = {
            int(row[0]): str(row[1])
            for row in db.query(MediaFile.id, MediaFile.uuid).filter(
                MediaFile.id.in_(media_file_ids)
            )
        }

    renames: list[tuple[str, str]] = []
    for speaker in linked_speakers:
        old_chunk_name = canonical_speaker_label_for_row(speaker)
        file_uuid = file_uuids.get(int(speaker.media_file_id or 0))
        if file_uuid and old_chunk_name:
            renames.append((file_uuid, old_chunk_name))
        speaker.display_name = new_name  # type: ignore[assignment]

    logger.info(
        f"Applied profile {profile_id} name '{new_name}' to {len(linked_speakers)} speaker(s)"
    )
    return renames
