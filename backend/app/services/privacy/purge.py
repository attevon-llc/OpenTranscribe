"""Purge: delete the voiceprints and inferred gender a privacy switch only hides (issue #1280).

Turning a layer off hides data and never deletes it (``policy`` module docstring). This module is
the separate, explicit, audited deletion, and it is irreversible by design. Three scopes reach it
(``PurgeScope``):

* ``organization``: everything stamped with one organization, all members' (an organization admin).
* ``user``: one person's own data in one tenant (organization or personal workspace).
* ``instance_personal`` / ``instance_all``: every personal workspace, or literally everything (a
  super_admin; ``instance_all`` is refused in a multi-tenant deployment by the caller).

What is deleted
---------------
``voiceprints``: the speaker and profile embedding documents in every speaker index (the legacy
backup index included, exactly as erasure does); ``speaker_profile.embedding_count`` and
``last_embedding_update`` are reset; ``speaker_match`` rows and unpromoted ``speaker_cluster`` rows
(with their members) in scope go, and ``speaker.cluster_id`` is cleared; unverified voiceprint
suggestions are cleared and the speaker's derived status and label are refreshed. Kept: profiles,
names, avatars, user-verified speaker-to-profile links and promoted clusters (user assertions, not
biometrics).

``inferred_gender``: ``speaker.predicted_gender`` and the ``gender`` / ``alignment`` /
``alignment_hint`` keys of ``attribute_confidence`` (the latter two are the name-versus-voice
comparison, itself an inference from gender) where ``gender_confirmed_by_user`` is not true, and
``attributes_predicted_at``. ``speaker_profile.predicted_gender`` is NOT touched: its only writer is
the user's own "confirm gender" action, so it is a human assertion, not an inference.

Safety
------
A target is refused unless its switch is OFF at the purging layer or above
(:func:`targets_still_enabled`); otherwise new data would grow straight back. That decision FAILS
CLOSED: a policy read that errored reads as "still enabled". The task re-checks it when it starts,
because the layer may have been switched back on between the request and the run.

OpenSearch snapshots keep old documents until they rotate (``services/opensearch_snapshot.py``);
the docs page states that window.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from typing import Literal
from typing import cast

from sqlalchemy import Text
from sqlalchemy import cast as sql_cast
from sqlalchemy import delete
from sqlalchemy import literal
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

VOICEPRINTS = "voiceprints"
INFERRED_GENDER = "inferred_gender"
PURGE_TARGETS = (VOICEPRINTS, INFERRED_GENDER)

ScopeKind = Literal["organization", "user", "instance_personal", "instance_all"]
_SCOPE_KINDS = ("organization", "user", "instance_personal", "instance_all")

#: The second pass runs this long after the first. It must exceed the write backstop's cache TTL
#: (``constants.PRIVACY_WRITE_BACKSTOP_TTL_SECONDS``, 10 s) plus an in-flight write, so a write
#: that raced the switch-off is caught.
SECOND_PASS_DELAY_SECONDS = 60

#: Redis record of a purge, readable by the person who started it.
_RECORD_KEY = "privacy_purge:{task_id}"
_RECORD_TTL_SECONDS = 7 * 24 * 3600

_SPEAKER_BATCH = 500


@dataclass(frozen=True)
class PurgeScope:
    """Whose data a purge deletes. Built by the route from the request context, never the body."""

    kind: ScopeKind
    organization_id: int | None = None
    user_id: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in _SCOPE_KINDS:
            raise ValueError(f"unknown purge scope {self.kind!r}")
        if self.kind == "organization" and self.organization_id is None:
            raise ValueError("an organization scope needs an organization id")
        if self.kind == "user" and self.user_id is None:
            raise ValueError("a user scope needs a user id")
        if self.kind in ("instance_personal", "instance_all") and (
            self.organization_id is not None or self.user_id is not None
        ):
            raise ValueError("an instance scope names no organization or user")

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe form for a Celery argument."""
        return {
            "kind": self.kind,
            "organization_id": self.organization_id,
            "user_id": self.user_id,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PurgeScope:
        return cls(
            kind=payload["kind"],
            organization_id=payload.get("organization_id"),
            user_id=payload.get("user_id"),
        )

    def sql_filters(self, model: Any) -> list[Any]:
        """Conditions selecting this scope's rows of a table with ``user_id`` / ``organization_id``.

        Not for ``Speaker``: production never stamps ``speaker.organization_id`` (the diarization
        helper creates the row without it), so a speaker's tenant is its FILE's. Use
        :meth:`speaker_filters`.
        """
        if self.kind == "instance_all":
            return []
        if self.kind == "instance_personal":
            return [model.organization_id.is_(None)]
        if self.kind == "organization":
            return [model.organization_id == self.organization_id]
        org = (
            model.organization_id.is_(None)
            if self.organization_id is None
            else model.organization_id == self.organization_id
        )
        return [model.user_id == self.user_id, org]

    def speaker_filters(self) -> list[Any]:
        """Conditions selecting this scope's speakers, by the tenant of the speaker's FILE.

        ``PrivacyView`` and every other reader take a speaker's tenant from
        ``media_file.organization_id``; ``speaker.organization_id`` is NULL for every speaker the
        pipeline created, so filtering on it matched nothing for an organization and everything
        in the personal workspace.
        """
        from app.models.media import MediaFile
        from app.models.media import Speaker

        file_ids = select(MediaFile.id).where(*self.sql_filters(MediaFile))
        return [Speaker.media_file_id.in_(file_ids)]


@dataclass
class PurgeCounts:
    """What a purge removed. Counts only; nothing here names a person or a recording."""

    values: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, amount: int) -> None:
        self.values[key] = self.values.get(key, 0) + int(amount)

    def merge(self, other: dict[str, int]) -> None:
        for key, amount in other.items():
            self.add(key, amount)


