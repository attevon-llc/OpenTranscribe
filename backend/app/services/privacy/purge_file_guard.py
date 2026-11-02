"""Which files of a ``files`` purge may actually be purged.

A voiceprint purge destroys data, so it follows the same rule as every other destruction of a
file's content: a file under a legal hold (platform or organization, via ``hold_guard``) or in
quarantine (taken down) is left alone. The caller asks at request time to answer the owner, and
the task asks again at execution time because a hold can be placed between the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.services.privacy.purge import PurgeScope


@dataclass(frozen=True)
class FileGuardResult:
    """``allowed`` may be purged; ``held`` and ``quarantined`` are skipped."""

    allowed: tuple[int, ...]
    held: tuple[int, ...]
    quarantined: tuple[int, ...]


def classify_files(db: Session, file_ids: tuple[int, ...] | list[int]) -> FileGuardResult:
    from app.models.media import MediaFile
    from app.services.hold_guard import hold_states

    ids = sorted({int(i) for i in file_ids})
    held = set(hold_states(db, ids))
    quarantined = {
        int(r[0])
        for r in db.execute(
            select(MediaFile.id).where(MediaFile.id.in_(ids), MediaFile.is_quarantined.is_(True))
        )
    }
    return FileGuardResult(
        allowed=tuple(i for i in ids if i not in held and i not in quarantined),
        held=tuple(sorted(held)),
        quarantined=tuple(sorted(quarantined - held)),
    )


def guard_files_scope(db: Session, scope: PurgeScope) -> PurgeScope | None:
    """``scope`` narrowed to purgeable files; ``None`` when none remain. Other kinds pass through."""
    if scope.kind != "files":
        return scope
    from app.services.privacy.purge import PurgeScope as _PurgeScope

    allowed = classify_files(db, scope.file_ids).allowed
    if not allowed:
        return None
    if allowed == scope.file_ids:
        return scope
    return _PurgeScope(kind="files", file_ids=allowed)
