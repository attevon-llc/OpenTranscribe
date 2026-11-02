"""Purge: delete the voiceprints and inferred gender a privacy switch only hides (issue #1280).

Turning a layer off hides data and never deletes it (``policy`` module docstring). This module is
the separate, explicit, audited deletion, and it is irreversible by design. Three scopes reach it
(``PurgeScope``):

* ``organization``: everything stamped with one organization, all members' (an organization admin).
* ``user``: one person's own data in one tenant (organization or personal workspace).
* ``instance_personal`` / ``instance_all``: every personal workspace, or literally everything (a
  super_admin; ``instance_all`` is refused in a multi-tenant deployment by the caller).
* ``files``: the voiceprints of the speakers of named files, whoever owns them (a retention
  destruction, ``purge_reason="retention"``). It never deletes a profile row or a profile
  document: each profile one of those speakers was linked to has its embedding RECOMPUTED from
  its remaining speakers after the speaker documents are gone (or erased when none remains).
  It is id-scoped only, like ``user``.

Interface of the ``files`` scope (stable; the voiceprint retention sweep codes against it)::

    scope = PurgeScope(kind="files", file_ids=(12, 34))     # non-empty; no org / user id
    targets_still_enabled(db, scope, [VOICEPRINTS], purge_reason=RETENTION) == []
    run_purge_pass(scope, [VOICEPRINTS], session_factory=SessionLocal)   # one pass
    privacy_purge_task.delay(scope=scope.to_payload(), targets=[VOICEPRINTS],
                             actor_user_id=None, purge_reason=RETENTION)  # both passes, audited

What is deleted
---------------
``voiceprints``: see :mod:`app.services.privacy.purge_voiceprints` (names and links a voiceprint
made revert to unnamed and unlinked; unverified voiceprint suggestions, ``speaker_match`` rows with
either side in scope, unpromoted clusters, profile embedding counters and the speaker, profile and
cluster-centroid documents of the scope's Postgres ids go; a person's typed or confirmed names and
links, profiles, avatars and promoted clusters stay).

``inferred_gender``: ``speaker.predicted_gender`` and the ``gender`` / ``alignment`` /
``alignment_hint`` keys of ``attribute_confidence`` (the latter two are the name-versus-voice
comparison, itself an inference from gender) where ``gender_confirmed_by_user`` is not true, and
``attributes_predicted_at``. ``speaker_profile.predicted_gender`` is NOT touched: its only writer is
the user's own "confirm gender" action, so it is a human assertion, not an inference.

Measured and verified
---------------------
Every pass returns the exact count removed per store (``postgres`` / ``opensearch`` groups; what
OpenSearch REPORTS as deleted, never the number of ids asked for) and then measures what it was
supposed to remove, in a fresh session after an index refresh (:mod:`purge_residual`). A non-zero
residual at the final pass makes the purge FAIL, never succeed; a measure that could not be taken is
reported as unverified, never as zero. Both go into the audit event and the status response.

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
from sqlalchemy import false
from sqlalchemy import literal
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.services.privacy.purge_residual import GENDER_RESIDUAL_KEYS
from app.services.privacy.purge_residual import VOICEPRINT_RESIDUAL_KEYS
from app.services.privacy.purge_residual import Residual
from app.services.privacy.purge_residual import measure_gender_residual
from app.services.privacy.purge_residual import measure_voiceprint_residual
from app.services.privacy.purge_voiceprints import purge_voiceprint_docs
from app.services.privacy.purge_voiceprints import purge_voiceprints_postgres
from app.services.privacy.purge_voiceprints import rebuild_trimmed_clusters

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

VOICEPRINTS = "voiceprints"
INFERRED_GENDER = "inferred_gender"
PURGE_TARGETS = (VOICEPRINTS, INFERRED_GENDER)

ScopeKind = Literal["organization", "user", "instance_personal", "instance_all", "files"]
_SCOPE_KINDS = ("organization", "user", "instance_personal", "instance_all", "files")

#: ``purge_reason`` of a destruction a retention schedule decided: it runs whether or not the
#: privacy switch is on (see :func:`targets_still_enabled`).
RETENTION = "retention"
#: ``purge_reason`` of an owner's per-file opt-out (issue #1356): the file's consent column, set
#: before the purge is queued, is what stops new voiceprints, so no switch needs to be off.
OPT_OUT = "opt_out"
PURGE_REASONS = (RETENTION, OPT_OUT)

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
    #: The files of a ``files`` scope (and only of one).
    file_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _SCOPE_KINDS:
            raise ValueError(f"unknown purge scope {self.kind!r}")
        if self.kind == "files":
            if not self.file_ids:
                raise ValueError("a files scope needs at least one file id")
            if self.organization_id is not None or self.user_id is not None:
                raise ValueError("a files scope names no organization or user")
            object.__setattr__(self, "file_ids", tuple(sorted({int(i) for i in self.file_ids})))
        elif self.file_ids:
            raise ValueError("only a files scope names file ids")
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
        payload: dict[str, Any] = {
            "kind": self.kind,
            "organization_id": self.organization_id,
            "user_id": self.user_id,
        }
        if self.kind == "files":
            payload["file_ids"] = list(self.file_ids)
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PurgeScope:
        return cls(
            kind=payload["kind"],
            organization_id=payload.get("organization_id"),
            user_id=payload.get("user_id"),
            file_ids=tuple(payload.get("file_ids") or ()),
        )

    def audit_details(self) -> dict[str, Any]:
        """The ``purge_scope`` object of an audit event: kind and ids, a file COUNT, no names."""
        details: dict[str, Any] = {
            "kind": self.kind,
            "organization_id": self.organization_id,
            "user_id": self.user_id,
        }
        if self.kind == "files":
            details["file_count"] = len(self.file_ids)
        return details

    def sql_filters(self, model: Any) -> list[Any]:
        """Conditions selecting this scope's rows of a table with ``user_id`` / ``organization_id``.

        Not for ``Speaker``: production never stamps ``speaker.organization_id`` (the diarization
        helper creates the row without it), so a speaker's tenant is its FILE's. Use
        :meth:`speaker_filters`.

        A ``files`` scope selects its own ``MediaFile`` rows and NOTHING of any other table: a
        file purge never deletes or resets a profile (or a stamped cluster) directly.
        """
        if self.kind == "files":
            from app.models.media import MediaFile

            if model is MediaFile:
                return [or_(*(MediaFile.id.in_(c) for c in _id_chunks(self.file_ids)))]
            return [false()]
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

        if self.kind == "files":
            return [or_(*(Speaker.media_file_id.in_(c) for c in _id_chunks(self.file_ids)))]
        file_ids = select(MediaFile.id).where(*self.sql_filters(MediaFile))
        return [Speaker.media_file_id.in_(file_ids)]


def _id_chunks(ids: tuple[int, ...]) -> list[list[int]]:
    from app.services.privacy.purge_voiceprints import _CHUNK

    return [list(ids[i : i + _CHUNK]) for i in range(0, len(ids), _CHUNK)]


# --- Is the purge allowed? ---------------------------------------------------------------


def targets_still_enabled(
    db: Session,
    scope: PurgeScope,
    targets: Iterable[str],
    *,
    purge_reason: str | None = None,
) -> list[str]:
    """The targets whose switch is NOT off at the purging layer or above. Empty = purge allowed.

    ``purge_reason=RETENTION`` is a destruction a retention schedule decided, not a privacy
    switch being honoured: it is allowed whatever the switches say (data past its retention
    period goes whether or not matching is on). ``purge_reason=OPT_OUT`` is the owner's per-file
    opt-out and is exempt the same way. They are the only exemptions, and both are explicit.

    The layers counted are the scope's own and everything above it: an instance purge counts
    platform and instance; an organization purge adds the organization; a user purge adds the
    user, so a person whose organization turned the feature off can still purge their own data
    though they can no longer change their own switch. Fails closed: a policy that could not be
    read cleanly keeps every target "still enabled".
    """
    from app.services.privacy.policy import _resolve as resolve_with_status

    if purge_reason is not None and purge_reason not in PURGE_REASONS:
        raise ValueError(f"unknown purge reason {purge_reason!r}")
    if purge_reason in (RETENTION, OPT_OUT):
        return []
    if scope.kind == "files":
        # A files scope exists for retention and opt-out; a privacy switch never authorises it.
        return list(dict.fromkeys(targets))
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
            # Persisted status describes the data; hiding is decided per read (#1280 D5).
            status = SpeakerStatusService.compute_speaker_status(speaker, voiceprint_names=True)
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


# --- One pass ----------------------------------------------------------------------------

POSTGRES = "postgres"
OPENSEARCH = "opensearch"

#: Counts of a store group, by the name the audit event and the status response use.
GroupedCounts = dict[str, dict[str, int]]


@dataclass
class TargetOutcome:
    """What one target's pass removed (counts per store) and what it measured afterwards."""

    counts: GroupedCounts = field(default_factory=lambda: {POSTGRES: {}, OPENSEARCH: {}})
    residual: Residual = field(default_factory=Residual)


