"""The voiceprint half of a privacy purge: relational rows, then the OpenSearch documents (#1280).

Everything here is scoped by what POSTGRES says belongs to the purged scope, never by a document's
own tenant stamp. A speaker's tenant is its FILE's (:meth:`PurgeScope.speaker_filters`); a
profile's is its row's; a cluster's is its MEMBERS'. OpenSearch documents are then selected by the
ids of those rows (speaker uuids, ``profile_<uuid>``, ``cluster_<uuid>``). A document written
before the tenant backfill carries no stamp: deleting by stamp misses it in an organization purge
and reaches it from a member's personal one.

What is deleted, and what is only reverted
-------------------------------------------
* A name and link a voiceprint wrote (``speaker.identity_source = 'voiceprint'``) revert to an
  unnamed, unlinked, unverified speaker. A link a voiceprint added to a person's own name
  (``'voiceprint_link'``) is removed, with the voice suggestion and confidence of the match
  that added it; the name and ``verified`` stay. A ``human`` or NULL
  identity is never touched. Machine-identified speakers leave promoted clusters (a cluster left
  empty is deleted).
* Voiceprint suggestions nobody verified; ``speaker_match`` rows with EITHER side in scope (a
  row joining an in-scope speaker to the same person's speaker in another scope is deleted too:
  it is voice-derived data of the purged scope, and counted separately); unpromoted clusters
  whose members are all in scope (a cluster that also holds another scope's speakers only loses
  the in-scope ones and has its centroid rebuilt from the rest); profile embedding counters.
* The speaker, profile and cluster-centroid documents of those ids, in every speaker index. An
  ORGANIZATION purge also deletes the documents carrying that organization's stamp
  (:func:`org_stamp_clause`): an orphan document Postgres has no row for, or one keyed by the
  pre-uuid integer speaker id, is the organization's all the same. A personal or user purge is
  id-scoped only: a document with no stamp is never reached by it.

Another tenant's rows, profiles and documents are never touched.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

from sqlalchemy import and_
from sqlalchemy import delete
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update

from app.utils.speaker_identity_source import MACHINE_IDENTITY_SOURCES
from app.utils.speaker_identity_source import VOICEPRINT
from app.utils.speaker_identity_source import VOICEPRINT_LINK
from app.utils.speaker_identity_source import cleared_provenance_values
from app.utils.speaker_identity_source import cleared_suggestion_values

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.orm import Session

    from app.services.privacy.purge import PurgeScope

#: Ids per ``IN (...)`` list; a scope can hold hundreds of thousands of speakers.
_CHUNK = 5000


@dataclass
class VoiceprintTargets:
    """What the relational half selected, for the OpenSearch half and the verification.

    Attributes:
        speaker_uuids: Every in-scope speaker (their voiceprint documents go).
        profile_uuids: Every in-scope profile (their embedding documents go).
        cluster_uuids: Clusters whose centroid document goes: the ones deleted, and promoted
            clusters belonging to an in-scope profile.
        rebuild_cluster_uuids: Clusters that also hold another scope's speakers (or a promoted
            cluster of another scope's profile): their centroid is rebuilt from the rest.
        rebuild_owner_ids: Owners of those clusters.
    """

    speaker_uuids: list[str] = field(default_factory=list)
    profile_uuids: list[str] = field(default_factory=list)
    cluster_uuids: list[str] = field(default_factory=list)
    rebuild_cluster_uuids: list[str] = field(default_factory=list)
    rebuild_owner_ids: list[int] = field(default_factory=list)

    def doc_ids(self) -> dict[str, list[str]]:
        """OpenSearch ``_id`` values by kind: ``speaker`` / ``profile`` / ``cluster``."""
        return {
            "speaker": list(self.speaker_uuids),
            "profile": [f"profile_{u}" for u in self.profile_uuids],
            "cluster": [f"cluster_{u}" for u in self.cluster_uuids],
        }


@dataclass
class PostgresPurge:
    """Result of :func:`purge_voiceprints_postgres`."""

    counts: dict[str, int]
    #: Speakers whose label or status may have changed (status refresh + reindex).
    cleared: list[int]
    targets: VoiceprintTargets


def _rowcount(db: Session, statement: Any) -> int:
    return int(cast("CursorResult[Any]", db.execute(statement)).rowcount or 0)


def _chunks(values: Iterable[int]) -> Iterable[list[int]]:
    items = sorted(set(values))
    for start in range(0, len(items), _CHUNK):
        yield items[start : start + _CHUNK]


def _scope_speaker_ids(scope: PurgeScope) -> Any:
    from app.models.media import Speaker

    return select(Speaker.id).where(*scope.speaker_filters())


def org_stamp_clause(scope: PurgeScope, kind: str) -> dict[str, Any] | None:
    """Documents of ``kind`` carrying the purged organization's own stamp, or ``None``.

    Only an ORGANIZATION scope has one: a positive match on ``organization_id`` is the
    organization's data whether or not Postgres still has the row. It is never used for a
    personal or user scope, where an absent stamp means "personal" and a stamp-based delete
    would reach a document it must not.

    Args:
        scope: The purge scope.
        kind: ``speaker`` / ``profile`` / ``cluster`` (the ``document_type`` of the document;
            a speaker document has none).
    """
    if scope.kind != "organization":
        return None
    stamp = {"term": {"organization_id": scope.organization_id}}
    if kind == "speaker":
        return {
            "bool": {
                "filter": [stamp],
                "must_not": [{"terms": {"document_type": ["profile", "cluster"]}}],
            }
        }
    return {"bool": {"filter": [stamp, {"term": {"document_type": kind}}]}}


def not_in_scope(scope: PurgeScope, speaker_id_column: Any) -> Any:
    """``speaker_id_column`` names a speaker OUTSIDE ``scope``, as an anti-join.

    ``NOT EXISTS`` rather than ``NOT IN (SELECT ...)``: Postgres evaluates the latter as a
    hashed SubPlan that spills to a per-row rescan once the subquery exceeds ``work_mem``, which
    is quadratic past about a million speakers (the first 5,000-cluster chunk did not finish in
    ten minutes at 4 MB). An anti-join stays linear.
    """
    from sqlalchemy.orm import aliased

    from app.models.media import MediaFile
    from app.models.media import Speaker

    inner = aliased(Speaker)
    return ~exists(
        select(1)
        .select_from(inner)
        .join(MediaFile, MediaFile.id == inner.media_file_id)
        .where(inner.id == speaker_id_column, *scope.sql_filters(MediaFile))
    )


def _revert_machine_identities(
    db: Session, scope: PurgeScope
) -> tuple[list[int], list[int], int, int, int, int]:
    """Steps 1 and 2: revert voiceprint-made identities, unlink voiceprint-made links.

    The record of how each identity came to be (time, person, detail: it names the profile or
    speaker the voice matched) and of the voice suggestion that went with it is cleared in the
    same statement, so no purge leaves a description of a name it removed.

    Returns:
        ``(reverted_ids, unlinked_ids, suggestions_cleared_with_them, links_removed,
        provenance_cleared, suggestion_details_cleared)``. ``links_removed`` counts only links
        that still pointed at a profile: a deleted profile already set the speaker's
        ``profile_id`` NULL (``ON DELETE SET NULL``), leaving a marker with no link to remove.
        ``provenance_cleared`` counts rows that actually held an identity time, person or
        detail; ``suggestion_details_cleared`` rows that held a suggestion detail.
    """
    from app.models.media import Speaker
    from app.utils.speaker_labels import EMBEDDING_SUGGESTION_SOURCES

    embedding_sources = sorted(EMBEDDING_SUGGESTION_SOURCES)
    has_voice_suggestion = Speaker.suggestion_source.in_(embedding_sources)
    with_suggestion = int(
        db.execute(
            select(func.count())
            .select_from(Speaker)
            .where(
                *scope.speaker_filters(),
                Speaker.identity_source.in_(sorted(MACHINE_IDENTITY_SOURCES)),
                has_voice_suggestion,
            )
        ).scalar_one()
    )
    provenance_cleared = int(
        db.execute(
            select(func.count())
            .select_from(Speaker)
            .where(
                *scope.speaker_filters(),
                Speaker.identity_source.in_(sorted(MACHINE_IDENTITY_SOURCES)),
                or_(
                    Speaker.identity_source_at.isnot(None),
                    Speaker.identity_source_user_id.isnot(None),
                    Speaker.identity_source_detail.isnot(None),
                ),
            )
        ).scalar_one()
    )
    suggestion_details_cleared = int(
        db.execute(
            select(func.count())
            .select_from(Speaker)
            .where(
                *scope.speaker_filters(),
                Speaker.identity_source.in_(sorted(MACHINE_IDENTITY_SOURCES)),
                has_voice_suggestion,
                Speaker.suggestion_detail.isnot(None),
            )
        ).scalar_one()
    )
    links_removed = int(
        db.execute(
            select(func.count())
            .select_from(Speaker)
            .where(
                *scope.speaker_filters(),
                Speaker.identity_source == VOICEPRINT_LINK,
                Speaker.profile_id.isnot(None),
            )
        ).scalar_one()
    )
    reverted = [
        int(row[0])
        for row in db.execute(
            update(Speaker)
            .where(*scope.speaker_filters(), Speaker.identity_source == VOICEPRINT)
            .values(
                display_name=None,
                profile_id=None,
                verified=False,
                **cleared_provenance_values(),
                # A voiceprint suggestion is voice data and goes with the identity it produced;
                # an LLM suggestion is not, and stays.
                **cleared_suggestion_values(has_voice_suggestion),
            )
            .returning(Speaker.id)
            .execution_options(synchronize_session=False)
        )
    ]
    unlinked = [
        int(row[0])
        for row in db.execute(
            update(Speaker)
            .where(*scope.speaker_filters(), Speaker.identity_source == VOICEPRINT_LINK)
            .values(
                profile_id=None,
                **cleared_provenance_values(),
                # The match that added the link is voice data too: its suggestion and
                # confidence go, the person's own name and verification stay.
                **cleared_suggestion_values(has_voice_suggestion),
            )
            .returning(Speaker.id)
            .execution_options(synchronize_session=False)
        )
    ]
    return (
        reverted,
        unlinked,
        with_suggestion,
        links_removed,
        provenance_cleared,
        suggestion_details_cleared,
    )


def _remove_from_promoted_clusters(
    db: Session, speaker_ids: list[int]
) -> tuple[int, list[tuple[int, str, int, int | None]]]:
    """Step 3: machine-identified speakers leave promoted clusters.

    Deletes their membership rows, clears ``speaker.cluster_id`` where it points at a promoted
    cluster, recounts the touched clusters and deletes those left empty.

    Returns:
        ``(membership_rows_removed, touched)`` where ``touched`` lists every promoted cluster
        that lost a member as ``(id, uuid, member_count_now, promoted_profile_id)``; the empty
        ones are already deleted.
    """
    from app.models.media import Speaker
    from app.models.media import SpeakerCluster
    from app.models.media import SpeakerClusterMember

    promoted = select(SpeakerCluster.id).where(SpeakerCluster.promoted_to_profile_id.isnot(None))
    touched_ids: set[int] = set()
    removed = 0
    for chunk in _chunks(speaker_ids):
        touched_ids.update(
            int(r[0])
            for r in db.execute(
                select(Speaker.cluster_id).where(
                    Speaker.id.in_(chunk),
                    Speaker.cluster_id.isnot(None),
                    Speaker.cluster_id.in_(promoted),
                )
            )
        )
        rows = db.execute(
            delete(SpeakerClusterMember)
            .where(
                SpeakerClusterMember.speaker_id.in_(chunk),
                SpeakerClusterMember.cluster_id.in_(promoted),
            )
            .returning(SpeakerClusterMember.cluster_id)
        ).all()
        removed += len(rows)
        touched_ids.update(int(r[0]) for r in rows)
        db.execute(
            update(Speaker)
            .where(Speaker.id.in_(chunk), Speaker.cluster_id.in_(promoted))
            .values(cluster_id=None)
            .execution_options(synchronize_session=False)
        )
    touched: list[tuple[int, str, int, int | None]] = []
    for chunk in _chunks(touched_ids):
        db.execute(
            update(SpeakerCluster)
            .where(SpeakerCluster.id.in_(chunk))
            .values(
                member_count=select(func.count())
                .select_from(SpeakerClusterMember)
                .where(SpeakerClusterMember.cluster_id == SpeakerCluster.id)
                .scalar_subquery()
            )
            .execution_options(synchronize_session=False)
        )
        touched.extend(
            (int(r[0]), str(r[1]), int(r[2] or 0), int(r[3]) if r[3] is not None else None)
            for r in db.execute(
                select(
                    SpeakerCluster.id,
                    SpeakerCluster.uuid,
                    SpeakerCluster.member_count,
                    SpeakerCluster.promoted_to_profile_id,
                ).where(SpeakerCluster.id.in_(chunk))
            )
        )
    return removed, touched


def _purge_clusters(
    db: Session, scope: PurgeScope, counts: dict[str, int], targets: VoiceprintTargets
) -> None:
    """Step: unpromoted clusters by their MEMBERS (not their stamp), promoted by their profile.

    A cluster whose members are all in scope (or that has none and is stamped to the scope) is
    deleted with its centroid. A cluster that also holds speakers of another scope loses only the
    in-scope members; its centroid is rebuilt from the rest.
    """
    from app.models.media import Speaker
    from app.models.media import SpeakerCluster
    from app.models.media import SpeakerClusterMember

    in_scope = _scope_speaker_ids(scope)
    unpromoted = SpeakerCluster.promoted_to_profile_id.is_(None)

    candidates: set[int] = {
        int(r[0])
        for r in db.execute(
            select(SpeakerCluster.id).where(
                unpromoted,
                or_(
                    SpeakerCluster.id.in_(
                        select(SpeakerClusterMember.cluster_id).where(
                            SpeakerClusterMember.speaker_id.in_(in_scope)
                        )
                    ),
                    SpeakerCluster.id.in_(
                        select(Speaker.cluster_id).where(
                            Speaker.id.in_(in_scope), Speaker.cluster_id.isnot(None)
                        )
                    ),
                ),
            )
        )
    }
    memberless_in_scope = select(SpeakerCluster.id).where(
        unpromoted,
        *scope.sql_filters(SpeakerCluster),
        ~exists().where(SpeakerClusterMember.cluster_id == SpeakerCluster.id),
        ~exists().where(Speaker.cluster_id == SpeakerCluster.id),
    )
    candidates.update(int(r[0]) for r in db.execute(memberless_in_scope))

    mixed: set[int] = set()
    for chunk in _chunks(candidates):
        mixed.update(
            int(r[0])
            for r in db.execute(
                select(SpeakerClusterMember.cluster_id).where(
                    SpeakerClusterMember.cluster_id.in_(chunk),
                    not_in_scope(scope, SpeakerClusterMember.speaker_id),
                )
            )
        )
        mixed.update(
            int(r[0])
            for r in db.execute(
                select(Speaker.cluster_id).where(
                    Speaker.cluster_id.in_(chunk), not_in_scope(scope, Speaker.id)
                )
            )
        )
    whole = candidates - mixed

    for chunk in _chunks(whole):
        targets.cluster_uuids.extend(
            str(r[0])
            for r in db.execute(select(SpeakerCluster.uuid).where(SpeakerCluster.id.in_(chunk)))
        )
        counts["cluster_members_deleted"] += _rowcount(
            db, delete(SpeakerClusterMember).where(SpeakerClusterMember.cluster_id.in_(chunk))
        )
        counts["speakers_unclustered"] += _rowcount(
            db,
            update(Speaker)
            .where(Speaker.cluster_id.in_(chunk))
            .values(cluster_id=None)
            .execution_options(synchronize_session=False),
        )
        counts["clusters_deleted"] += _rowcount(
            db, delete(SpeakerCluster).where(SpeakerCluster.id.in_(chunk))
        )

    for chunk in _chunks(mixed):
        counts["cluster_members_deleted"] += _rowcount(
            db,
            delete(SpeakerClusterMember).where(
                SpeakerClusterMember.cluster_id.in_(chunk),
                SpeakerClusterMember.speaker_id.in_(in_scope),
            ),
        )
        counts["speakers_unclustered"] += _rowcount(
            db,
            update(Speaker)
            .where(Speaker.cluster_id.in_(chunk), Speaker.id.in_(in_scope))
            .values(cluster_id=None)
            .execution_options(synchronize_session=False),
        )
        db.execute(
            update(SpeakerCluster)
            .where(SpeakerCluster.id.in_(chunk))
            .values(
                member_count=select(func.count())
                .select_from(SpeakerClusterMember)
                .where(SpeakerClusterMember.cluster_id == SpeakerCluster.id)
                .scalar_subquery()
            )
            .execution_options(synchronize_session=False)
        )
        for row in db.execute(
            select(SpeakerCluster.uuid, SpeakerCluster.user_id).where(SpeakerCluster.id.in_(chunk))
        ):
            targets.rebuild_cluster_uuids.append(str(row[0]))
            targets.rebuild_owner_ids.append(int(row[1]))
    counts["cross_scope_clusters_trimmed"] += len(mixed)


def purge_voiceprints_postgres(db: Session, scope: PurgeScope) -> PostgresPurge:
    """The relational half of a voiceprint purge. Commits.

    Returns:
        The counts (keys of the audit event's ``postgres`` group), the speakers whose status and
        label need refreshing (see :func:`app.services.privacy.purge.refresh_cleared_speakers`),
        and the ids the OpenSearch half and the verification need.
    """
    from app.models.media import Speaker
    from app.models.media import SpeakerMatch
    from app.models.media import SpeakerProfile
    from app.utils.speaker_labels import EMBEDDING_SUGGESTION_SOURCES

    counts: dict[str, int] = {
        key: 0
        for key in (
            "speaker_identities_reverted",
            "speaker_links_removed",
            "suggestions_cleared",
            "identity_provenance_cleared",
            "suggestion_details_cleared",
            "speaker_matches_deleted",
            "cross_scope_matches",
            "speakers_unclustered",
            "cluster_members_deleted",
            "clusters_deleted",
            "cross_scope_clusters_trimmed",
            "promoted_cluster_members_removed",
            "promoted_clusters_deleted",
            "profiles_reset",
        )
    }
    targets = VoiceprintTargets()
    targets.speaker_uuids = [
        str(r[0]) for r in db.execute(select(Speaker.uuid).where(*scope.speaker_filters()))
    ]
    profile_rows = db.execute(
        select(SpeakerProfile.id, SpeakerProfile.uuid).where(*scope.sql_filters(SpeakerProfile))
    ).all()
    targets.profile_uuids = [str(r[1]) for r in profile_rows]
    scope_profile_ids = [int(r[0]) for r in profile_rows]

    (
        reverted,
        unlinked,
        suggestions_with_them,
        links_removed,
        provenance_cleared,
        details_with_them,
    ) = _revert_machine_identities(db, scope)
    counts["speaker_identities_reverted"] = len(reverted)
    counts["speaker_links_removed"] = links_removed
    counts["suggestions_cleared"] = suggestions_with_them
    counts["identity_provenance_cleared"] = provenance_cleared
    counts["suggestion_details_cleared"] = details_with_them
    machine_ids = reverted + unlinked

    removed, touched = _remove_from_promoted_clusters(db, machine_ids)
    counts["promoted_cluster_members_removed"] = removed
    from app.models.media import SpeakerCluster

    owned_profiles = set(scope_profile_ids)
    emptied = [t for t in touched if t[2] == 0]
    emptied_owner = {
        int(r[0]): int(r[1])
        for r in db.execute(
            select(SpeakerCluster.id, SpeakerCluster.user_id).where(
                SpeakerCluster.id.in_([t[0] for t in emptied])
            )
        )
    }
    for cluster_id, cluster_uuid, _count, _profile in emptied:
        # The centroid averaged the voices this purge removed, so it always goes. The row goes
        # too unless a person's own purge reaches a cluster another member owns: that row is
        # theirs, and an empty one is harmless (they re-cluster, or delete it).
        targets.cluster_uuids.append(cluster_uuid)
        if scope.kind == "user" and emptied_owner.get(cluster_id) != scope.user_id:
            continue
        counts["promoted_clusters_deleted"] += _rowcount(
            db, delete(SpeakerCluster).where(SpeakerCluster.id == cluster_id)
        )
    for _id, cluster_uuid, count, profile_id in touched:
        if count > 0 and profile_id not in owned_profiles:
            # Another scope's promoted cluster that lost an in-scope machine member.
            targets.rebuild_cluster_uuids.append(cluster_uuid)
    # Promoted clusters of an in-scope profile: their centroid is an average of the voiceprints
    # this purge deletes, so it goes too (the cluster and its human members stay).
    if scope_profile_ids:
        for chunk in _chunks(scope_profile_ids):
            targets.cluster_uuids.extend(
                str(r[0])
                for r in db.execute(
                    select(SpeakerCluster.uuid).where(
                        SpeakerCluster.promoted_to_profile_id.in_(chunk)
                    )
                )
            )

    _purge_clusters(db, scope, counts, targets)

    in_scope = _scope_speaker_ids(scope)
    both_sides = and_(
        SpeakerMatch.speaker1_id.in_(in_scope), SpeakerMatch.speaker2_id.in_(in_scope)
    )
    counts["speaker_matches_deleted"] = _rowcount(
        db, delete(SpeakerMatch).where(both_sides).execution_options(synchronize_session=False)
    )
    # The other side's speaker row is never modified; only the row that joins it to this scope.
    counts["cross_scope_matches"] = _rowcount(
        db,
        delete(SpeakerMatch)
        .where(or_(SpeakerMatch.speaker1_id.in_(in_scope), SpeakerMatch.speaker2_id.in_(in_scope)))
        .execution_options(synchronize_session=False),
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

    unverified_voice_suggestion = (
        *scope.speaker_filters(),
        Speaker.suggestion_source.in_(sorted(EMBEDDING_SUGGESTION_SOURCES)),
        or_(Speaker.verified.is_(None), Speaker.verified.is_(False)),
    )
    counts["suggestion_details_cleared"] += int(
        db.execute(
            select(func.count())
            .select_from(Speaker)
            .where(*unverified_voice_suggestion, Speaker.suggestion_detail.isnot(None))
        ).scalar_one()
    )
    cleared_suggestions = [
        int(row[0])
        for row in db.execute(
            update(Speaker)
            .where(*unverified_voice_suggestion)
            .values(**cleared_suggestion_values())
            .returning(Speaker.id)
            .execution_options(synchronize_session=False)
        )
    ]
    counts["suggestions_cleared"] += len(cleared_suggestions)
    db.commit()
    cleared = sorted(set(machine_ids) | set(cleared_suggestions))
    targets.cluster_uuids = list(dict.fromkeys(targets.cluster_uuids))
    targets.rebuild_cluster_uuids = list(dict.fromkeys(targets.rebuild_cluster_uuids))
    targets.rebuild_owner_ids = sorted(set(targets.rebuild_owner_ids))
    return PostgresPurge(counts=counts, cleared=cleared, targets=targets)


def rebuild_trimmed_clusters(db: Session, targets: VoiceprintTargets) -> list[dict[str, Any]]:
    """Rebuild (or erase) the centroids of clusters that also hold another scope's speakers.

    Runs after the relational commit, through the one cluster-refresh implementation the
    speaker delete uses (:func:`speaker_cleanup.reconcile_speaker_impact`). Never raises.

    Returns:
        ``{"stage", "error"}`` per centroid that may still carry the purged voices.
    """
    from app.models.media import SpeakerCluster
    from app.services.speaker_cleanup import SpeakerImpact
    from app.services.speaker_cleanup import reconcile_speaker_impact

    if not targets.rebuild_cluster_uuids:
        return []
    owners = {
        int(r[0])
        for r in db.query(SpeakerCluster.user_id)
        .filter(SpeakerCluster.uuid.in_(targets.rebuild_cluster_uuids))
        .all()
    }
    return reconcile_speaker_impact(
        db,
        SpeakerImpact(owner_ids=owners, cluster_uuids=set(targets.rebuild_cluster_uuids)),
    )


def purge_voiceprint_docs(
    scope: PurgeScope, targets: VoiceprintTargets, errors: list[dict[str, Any]]
) -> dict[str, int]:
    """Delete the scope's speaker, profile and cluster-centroid documents. Holds no session.

    Returns:
        ``{"speaker_docs_deleted", "profile_docs_deleted", "cluster_docs_deleted"}``: what
        OpenSearch reports as deleted, never the number of ids asked for.
    """
    from app.services.gdpr_erasure_service import delete_speaker_docs_by_ids
    from app.services.gdpr_erasure_service import delete_speaker_docs_by_query

    if scope.kind == "instance_all":
        # Everything: no ids to enumerate, and orphan documents with no Postgres row go too.
        # Split by kind so each is counted on its own.
        profile = delete_speaker_docs_by_query(
            {"query": {"bool": {"filter": [{"term": {"document_type": "profile"}}]}}},
            errors=errors,
            label="the whole instance (profiles)",
        )
        cluster = delete_speaker_docs_by_query(
            {"query": {"bool": {"filter": [{"term": {"document_type": "cluster"}}]}}},
            errors=errors,
            label="the whole instance (clusters)",
        )
        speaker = delete_speaker_docs_by_query(
            {"query": {"match_all": {}}}, errors=errors, label="the whole instance (speakers)"
        )
        return {
            "speaker_docs_deleted": speaker,
            "profile_docs_deleted": profile,
            "cluster_docs_deleted": cluster,
        }
    ids = targets.doc_ids()
    label = f"scope {scope.kind}"
    return {
        "speaker_docs_deleted": delete_speaker_docs_by_ids(
            ids["speaker"], also=org_stamp_clause(scope, "speaker"), errors=errors, label=label
        ),
        "profile_docs_deleted": delete_speaker_docs_by_ids(
            ids["profile"], also=org_stamp_clause(scope, "profile"), errors=errors, label=label
        ),
        "cluster_docs_deleted": delete_speaker_docs_by_ids(
            ids["cluster"], also=org_stamp_clause(scope, "cluster"), errors=errors, label=label
        ),
    }


def machine_identity_count(db: Session, scope: PurgeScope) -> int:
    """Speakers in scope still carrying a voiceprint-made name or link (residual measure)."""
    from app.models.media import Speaker

    return int(
        db.execute(
            select(func.count())
            .select_from(Speaker)
            .where(
                *scope.speaker_filters(),
                Speaker.identity_source.in_(sorted(MACHINE_IDENTITY_SOURCES)),
            )
        ).scalar_one()
    )