# --- Is the purge allowed? ---------------------------------------------------------------


def targets_still_enabled(db: Session, scope: PurgeScope, targets: Iterable[str]) -> list[str]:
    """The targets whose switch is NOT off at the purging layer or above. Empty = purge allowed.

    The layers counted are the scope's own and everything above it: an instance purge counts
    platform and instance; an organization purge adds the organization; a user purge adds the
    user, so a person whose organization turned the feature off can still purge their own data
    though they can no longer change their own switch. Fails closed: a policy that could not be
    read cleanly keeps every target "still enabled".
    """
    from app.services.privacy.policy import _resolve as resolve_with_status

    user_id = scope.user_id if scope.kind == "user" else None
    organization_id = scope.organization_id if scope.kind in ("organization", "user") else None
    policy, clean = resolve_with_status(db, user_id, organization_id)
    decisions = {VOICEPRINTS: policy.voiceprints, INFERRED_GENDER: policy.gender}
    blocked: list[str] = []
    for target in dict.fromkeys(targets):
        decision = decisions.get(target)
        if decision is None or not clean or decision.enabled:
            blocked.append(target)
    return blocked


# --- Postgres ----------------------------------------------------------------------------


def _rowcount(db: Session, statement: Any) -> int:
    """Execute a DML statement and return how many rows it touched."""
    return int(cast("CursorResult[Any]", db.execute(statement)).rowcount or 0)


def _scope_speaker_ids(scope: PurgeScope) -> Any:
    from app.models.media import Speaker

    return select(Speaker.id).where(*scope.speaker_filters())


def purge_voiceprints_postgres(db: Session, scope: PurgeScope) -> tuple[dict[str, int], list[int]]:
    """The relational half of a voiceprint purge. Commits.

    Returns:
        The counts, and the ids of speakers whose voiceprint suggestion was cleared (their
        status and label need refreshing; see :func:`refresh_cleared_speakers`).
    """
    from app.models.media import Speaker
    from app.models.media import SpeakerCluster
    from app.models.media import SpeakerClusterMember
    from app.models.media import SpeakerMatch
    from app.models.media import SpeakerProfile
    from app.utils.speaker_labels import EMBEDDING_SUGGESTION_SOURCES

    counts: dict[str, int] = {}
    in_scope = _scope_speaker_ids(scope)

    counts["matches_deleted"] = _rowcount(
        db,
        delete(SpeakerMatch).where(
            or_(SpeakerMatch.speaker1_id.in_(in_scope), SpeakerMatch.speaker2_id.in_(in_scope))
        ),
    )

    # Promoted clusters belong to a named profile and stay (the profile is a user assertion); their
    # centroid documents go with the OpenSearch delete.
    unpromoted = select(SpeakerCluster.id).where(
        *scope.sql_filters(SpeakerCluster), SpeakerCluster.promoted_to_profile_id.is_(None)
    )
    counts["speakers_unclustered"] = _rowcount(
        db,
        update(Speaker)
        .where(Speaker.cluster_id.in_(unpromoted))
        .values(cluster_id=None)
        .execution_options(synchronize_session=False),
    )
    counts["cluster_members_deleted"] = _rowcount(
        db, delete(SpeakerClusterMember).where(SpeakerClusterMember.cluster_id.in_(unpromoted))
    )
    counts["clusters_deleted"] = _rowcount(
        db,
        delete(SpeakerCluster).where(
            SpeakerCluster.id.in_(
                select(SpeakerCluster.id).where(
                    *scope.sql_filters(SpeakerCluster),
                    SpeakerCluster.promoted_to_profile_id.is_(None),
                )
            )
        ),
    )

    counts["profiles_reset"] = _rowcount(
        db,
        update(SpeakerProfile)
        .where(
            *scope.sql_filters(SpeakerProfile),
            or_(
                SpeakerProfile.embedding_count > 0,
                SpeakerProfile.last_embedding_update.isnot(None),
            ),
        )
        .values(embedding_count=0, last_embedding_update=None)
        .execution_options(synchronize_session=False),
    )

    cleared = [
        int(row[0])
        for row in db.execute(
            update(Speaker)
            .where(
                *scope.speaker_filters(),
                Speaker.suggestion_source.in_(sorted(EMBEDDING_SUGGESTION_SOURCES)),
                or_(Speaker.verified.is_(None), Speaker.verified.is_(False)),
            )
            .values(suggested_name=None, suggestion_source=None, confidence=None)
            .returning(Speaker.id)
            .execution_options(synchronize_session=False)
        )
    ]
    counts["suggestions_cleared"] = len(cleared)
    db.commit()
    return counts, cleared