@dataclass
class PassResult:
    """One full pass over the targets.

    ``errors`` is non-empty when a store could not be fully purged or measured; the caller must
    record the purge as partial, not complete.
    """

    outcomes: dict[str, TargetOutcome] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    #: Cluster centroid documents whose deletion this pass could not confirm. The cluster rows
    #: are already gone from Postgres, so the next pass cannot recompute these ids: it must be
    #: handed them (``run_purge_pass(carried_cluster_uuids=...)``) or they are never retried.
    unfinished_cluster_uuids: list[str] = field(default_factory=list)

    def counts(self) -> GroupedCounts:
        """All targets' counts, grouped per store."""
        merged: GroupedCounts = {POSTGRES: {}, OPENSEARCH: {}}
        for outcome in self.outcomes.values():
            for store, values in outcome.counts.items():
                for key, amount in values.items():
                    merged[store][key] = merged[store].get(key, 0) + amount
        return merged

    def residual(self) -> Residual:
        merged = Residual()
        for outcome in self.outcomes.values():
            merged.merge(outcome.residual)
        return merged


def merge_grouped(total: GroupedCounts, more: GroupedCounts) -> GroupedCounts:
    """Add ``more`` into ``total`` (pass 2 adds to pass 1)."""
    for store, values in more.items():
        bucket = total.setdefault(store, {})
        for key, amount in values.items():
            bucket[key] = bucket.get(key, 0) + int(amount)
    return total


