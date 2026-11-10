"""Re-verify the voice-made stamps the v449 upgrade wrote, and report what it left (issue #1334 item 1).

The v449 backfill marked an existing speaker identity ``voiceprint`` only with positive evidence
that the matcher wrote it (the auto-accept shape, a profile older than the speaker, and another
voice linked to that profile earlier): see ``alembic/versions/v449_add_speaker_identity_source.py``.
One shape still passes that test without being the machine's: a person who assigned an EXISTING
profile to a later speaker whose voiceprint was then re-extracted. A voiceprint purge clears a
stamped name, and nothing restores it, so an operator needs a way to see these rows and to put
them back to "unknown" (NULL, read as a person's, never hidden and never purged).

What :func:`reverify_identity_sources` measures, inside an optional organization or user scope:

* ``backfill_stamped``: rows still carrying the backfill's stamp (``identity_source =
  'voiceprint'`` with ``identity_source_detail = {"origin": "v449_backfill"}``). Any later write
  through the identity helpers replaces that detail, so these rows are exactly the ones no one
  has touched since the upgrade. Each is re-tested against the backfill's evidence on the data
  as it is NOW:

  * ``evidence_holds``: the shape and the evidence still hold;
  * ``evidence_lost``: they do not (the earlier voice was deleted or merged away, the suggestion
    or the profile's name changed, ...). The stamp rests on nothing any more.

* ``shape_left_unmarked``: unmarked rows with the auto-accept shape, as v449 counted them, and
  ``shape_left_unmarked_with_evidence``, those of them that now also have the evidence. Reported
  only: stamping on the shape is the mistake the backfill was narrowed to avoid, and mistaking a
  person's name for a machine's is the costly direction.
* ``summaries_to_review``: completed files with a stored summary and a speaker whose name a
  voiceprint wrote (any ``voiceprint`` row). A summary is model output stored as written (item 3);
  switching voiceprints off masks the name in the index and chat, not in the stored text, so these
  are the summaries an administrator may want to regenerate while voiceprints are off.
* ``recorded``: the counts v449 itself recorded in ``system_settings``
  (``privacy.identity_source_backfill``), for comparison. Never rewritten here.

Dry run by default. With ``apply`` it clears the stamp of the ``evidence_lost`` rows, or of every
``backfill_stamped`` row with ``include_evidenced`` (an operator who would rather keep every
pre-0.6.0 name than let a purge clear an ambiguous one). Speakers of a file under a legal hold or
in quarantine are never cleared (``skipped_protected``): a hold freezes the file as it stands and
every background write skips a frozen file (``hold_guard``), and clearing a stamp can change the
label the held file shows. Clearing is the backfill's own
"unknown": the columns :func:`~app.utils.speaker_identity_source.cleared_provenance_values`
nulls, the name and link untouched. Each cleared row gets a ``confirmed`` identity-history row
(``origin: identity_reverify``, a system actor); the affected files are re-indexed so a name that
was hidden while voiceprints are off becomes searchable again. Every run, dry or not, writes one
``privacy.identity_source.reverify`` audit event with the counts and the ids of the speakers it
cleared (never a name).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy import and_
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased

from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.models.system_settings import SystemSettings
from app.services.speaker_identity_history import record_bulk_identity_events
from app.utils.speaker_identity_detail import ORIGIN_V449_BACKFILL
from app.utils.speaker_identity_source import BACKFILL_RECORD_SETTING_KEY
from app.utils.speaker_identity_source import VOICEPRINT
from app.utils.speaker_identity_source import cleared_provenance_values

logger = logging.getLogger(__name__)

#: ``origin`` of the identity-history row a cleared stamp gets.
ORIGIN_IDENTITY_REVERIFY = "identity_reverify"
#: Speaker uuids listed per category in the report and the audit event; counts are always exact.
LISTED_IDS_CAP = 1000


def shape_clause() -> ColumnElement[bool]:
    """The v449 auto-accept shape (``v449.SHAPE_WHERE`` without its ``identity_source IS NULL``,
    so it also applies to the rows the backfill stamped).

    ``tests/unit/test_identity_source_reverify.py`` pins this and :func:`evidence_clause` to the
    migration's SQL on the same rows.
    """
    return and_(
        Speaker.suggestion_source == "profile_match",
        Speaker.verified.is_(True),
        Speaker.profile_id.isnot(None),
        func.coalesce(Speaker.suggested_name, "") != "",
        Speaker.display_name == Speaker.suggested_name,
        exists().where(
            SpeakerProfile.id == Speaker.profile_id, SpeakerProfile.name == Speaker.display_name
        ),
    )


def evidence_clause() -> ColumnElement[bool]:
    """The v449 evidence: the profile existed before the speaker and another voice was linked to
    it first. NULL timestamps prove nothing and fail it."""
    other = aliased(Speaker)
    return exists().where(
        SpeakerProfile.id == Speaker.profile_id,
        Speaker.created_at > SpeakerProfile.created_at,
        exists().where(
            other.profile_id == SpeakerProfile.id,
            other.id != Speaker.id,
            other.created_at < Speaker.created_at,
        ),
    )


def backfill_stamp_clause() -> ColumnElement[bool]:
    """Rows still carrying the backfill's own stamp, untouched since."""
    return and_(
        Speaker.identity_source == VOICEPRINT,
        Speaker.identity_source_detail["origin"].astext == ORIGIN_V449_BACKFILL,
    )