def purge_inferred_gender_postgres(db: Session, scope: PurgeScope) -> dict[str, int]:
    """The relational half of an inferred-gender purge. Commits."""
    from app.models.media import Speaker

    def drop(key: str) -> Any:
        return sql_cast(literal(key), Text)

    inferred = (
        or_(
            Speaker.gender_confirmed_by_user.is_(None), Speaker.gender_confirmed_by_user.is_(False)
        ),
        or_(
            Speaker.predicted_gender.isnot(None),
            Speaker.attribute_confidence.has_key("gender"),
            Speaker.attribute_confidence.has_key("alignment"),
            Speaker.attribute_confidence.has_key("alignment_hint"),
        ),
    )
    bag = Speaker.attribute_confidence.op("-")(drop("gender"))
    bag = bag.op("-")(drop("alignment"))
    bag = bag.op("-")(drop("alignment_hint"))
    cleared = _rowcount(
        db,
        update(Speaker)
        .where(*scope.speaker_filters(), *inferred)
        .values(predicted_gender=None, attribute_confidence=bag, attributes_predicted_at=None)
        .execution_options(synchronize_session=False),
    )
    db.commit()
    return {"gender_speakers_cleared": cleared}


def refresh_cleared_speakers(
    db: Session,
    speaker_ids: list[int],
    reindex: Callable[[list[tuple[int, str, int]]], int] | None = None,
) -> dict[str, int]:
    """Recompute the derived status and label of speakers whose suggestion was cleared.

    A cleared voiceprint suggestion may have been the speaker's label (a >=0.75 voiceprint
    suggestion is the canonical name), so ``resolved_display_name`` and the chunk index would keep
    showing it. The status columns are recomputed here and each affected file is queued for a
    (debounced) transcript reindex.

    Args:
        db: Session. Commits.
        speaker_ids: The speakers to refresh.
        reindex: Queues reindexes for ``(file_id, file_uuid, owner_user_id)`` triples and returns
            how many failed to queue. Defaults to :func:`_queue_reindexes`; a parameter so a test
            need not stand up Redis and a broker.

    Returns:
        ``{"statuses_refreshed", "reindex_queue_failed"}``.
    """
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.services.speaker_status_service import SpeakerStatusService

    refreshed = 0
    file_ids: set[int] = set()
    for start in range(0, len(speaker_ids), _SPEAKER_BATCH):
        batch = speaker_ids[start : start + _SPEAKER_BATCH]
        for speaker in db.query(Speaker).filter(Speaker.id.in_(batch)).all():
            status = SpeakerStatusService.compute_speaker_status(speaker)
            speaker.computed_status = status["computed_status"]  # type: ignore[assignment]
            speaker.status_text = status["status_text"]  # type: ignore[assignment]
            speaker.status_color = status["status_color"]  # type: ignore[assignment]
            speaker.resolved_display_name = status["resolved_display_name"]  # type: ignore[assignment]
            file_ids.add(int(speaker.media_file_id))
            refreshed += 1
        db.commit()

    files = [
        (int(row[0]), str(row[1]), int(row[2]))
        for row in db.query(MediaFile.id, MediaFile.uuid, MediaFile.user_id)
        .filter(MediaFile.id.in_(sorted(file_ids)))
        .all()
    ]
    failed = (reindex or _queue_reindexes)(files) if files else 0
    return {"statuses_refreshed": refreshed, "reindex_queue_failed": failed}


def _queue_reindexes(files: list[tuple[int, str, int]]) -> int:
    from app.services.search.reindex_dispatch import dispatch_transcript_reindex

    failed = 0
    for file_id, file_uuid, user_id in files:
        try:
            dispatch_transcript_reindex(file_id=file_id, file_uuid=file_uuid, user_id=user_id)
        except Exception:
            logger.exception("privacy purge: could not queue a reindex for file %s", file_id)
            failed += 1
    return failed