def run_purge_pass(
    scope: PurgeScope,
    targets: Iterable[str],
    *,
    session_factory: Callable[[], Session],
    reindex: Callable[[list[tuple[int, str, int]]], int] | None = None,
    carried_cluster_uuids: Iterable[str] = (),
) -> PassResult:
    """One full pass over the targets: relational rows, OpenSearch documents, then verification.

    Each relational step opens its own short session and closes it before the next, and the
    OpenSearch delete (slow) holds none. Idempotent, which is what lets the task run it twice.

    Args:
        scope: Whose data.
        targets: Which targets.
        session_factory: Opens a short session.
        reindex: See :func:`refresh_cleared_speakers`.
        carried_cluster_uuids: ``PassResult.unfinished_cluster_uuids`` of the previous pass. A
            pass deletes the cluster ROWS before their centroid documents, so a document whose
            delete failed has no row left to name it; its id is carried here to be deleted and
            measured again.
    """
    wanted = set(targets)
    result = PassResult()

    if VOICEPRINTS in wanted:
        outcome = TargetOutcome()
        db = session_factory()
        try:
            done = purge_voiceprints_postgres(db, scope)
            outcome.counts[POSTGRES].update(done.counts)
            refreshed = refresh_cleared_speakers(db, done.cleared, reindex)
            outcome.counts[POSTGRES]["statuses_refreshed"] = refreshed["statuses_refreshed"]
            if refreshed["reindex_queue_failed"]:
                result.errors.append(
                    {"stage": "reindex", "failed": refreshed["reindex_queue_failed"]}
                )
            for residue in rebuild_trimmed_clusters(db, done.targets):
                result.errors.append(dict(residue))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        done.targets.cluster_uuids = list(
            dict.fromkeys([*done.targets.cluster_uuids, *map(str, carried_cluster_uuids)])
        )
        outcome.counts[OPENSEARCH].update(purge_voiceprint_docs(scope, done.targets, result.errors))
        if done.targets.recompute_profile_ids:
            # AFTER the speaker documents are gone (and the index refreshed), so a recompute
            # can never average a voiceprint this pass deleted back into a profile.
            outcome.counts[OPENSEARCH]["profiles_recomputed"] = recompute_profiles(
                done.targets.recompute_profile_ids,
                done.targets.speaker_uuids,
                session_factory,
                result.errors,
            )
        outcome.residual = measure_voiceprint_residual(
            scope, done.targets, session_factory=session_factory, errors=result.errors
        )
        if outcome.residual.counts.get("cluster_docs") != 0:  # still there, or never measured
            result.unfinished_cluster_uuids = list(done.targets.cluster_uuids)
        result.outcomes[VOICEPRINTS] = outcome

    if INFERRED_GENDER in wanted:
        outcome = TargetOutcome()
        db = session_factory()
        try:
            outcome.counts[POSTGRES].update(purge_inferred_gender_postgres(db, scope))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        outcome.residual = measure_gender_residual(scope, session_factory=session_factory)
        result.outcomes[INFERRED_GENDER] = outcome

    return result