@dataclass(frozen=True)
class ReverifyScope:
    """Which speakers a run covers: everything, one organization, or one user's."""

    organization_id: int | None = None
    user_id: int | None = None

    @property
    def kind(self) -> str:
        if self.user_id is not None:
            return "user"
        return "organization" if self.organization_id is not None else "instance"

    def filters(self) -> list[ColumnElement[bool]]:
        """Conditions on ``Speaker`` that keep a query inside the scope."""
        found: list[ColumnElement[bool]] = []
        if self.organization_id is not None:
            # The tenant is the FILE's: the pipeline never stamps speaker.organization_id.
            found.append(
                Speaker.media_file_id.in_(
                    select(MediaFile.id).where(MediaFile.organization_id == self.organization_id)
                )
            )
        if self.user_id is not None:
            found.append(Speaker.user_id == self.user_id)
        return found


@dataclass
class ReverifyReport:
    """What one run measured and did. ``*_uuids`` lists are capped at :data:`LISTED_IDS_CAP`."""

    scope: ReverifyScope
    dry_run: bool
    include_evidenced: bool
    backfill_stamped: int = 0
    evidence_holds: int = 0
    evidence_lost: int = 0
    shape_left_unmarked: int = 0
    shape_left_unmarked_with_evidence: int = 0
    summaries_to_review: int = 0
    cleared: int = 0
    skipped_protected: int = 0
    reindex_queue_failed: int = 0
    recorded: dict[str, Any] | None = None
    evidence_lost_uuids: list[str] = field(default_factory=list)
    evidence_holds_uuids: list[str] = field(default_factory=list)
    cleared_uuids: list[str] = field(default_factory=list)
    summary_file_uuids: list[str] = field(default_factory=list)
    reindex_failed_file_uuids: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "backfill_stamped": self.backfill_stamped,
            "evidence_holds": self.evidence_holds,
            "evidence_lost": self.evidence_lost,
            "shape_left_unmarked": self.shape_left_unmarked,
            "shape_left_unmarked_with_evidence": self.shape_left_unmarked_with_evidence,
            "summaries_to_review": self.summaries_to_review,
            "cleared": self.cleared,
            "skipped_protected": self.skipped_protected,
            "reindex_queue_failed": self.reindex_queue_failed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "include_evidenced": self.include_evidenced,
            "scope": self.scope.kind,
            "organization_id": self.scope.organization_id,
            "user_id": self.scope.user_id,
            "counts": self.counts(),
            "recorded": self.recorded,
            "evidence_lost_speaker_uuids": self.evidence_lost_uuids,
            "evidence_holds_speaker_uuids": self.evidence_holds_uuids,
            "cleared_speaker_uuids": self.cleared_uuids,
            "summary_file_uuids": self.summary_file_uuids,
            "reindex_failed_file_uuids": self.reindex_failed_file_uuids,
        }