# --- OpenSearch --------------------------------------------------------------------------


def purge_voiceprint_docs(scope: PurgeScope, errors: list[dict[str, Any]]) -> int:
    """Delete the scope's speaker and profile embedding documents. Holds no database session."""
    from app.services.gdpr_erasure_service import _erase_speaker_voiceprints
    from app.services.gdpr_erasure_service import delete_speaker_docs_by_query

    if scope.kind == "instance_all":
        return delete_speaker_docs_by_query(
            {"query": {"match_all": {}}}, errors=errors, label="the whole instance"
        )
    if scope.kind == "instance_personal":
        return _erase_speaker_voiceprints(personal_only=True, errors=errors)
    if scope.kind == "organization":
        return _erase_speaker_voiceprints(organization_id=scope.organization_id, errors=errors)
    return _erase_speaker_voiceprints(
        user_id=scope.user_id,
        organization_id=scope.organization_id,
        personal_only=scope.organization_id is None,
        errors=errors,
    )


# --- One pass ----------------------------------------------------------------------------


def run_purge_pass(
    scope: PurgeScope,
    targets: Iterable[str],
    *,
    session_factory: Callable[[], Session],
    reindex: Callable[[list[tuple[int, str, int]]], int] | None = None,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """One full pass over the targets: relational rows first, then the OpenSearch documents.

    Each relational step opens its own short session and closes it before the next, and the
    OpenSearch delete (slow) holds none. Idempotent, which is what lets the task run it twice.

    Returns:
        ``(counts, errors)``. ``errors`` is non-empty when a store could not be fully purged; the
        caller must record the purge as partial, not complete.
    """
    wanted = set(targets)
    counts = PurgeCounts()
    errors: list[dict[str, Any]] = []

    if VOICEPRINTS in wanted:
        db = session_factory()
        try:
            relational, cleared = purge_voiceprints_postgres(db, scope)
            counts.merge(relational)
            counts.merge(refresh_cleared_speakers(db, cleared, reindex))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        counts.add("voiceprint_docs_deleted", purge_voiceprint_docs(scope, errors))

    if INFERRED_GENDER in wanted:
        db = session_factory()
        try:
            counts.merge(purge_inferred_gender_postgres(db, scope))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return counts.values, errors


# --- Audit and status record -------------------------------------------------------------

_EVENT_FOR_TARGET = {
    VOICEPRINTS: AuditEventType.PRIVACY_VOICEPRINT_PURGE,
    INFERRED_GENDER: AuditEventType.PRIVACY_GENDER_PURGE,
}


def audit_purge(
    *,
    scope: PurgeScope,
    targets: Iterable[str],
    phase: str,
    actor_user_id: int | None,
    actor_username: str | None,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    source_ip: str | None = None,
    user_agent: str | None = None,
    counts: dict[str, int] | None = None,
    reason: str | None = None,
) -> None:
    """One audit event per target. Org-stamped; ``details`` hold the scope kind and counts only."""
    for target in dict.fromkeys(targets):
        details: dict[str, Any] = {"phase": phase, "scope": scope.kind}
        if counts is not None:
            details["counts"] = counts
        if reason is not None:
            details["reason"] = reason
        audit_logger.log(
            event_type=_EVENT_FOR_TARGET[target],
            outcome=outcome,
            user_id=actor_user_id,
            username=actor_username,
            source_ip=source_ip,
            user_agent=user_agent,
            details=details,
            organization_id=scope.organization_id,
            target_user_id=scope.user_id if scope.user_id != actor_user_id else None,
        )


def write_record(task_id: str, user_id: int, counts: dict[str, int]) -> None:
    """Remember a purge's initiator and running counts, for ``GET /privacy/purge/{task_id}``."""
    try:
        from app.core.redis import get_redis

        get_redis().set(
            _RECORD_KEY.format(task_id=task_id),
            json.dumps({"user_id": user_id, "counts": counts}),
            ex=_RECORD_TTL_SECONDS,
        )
    except Exception:
        logger.warning("privacy purge: could not store the status record for %s", task_id)


def read_record(task_id: str) -> dict[str, Any] | None:
    """The record written by :func:`write_record`, or ``None`` (unknown, expired or no Redis)."""
    try:
        from app.core.redis import get_redis

        raw = get_redis().get(_RECORD_KEY.format(task_id=task_id))
        record = json.loads(raw) if raw else None
    except Exception:
        logger.warning("privacy purge: could not read the status record for %s", task_id)
        return None
    return record if isinstance(record, dict) else None