def recompute_profiles(
    profile_ids: Iterable[int],
    deleted_speaker_uuids: Iterable[str],
    session_factory: Callable[[], Session],
    errors: list[dict[str, Any]],
) -> int:
    """Recompute each profile's embedding from the speakers it still has; erase what cannot be.

    Runs only once the deleted speakers' documents are confirmed gone (a refresh, then a count):
    otherwise a recompute could average a voiceprint this pass deleted back into the profile, so
    the embedding is ERASED instead (an absent embedding is rebuilt later; a stale one is a copy
    of the deleted voices). The rebuild is the one every speaker removal uses
    (``speaker_cleanup.reconcile_speaker_impact``): the average of the remaining voiceprints, or
    an erased document when none remains. The profile ROW and its links stay.

    Returns:
        How many profiles were recomputed (0 when they had to be erased); failures land in
        ``errors``.
    """
    from app.models.media import SpeakerProfile
    from app.services.gdpr_erasure_service import count_speaker_docs_by_ids
    from app.services.speaker_cleanup import SpeakerImpact
    from app.services.speaker_cleanup import reconcile_speaker_impact

    ids = sorted({int(p) for p in profile_ids})
    if not ids:
        return 0
    remaining = count_speaker_docs_by_ids(list(deleted_speaker_uuids), errors=errors)
    db = session_factory()
    try:
        if remaining == 0:
            impact = SpeakerImpact(profile_ids=set(ids))
        else:
            uuids = {
                str(r[0])
                for r in db.query(SpeakerProfile.uuid).filter(SpeakerProfile.id.in_(ids)).all()
            }
            db.query(SpeakerProfile).filter(SpeakerProfile.id.in_(ids)).update(
                {SpeakerProfile.embedding_count: 0}, synchronize_session=False
            )
            db.commit()
            impact = SpeakerImpact(erase_profile_uuids=uuids)
        for residue in reconcile_speaker_impact(db, impact):
            errors.append(dict(residue))
    finally:
        db.close()
    return len(ids) if remaining == 0 else 0


# --- Audit and status record -------------------------------------------------------------

_EVENT_FOR_TARGET = {
    VOICEPRINTS: AuditEventType.PRIVACY_VOICEPRINT_PURGE,
    INFERRED_GENDER: AuditEventType.PRIVACY_GENDER_PURGE,
}

#: Residual measures per target, for the status response's fixed shape.
RESIDUAL_KEYS = {VOICEPRINTS: VOICEPRINT_RESIDUAL_KEYS, INFERRED_GENDER: GENDER_RESIDUAL_KEYS}


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
    task_id: str | None = None,
    outcomes: dict[str, TargetOutcome] | None = None,
    reason: str | None = None,
    purge_reason: str | None = None,
) -> None:
    """One audit event per target, org-stamped, so an administrator can verify a purge from it.

    ``details`` hold the task id, phase, the purge's targets, the ``purge_scope`` (kind and ids;
    a ``files`` scope gives a ``file_count``), ``purge_reason`` when one was given (``retention``),
    and, for a pass that ran, that target's exact counts per store plus the residual it measured
    and whether it was all zero. ``reason`` is the OUTCOME's reason (``residual``, ``error``...),
    a different thing. Counts and ids of the scope only; never a name.
    """
    wanted = list(dict.fromkeys(targets))
    for target in wanted:
        details: dict[str, Any] = {
            "task_id": task_id,
            "phase": phase,
            "targets": wanted,
            # NOT "scope": content holds and the org audit export index `details.scope` as a
            # string, and OpenSearch rejects an object under a text-typed field (mapper_parsing_
            # exception). The rejected purge event fell to the fallback file and was unsearchable,
            # which the v0.6.0 rehearsal's audit check found.
            "purge_scope": scope.audit_details(),
        }
        if purge_reason is not None:
            details["purge_reason"] = purge_reason
        measured = (outcomes or {}).get(target)
        if measured is not None:
            details["counts"] = measured.counts
            details["residual"] = measured.residual.counts
            details["residual_unverified"] = sorted(set(measured.residual.unverified))
            details["residual_ok"] = measured.residual.ok
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


def purge_audit_event_types(targets: Iterable[str] | None) -> list[AuditEventType]:
    """The audit event type of each target, in order (what the status response points at)."""
    return [_EVENT_FOR_TARGET[t] for t in dict.fromkeys(targets or []) if t in _EVENT_FOR_TARGET]


def write_record(
    task_id: str,
    user_id: int | None,
    counts: GroupedCounts | dict[str, Any],
    *,
    targets: list[str] | None = None,
    residual: dict[str, int] | None = None,
    residual_ok: bool | None = None,
    residual_unverified: list[str] | None = None,
) -> None:
    """Remember a purge's initiator, running counts and residual, for ``GET /privacy/purge/{id}``."""
    try:
        from app.core.redis import get_redis

        record: dict[str, Any] = {"user_id": user_id, "counts": counts}
        if targets is not None:
            record["targets"] = list(targets)
        if residual is not None:
            record["residual"] = residual
            record["residual_ok"] = bool(residual_ok)
            record["residual_unverified"] = list(residual_unverified or [])
        get_redis().set(
            _RECORD_KEY.format(task_id=task_id), json.dumps(record), ex=_RECORD_TTL_SECONDS
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
