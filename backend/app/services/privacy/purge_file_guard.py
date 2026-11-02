"""Which files of a ``files`` purge may actually be purged.

A voiceprint purge destroys data, so it follows the same rule as every other destruction of a
file's content: a file under a legal hold (platform or organization, via ``hold_guard``) or in
quarantine (taken down) is left alone. The caller asks at request time to answer the owner, and
the task asks again at execution time because a hold can be placed between the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import and_
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement
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


# --- The same rule as a SQL predicate, for the scopes that select by tenant, not by id ----


def protected_file_pred() -> ColumnElement[bool]:
    """``MediaFile`` is held (platform or organization, ``hold_guard``) or quarantined."""
    from app.models.media import MediaFile
    from app.services.hold_guard import held_pred

    return or_(held_pred(), MediaFile.is_quarantined.is_(True))


def unprotected_file_pred() -> ColumnElement[bool]:
    """``MediaFile`` is held by nobody and not quarantined: a purge may touch its voiceprints."""
    from app.models.media import MediaFile
    from app.services.hold_guard import not_held_pred

    return and_(not_held_pred(), MediaFile.is_quarantined.is_(False))


def profile_keeps_protected_voice() -> ColumnElement[bool]:
    """EXISTS a speaker of a protected file linked to the enclosing query's ``SpeakerProfile``."""
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.models.media import SpeakerProfile

    return exists().where(
        Speaker.profile_id == SpeakerProfile.id,
        Speaker.media_file_id.in_(select(MediaFile.id).where(protected_file_pred())),
    )


@dataclass(frozen=True)
class ScopeProtection:
    """What a tenant-wide purge leaves alone. Counts only: a hold's uuid never appears here.

    ``files_held`` / ``files_quarantined`` count files with speakers (held takes precedence, as
    in :func:`classify_files`); ``profiles_retained`` the profiles that keep one of their voices.
    """

    files_held: int = 0
    files_quarantined: int = 0
    profiles_retained: int = 0

    @property
    def any(self) -> bool:
        return bool(self.files_held or self.files_quarantined or self.profiles_retained)

    def counts(self) -> dict[str, int]:
        """The audit/status counters (keys in :data:`PROTECTION_COUNT_KEYS`)."""
        return {
            "files_skipped_held": self.files_held,
            "files_skipped_quarantined": self.files_quarantined,
            "profiles_retained": self.profiles_retained,
        }


PROTECTION_COUNT_KEYS = ("files_skipped_held", "files_skipped_quarantined", "profiles_retained")


def scope_protection(db: Session, scope: PurgeScope) -> ScopeProtection:
    """Held / quarantined files and retained profiles inside a tenant scope's reach.

    Always empty for a ``files`` scope (it is narrowed by id, :func:`guard_files_scope`).
    """
    if scope.kind == "files":
        return ScopeProtection()
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.models.media import SpeakerProfile
    from app.services.hold_guard import held_pred

    has_speaker = exists().where(Speaker.media_file_id == MediaFile.id)
    tenant = scope.tenant_filters(MediaFile)

    def files(*conditions: ColumnElement[bool]) -> int:
        return int(
            db.execute(
                select(func.count()).select_from(MediaFile).where(*tenant, has_speaker, *conditions)
            ).scalar_one()
        )

    retained = int(
        db.execute(
            select(func.count())
            .select_from(SpeakerProfile)
            .where(*scope.tenant_filters(SpeakerProfile), profile_keeps_protected_voice())
        ).scalar_one()
    )
    return ScopeProtection(
        files_held=files(held_pred()),
        files_quarantined=files(~held_pred(), MediaFile.is_quarantined.is_(True)),
        profiles_retained=retained,
    )
