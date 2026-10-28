"""Which recordings an admin voiceprint migration will skip because their scope forbids it.

An embedding migration or consistency repair writes voiceprints, and a (user, organization)
scope whose voiceprint switch is off must not get one (plan D5, Q3): the batch skips those
files and reports ``skipped_by_policy``, so after the index swap that scope has none. This
module answers the same question BEFORE the run starts, so an admin is warned instead of
finding out from the report, and so a request that could do nothing at all is refused (409)
rather than reported as an empty success.

The decision per scope is the policy's own (:class:`PrivacyView`), judged for the file's
owner and organization, never for the admin who asks.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from app.services.privacy.policy import Layer
from app.services.privacy.redact_view import PrivacyView

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class MigrationScopeSummary:
    """Completed recordings split by whether their scope allows a voiceprint."""

    files_total: int = 0
    files_skipped: int = 0
    scopes_total: int = 0
    scopes_skipped: int = 0
    #: The layers that forbid it, deduplicated, in first-seen order.
    locked_by: tuple[Layer, ...] = field(default_factory=tuple)

    @property
    def any_skipped(self) -> bool:
        return self.files_skipped > 0

    @property
    def all_skipped(self) -> bool:
        """Every recording is in a forbidden scope: the run could write nothing."""
        return self.files_total > 0 and self.files_skipped == self.files_total

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_total": self.files_total,
            "files_skipped": self.files_skipped,
            "scopes_total": self.scopes_total,
            "scopes_skipped": self.scopes_skipped,
            "locked_by": list(self.locked_by),
        }


def summarize_migration_scopes(db: Session, user_id: int | None = None) -> MigrationScopeSummary:
    """Count the completed recordings a migration would skip by policy.

    Args:
        db: Session, read only.
        user_id: Restrict to one owner, like the migration's own ``user_id`` argument.

    Returns:
        The summary. A scope whose policy cannot be read counts as forbidden (the policy
        fails closed), which is what the batch itself will do.
    """
    from sqlalchemy import func

    from app.models.media import FileStatus
    from app.models.media import MediaFile

    query = db.query(MediaFile.user_id, MediaFile.organization_id, func.count()).filter(
        MediaFile.status == FileStatus.COMPLETED
    )
    if user_id is not None:
        query = query.filter(MediaFile.user_id == user_id)
    rows = query.group_by(MediaFile.user_id, MediaFile.organization_id).all()

    view = PrivacyView(db)
    files_total = files_skipped = scopes_skipped = 0
    layers: list[Layer] = []
    for owner, org, count in rows:
        files_total += int(count)
        decision = view.voiceprints_for_scope(int(owner), int(org) if org is not None else None)
        if decision.enabled:
            continue
        files_skipped += int(count)
        scopes_skipped += 1
        if decision.locked_by is not None and decision.locked_by not in layers:
            layers.append(decision.locked_by)
    return MigrationScopeSummary(
        files_total=files_total,
        files_skipped=files_skipped,
        scopes_total=len(rows),
        scopes_skipped=scopes_skipped,
        locked_by=tuple(layers),
    )
