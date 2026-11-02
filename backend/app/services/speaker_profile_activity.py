"""When a voiceprint was last *used*: ``speaker_profile.last_interaction_at`` (migration v450).

An interaction is an event that creates, confirms or changes the link between a voiceprint and
a person, or a person's edit of the profile:

* ``human_link`` / ``human_unlink``: a person assigns or unassigns a speaker;
* ``human_edit``: a person edits the profile (name, description, avatar, gender confirmation);
* ``suggestion_decision``: a person accepts or rejects a voiceprint suggestion for it;
* ``auto_link``: an automatic voiceprint match that WRITES a link.

Not interactions: viewing or listing a profile, computing a suggestion, and maintenance
(embedding recompute, cluster rebuild, re-embed, migrations, privacy purge, tenant backfill).
``updated_at`` moves on all of those, which is why it cannot serve as the retention clock.
A profile's creation is its first interaction (the column's ORM default).

``tests/unit/test_profile_interaction_sites.py`` fails when a new site writes a speaker's
``profile_id`` without calling :func:`touch_profile_interaction` or being listed as exempt.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.media import SpeakerProfile

INTERACTION_KINDS = frozenset(
    {"human_link", "human_unlink", "human_edit", "suggestion_decision", "auto_link"}
)

#: Whether an automatic voiceprint link counts. On: a voice that keeps being recognised is in
#: use, so its retention clock restarts. Switch off here if that policy changes.
AUTO_LINK_COUNTS = True


def touch_profile_interaction(
    db: Session, profile_ids: int | Iterable[int | None] | None, kind: str
) -> None:
    """Set ``last_interaction_at = now()`` on the given profiles. The caller commits.

    A Core UPDATE rather than an attribute assignment, so it is one statement however many
    profiles, and it neither depends on nor is confused with ``updated_at``. ``None`` ids
    are ignored (a speaker that was not linked has no profile to touch).
    """
    if kind not in INTERACTION_KINDS:
        raise ValueError(f"unknown profile interaction kind: {kind!r}")
    if kind == "auto_link" and not AUTO_LINK_COUNTS:
        return
    if profile_ids is None:
        return
    raw = [profile_ids] if isinstance(profile_ids, int) else list(profile_ids)
    ids = sorted({int(p) for p in raw if p is not None})
    if not ids:
        return
    db.execute(
        update(SpeakerProfile)
        .where(SpeakerProfile.id.in_(ids))
        .values(last_interaction_at=func.now())
        .execution_options(synchronize_session=False)
    )
