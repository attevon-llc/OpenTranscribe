"""``held_file_ids()`` and ``held_pred()`` are two spellings of one rule and must not drift.

``held_file_ids`` is a UNION (platform flag, unreleased hold row) so the gallery's ``held=``
filter becomes a semi-join instead of a per-row SubPlan (issue #1337, G8). That makes it a second
definition of "held" beside ``held_pred``; this pins them to the same set across every case:
platform flag, organization hold, a released hold, both authorities at once, and no hold.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.media import MediaFile
from app.services import content_hold_service as holds
from app.services.hold_guard import held_file_ids
from app.services.hold_guard import held_pred

REASON = "Matter 2026-31 (equivalence check)"


def _place(gov, *files: MediaFile):
    change = holds.place_file_hold(
        gov.db, org_id=gov.org.id, actor=gov.admin, files=list(files), reason=REASON
    )
    gov.db.commit()
    return change.hold


def test_held_file_ids_selects_exactly_the_files_held_pred_accepts(gov):
    free, org_held, platform_held, released, both = (gov.file(gov.alice) for _ in range(5))
    _place(gov, org_held, both)
    released_hold = _place(gov, released)
    holds.release_hold(
        gov.db, org_id=gov.org.id, actor=gov.admin, hold=released_hold, reason=REASON
    )
    for media_file in (platform_held, both):
        media_file.legal_hold = True
    gov.db.commit()
    ids = [f.id for f in (free, org_held, platform_held, released, both)]

    by_union = set(
        gov.db.scalars(
            select(MediaFile.id).where(MediaFile.id.in_(ids), MediaFile.id.in_(held_file_ids()))
        )
    )
    by_pred = set(gov.db.scalars(select(MediaFile.id).where(MediaFile.id.in_(ids), held_pred())))

    assert by_union == by_pred
    assert by_pred == {org_held.id, platform_held.id, both.id}, (
        "control: one file per case; the free and released files must not be held"
    )
