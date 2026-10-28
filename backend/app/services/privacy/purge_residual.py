"""Residual verification of a voiceprint / inferred-gender purge (issue #1280).

A purge that reports success without looking is a claim. After each pass, in a FRESH session and
after an OpenSearch refresh, this measures what the pass was supposed to remove, in the purged
scope. Every value must be 0. A value that could not be measured is reported as unverified, never
as 0: an unreachable store must not read as an empty one.

Only counts leave this module (no names, no ids).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select

from app.services.privacy.purge_voiceprints import org_stamp_clause
from app.utils.speaker_identity_source import MACHINE_IDENTITY_SOURCES

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.services.privacy.purge import PurgeScope
    from app.services.privacy.purge_voiceprints import VoiceprintTargets

#: The measures of a voiceprint purge, in the order they are shown.
VOICEPRINT_RESIDUAL_KEYS = (
    "speaker_identities_machine",
    "speaker_identity_provenance",
    "voiceprint_suggestions",
    "voiceprint_suggestion_details",
    "speaker_matches",
    "unpromoted_cluster_members",
    "profiles_with_embeddings",
    "speaker_docs",
    "profile_docs",
    "cluster_docs",
)

GENDER_RESIDUAL_KEYS = ("speakers_with_inferred_gender",)


@dataclass
class Residual:
    """What a pass left behind. ``counts`` are measured; ``unverified`` names the measures that
    could not be taken (and so are NOT in ``counts``)."""

    counts: dict[str, int] = field(default_factory=dict)
    unverified: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only when everything was measured and every measure is 0."""
        return not self.unverified and all(v == 0 for v in self.counts.values())

    def merge(self, other: Residual) -> None:
        self.counts.update(other.counts)
        self.unverified.extend(other.unverified)


def _count(db: Session, statement: Any) -> int:
    return int(db.execute(statement).scalar_one() or 0)


def measure_voiceprint_residual(
    scope: PurgeScope,
    targets: VoiceprintTargets,
    *,
    session_factory: Callable[[], Session],
    errors: list[dict[str, Any]],
) -> Residual:
    """Measure the voiceprint stores of ``scope`` after a pass."""
    from app.models.media import Speaker
    from app.models.media import SpeakerCluster
    from app.models.media import SpeakerClusterMember
    from app.models.media import SpeakerMatch
    from app.models.media import SpeakerProfile
    from app.services.gdpr_erasure_service import count_speaker_docs_by_ids
    from app.utils.speaker_labels import EMBEDDING_SUGGESTION_SOURCES

    result = Residual()
    db = session_factory()
    try:
        in_scope = select(Speaker.id).where(*scope.speaker_filters())
        unpromoted = select(SpeakerCluster.id).where(
            SpeakerCluster.promoted_to_profile_id.is_(None)
        )
        result.counts["speaker_identities_machine"] = _count(
            db,
            select(func.count())
            .select_from(Speaker)
            .where(
                *scope.speaker_filters(),
                Speaker.identity_source.in_(sorted(MACHINE_IDENTITY_SOURCES)),
            ),
        )
        # The record of how a machine identity came to be (it names the profile or speaker the
        # voice matched): on a machine identity, or left over on a row with no identity at all.
        result.counts["speaker_identity_provenance"] = _count(
            db,
            select(func.count())
            .select_from(Speaker)
            .where(
                *scope.speaker_filters(),
                or_(
                    Speaker.identity_source.in_(sorted(MACHINE_IDENTITY_SOURCES)),
                    Speaker.identity_source.is_(None),
                ),
                or_(
                    Speaker.identity_source_at.isnot(None),
                    Speaker.identity_source_user_id.isnot(None),
                    Speaker.identity_source_detail.isnot(None),
                ),
            ),
        )
        result.counts["voiceprint_suggestion_details"] = _count(
            db,
            select(func.count())
            .select_from(Speaker)
            .where(
                *scope.speaker_filters(),
                Speaker.suggestion_detail["origin"].astext.in_(
                    sorted(EMBEDDING_SUGGESTION_SOURCES)
                ),
            ),
        )
        result.counts["voiceprint_suggestions"] = _count(
            db,
            select(func.count())
            .select_from(Speaker)
            .where(
                *scope.speaker_filters(),
                Speaker.suggestion_source.in_(sorted(EMBEDDING_SUGGESTION_SOURCES)),
            ),
        )
        result.counts["speaker_matches"] = _count(
            db,
            select(func.count())
            .select_from(SpeakerMatch)
            .where(
                or_(SpeakerMatch.speaker1_id.in_(in_scope), SpeakerMatch.speaker2_id.in_(in_scope))
            ),
        )
        result.counts["unpromoted_cluster_members"] = _count(
            db,
            select(func.count())
            .select_from(SpeakerClusterMember)
            .where(
                SpeakerClusterMember.speaker_id.in_(in_scope),
                SpeakerClusterMember.cluster_id.in_(unpromoted),
            ),
        ) + _count(
            db,
            select(func.count())
            .select_from(Speaker)
            .where(*scope.speaker_filters(), Speaker.cluster_id.in_(unpromoted)),
        )
        result.counts["profiles_with_embeddings"] = _count(
            db,
            select(func.count())
            .select_from(SpeakerProfile)
            .where(
                *scope.sql_filters(SpeakerProfile),
                or_(
                    SpeakerProfile.embedding_count > 0,
                    SpeakerProfile.last_embedding_update.isnot(None),
                ),
            ),
        )
    finally:
        db.close()

    ids = targets.doc_ids()
    for key, kind in (
        ("speaker_docs", "speaker"),
        ("profile_docs", "profile"),
        ("cluster_docs", "cluster"),
    ):
        found = count_speaker_docs_by_ids(
            ids[kind], also=org_stamp_clause(scope, kind), errors=errors
        )
        if found is None:
            result.unverified.append(key)
        else:
            result.counts[key] = found
    return result


def measure_gender_residual(
    scope: PurgeScope, *, session_factory: Callable[[], Session]
) -> Residual:
    """Speakers in scope still carrying an inferred (unconfirmed) gender."""
    from app.models.media import Speaker

    db = session_factory()
    try:
        remaining = _count(
            db,
            select(func.count())
            .select_from(Speaker)
            .where(
                *scope.speaker_filters(),
                or_(
                    Speaker.gender_confirmed_by_user.is_(None),
                    Speaker.gender_confirmed_by_user.is_(False),
                ),
                or_(
                    Speaker.predicted_gender.isnot(None),
                    Speaker.attribute_confidence.has_key("gender"),
                    Speaker.attribute_confidence.has_key("alignment"),
                    Speaker.attribute_confidence.has_key("alignment_hint"),
                ),
            ),
        )
    finally:
        db.close()
    return Residual(counts={"speakers_with_inferred_gender": remaining})