def _recorded(db: Session) -> dict[str, Any] | None:
    raw = db.execute(
        select(SystemSettings.value).where(SystemSettings.key == BACKFILL_RECORD_SETTING_KEY)
    ).scalar_one_or_none()
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return {"unreadable": True}
    return value if isinstance(value, dict) else None


def _measure(db: Session, report: ReverifyReport) -> list[tuple[int, bool]]:
    """Fill the counts and lists; return ``(speaker id, evidence holds)`` for each stamped row."""
    scope = report.scope.filters()
    stamped = db.execute(
        select(Speaker.id, Speaker.uuid, and_(shape_clause(), evidence_clause()))
        .where(backfill_stamp_clause(), *scope)
        .order_by(Speaker.id)
    ).all()
    rows: list[tuple[int, bool]] = []
    for speaker_id, speaker_uuid, evidenced in stamped:
        rows.append((int(speaker_id), bool(evidenced)))
        listed = report.evidence_holds_uuids if evidenced else report.evidence_lost_uuids
        if len(listed) < LISTED_IDS_CAP:
            listed.append(str(speaker_uuid))
    report.backfill_stamped = len(rows)
    report.evidence_holds = sum(1 for _, evidenced in rows if evidenced)
    report.evidence_lost = report.backfill_stamped - report.evidence_holds

    unmarked, with_evidence = db.execute(
        select(func.count(), func.count().filter(evidence_clause())).where(
            Speaker.identity_source.is_(None), shape_clause(), *scope
        )
    ).one()
    report.shape_left_unmarked = int(unmarked or 0)
    report.shape_left_unmarked_with_evidence = int(with_evidence or 0)

    files = db.execute(
        select(MediaFile.id, MediaFile.uuid)
        .where(
            MediaFile.summary_data.isnot(None),
            exists().where(
                Speaker.media_file_id == MediaFile.id, Speaker.identity_source == VOICEPRINT, *scope
            ),
        )
        .order_by(MediaFile.id)
    ).all()
    report.summaries_to_review = len(files)
    report.summary_file_uuids = [str(row[1]) for row in files[:LISTED_IDS_CAP]]
    report.recorded = _recorded(db)
    return rows


def _on_unprotected_file() -> ColumnElement[bool]:
    """``Speaker`` belongs to a file nobody holds and that is not quarantined."""
    from app.services.privacy.purge_file_guard import unprotected_file_pred

    return Speaker.media_file_id.in_(select(MediaFile.id).where(unprotected_file_pred()))


def _count_protected(db: Session, speaker_ids: list[int]) -> int:
    """How many of ``speaker_ids`` sit on a held or quarantined file."""
    if not speaker_ids:
        return 0
    return int(
        db.execute(
            select(func.count()).where(Speaker.id.in_(speaker_ids), ~_on_unprotected_file())
        ).scalar_one()
    )


def _clear(db: Session, speaker_ids: list[int], *, reason: str) -> list[tuple[int, str]]:
    """Clear the backfill stamp of these rows and record each in the identity history. No commit.

    The ``WHERE`` repeats the stamp test, so a row a person or the matcher rewrote between the
    measurement and this statement keeps its new record, and the protection test, so a speaker of
    a file held or quarantined since is left as it is.
    """
    if not speaker_ids:
        return []
    cleared = [
        (int(row[0]), str(row[1]))
        for row in db.execute(
            update(Speaker)
            .where(Speaker.id.in_(speaker_ids), backfill_stamp_clause(), _on_unprotected_file())
            .values(**cleared_provenance_values())
            .returning(Speaker.id, Speaker.uuid)
            .execution_options(synchronize_session=False)
        )
    ]
    record_bulk_identity_events(
        db,
        [speaker_id for speaker_id, _ in cleared],
        kind="confirmed",
        user_id=None,
        detail={
            "origin": ORIGIN_IDENTITY_REVERIFY,
            "previous_origin": ORIGIN_V449_BACKFILL,
            "reason": reason,
        },
    )
    return cleared


def _queue_reindexes(files: list[tuple[int, str, int]]) -> list[str]:
    """Queue each file's (debounced) transcript reindex; return the uuids that failed to queue.

    A cleared stamp moves the file out of the label-drift scan (``label_reindex``), which only
    looks at files a voiceprint names, so a failed file is reported rather than left to it.
    """
    from app.services.search.reindex_dispatch import ReindexDispatch
    from app.services.search.reindex_dispatch import queue_transcript_reindex

    failed: list[str] = []
    for file_id, file_uuid, user_id in files:
        outcome = queue_transcript_reindex(file_id=file_id, file_uuid=file_uuid, user_id=user_id)
        if outcome is ReindexDispatch.FAILED:
            failed.append(file_uuid)
    return failed


def _audit(report: ReverifyReport, outcome: AuditOutcome) -> None:
    audit_logger.log(
        event_type=AuditEventType.PRIVACY_IDENTITY_REVERIFY,
        outcome=outcome,
        organization_id=report.scope.organization_id,
        target_user_id=report.scope.user_id,
        details={
            "phase": "dry_run" if report.dry_run else "applied",
            "scope": report.scope.kind,
            "organization_id": report.scope.organization_id,
            "user_id": report.scope.user_id,
            "include_evidenced": report.include_evidenced,
            "counts": report.counts(),
            "recorded": report.recorded,
            "cleared_speaker_uuids": report.cleared_uuids,
            "cleared_speaker_uuids_truncated": report.cleared > len(report.cleared_uuids),
            "reindex_failed_file_uuids": report.reindex_failed_file_uuids,
        },
    )


def reverify_identity_sources(
    db: Session,
    *,
    scope: ReverifyScope | None = None,
    apply: bool = False,
    include_evidenced: bool = False,
    reindex: Callable[[list[tuple[int, str, int]]], list[str]] | None = None,
) -> ReverifyReport:
    """Measure the backfill's stamps in ``scope`` and, with ``apply``, clear the unfounded ones.

    Args:
        db: Session. Commits when ``apply`` is set; a dry run only reads.
        scope: Organization and/or user to cover; everything when omitted.
        apply: Clear stamps (default: dry run, nothing written but the audit event).
        include_evidenced: With ``apply``, clear every backfill stamp, not only those whose
            evidence no longer holds.
        reindex: Queues the transcript reindex of each affected ``(file_id, file_uuid,
            owner_user_id)`` and returns the uuids of the files it could NOT queue (default
            :func:`_queue_reindexes`); a parameter so a test need not stand up a broker.

    Returns:
        The report; also audited as ``privacy.identity_source.reverify``.
    """
    from app.services.privacy.purge import refresh_cleared_speakers

    report = ReverifyReport(
        scope=scope or ReverifyScope(), dry_run=not apply, include_evidenced=include_evidenced
    )
    try:
        rows = _measure(db, report)
        targets = [sid for sid, evidenced in rows if include_evidenced or not evidenced]
        report.skipped_protected = _count_protected(db, targets)
        if apply:
            reason = "operator_cleared" if include_evidenced else "evidence_lost"
            cleared = _clear(db, targets, reason=reason)
            db.commit()
            report.cleared = len(cleared)
            report.cleared_uuids = [u for _, u in cleared[:LISTED_IDS_CAP]]
            if cleared:

                def queue(files: list[tuple[int, str, int]]) -> int:
                    failed = (reindex or _queue_reindexes)(files)
                    report.reindex_failed_file_uuids.extend(failed[:LISTED_IDS_CAP])
                    return len(failed)

                refreshed = refresh_cleared_speakers(db, [sid for sid, _ in cleared], queue)
                report.reindex_queue_failed = refreshed["reindex_queue_failed"]
    except Exception:
        db.rollback()
        logger.exception("speaker identity re-verify failed")
        _audit(report, AuditOutcome.FAILURE)
        raise
    _audit(report, AuditOutcome.PARTIAL if report.reindex_queue_failed else AuditOutcome.SUCCESS)
    return report
