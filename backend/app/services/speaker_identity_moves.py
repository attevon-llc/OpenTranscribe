"""The writes that move a speaker's identity: unlink, join a cluster, detach (issue #1233).

Split from ``speaker_cleanup`` by concern. Each function changes Postgres rows and hands
back what it touched; the caller commits, flushes its tracker and passes the returned
:class:`~app.services.speaker_cleanup.SpeakerImpact` to ``reconcile_speaker_impact``.

* :func:`detach_from_cluster` — unassigning a speaker from a cluster means that section is
  no longer that person: it leaves the cluster, leaves the cluster's promoted profile
  (:func:`unlink_from_profile`: a name that came from the profile is cleared, a name the
  user typed stays) and, when blacklisting, is recorded as cannot-link against every
  remaining member and as blacklisted from the profile
  (:func:`~app.services.speaker_identity_rules.record_unassignment`).
  The cluster and profile remain for everyone else.
* :func:`join_cluster_identity` — joining a promoted cluster links its profile and takes
  its name, the one way every path propagates a cluster's identity.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING
from typing import Literal

from sqlalchemy.orm import Session

from app.services.speaker_cleanup import SpeakerImpact
from app.services.speaker_identity_rules import record_unassignment
from app.services.speaker_identity_rules import speaker_blocked_from_profile
from app.utils.speaker_identity_detail import ORIGIN_CLUSTER_JOIN
from app.utils.speaker_identity_detail import ORIGIN_CLUSTER_MERGED
from app.utils.speaker_identity_detail import voiceprint_detail
from app.utils.speaker_identity_source import after_unlink
from app.utils.speaker_identity_source import clear_suggestion
from app.utils.speaker_identity_source import record_human_identity
from app.utils.speaker_identity_source import record_machine_identity

if TYPE_CHECKING:
    from app.models.speaker import Speaker
    from app.models.speaker import SpeakerCluster
    from app.services.speaker_rename_tracker import SpeakerRenameTracker


def unlink_from_profile(
    db: Session,
    speakers: Iterable[Speaker],
    profile_id: int,
    tracker: SpeakerRenameTracker,
) -> set[int]:
    """Take a profile's identity back from these speakers. Returns the ids it unlinked.

    The rule the profile delete established: the link goes, ``verified`` goes, and a
    display name or suggestion that IS the profile's name goes with it (it came from the
    profile), while a name the user set to something else survives. The chunk plane is
    told through ``tracker``, which the caller flushes after its commit. A speaker linked
    to a different profile is not touched.
    """
    from app.models.speaker import SpeakerProfile
    from app.utils.speaker_labels import canonical_speaker_label_for_row

    name = db.query(SpeakerProfile.name).filter(SpeakerProfile.id == int(profile_id)).scalar()
    unlinked: set[int] = set()
    for speaker in speakers:
        if speaker.profile_id is None or int(speaker.profile_id) != int(profile_id):
            continue
        indexed_as = canonical_speaker_label_for_row(speaker)
        speaker.profile_id = None  # type: ignore[assignment]
        speaker.verified = False  # type: ignore[assignment]
        if name is not None and speaker.display_name == name:
            speaker.display_name = None  # type: ignore[assignment]
        if name is not None and speaker.suggested_name == name:
            clear_suggestion(speaker)
        # Settle who made what is left, before the "after" label below depends on it (#1280).
        after_unlink(speaker)
        tracker.record(
            int(speaker.media_file_id), indexed_as, canonical_speaker_label_for_row(speaker)
        )
        unlinked.add(int(speaker.id))
    return unlinked


def join_cluster_identity(
    db: Session,
    speaker: Speaker,
    cluster: SpeakerCluster,
    tracker: SpeakerRenameTracker,
    *,
    source: Literal["voiceprint", "human"],
    user_id: int | None = None,
    confidence: float | None = None,
) -> set[int]:
    """Joining a promoted cluster links its profile and takes its name.

    The one way every assignment path propagates a cluster's identity. Refuses (returns
    an empty set, writes nothing) when the speaker is blocked from the profile.

    ``source`` says who decided the join (issue #1280): ``"voiceprint"`` when a voice
    comparison placed the speaker in the cluster (upload-time clustering, Re-cluster),
    ``"human"`` when a person did (merging clusters). It is required so that no caller can
    forget to say. ``user_id`` is the person (``source="human"``); ``confidence`` the cluster
    match similarity (``source="voiceprint"``). Both go into the recorded origin.

    Returns:
        The profiles whose membership changed (the new one and any it left), for the
        caller's :class:`SpeakerImpact`; empty when nothing changed.
    """
    from app.models.speaker import SpeakerProfile
    from app.utils.speaker_labels import canonical_speaker_label_for_row

    profile_id = cluster.promoted_to_profile_id
    if profile_id is None or speaker_blocked_from_profile(db, int(speaker.id), int(profile_id)):
        return set()
    profile_row = (
        db.query(SpeakerProfile.name, SpeakerProfile.uuid)
        .filter(SpeakerProfile.id == int(profile_id))
        .first()
    )
    if profile_row is None:
        return set()
    name, profile_uuid = profile_row
    old = int(speaker.profile_id) if speaker.profile_id is not None else None
    indexed_as = canonical_speaker_label_for_row(speaker)
    previous_display_name = speaker.display_name
    speaker.profile_id = int(profile_id)  # type: ignore[assignment]
    speaker.display_name = name  # type: ignore[assignment]
    speaker.verified = True  # type: ignore[assignment]
    if source == "voiceprint":
        from app.services.embedding_mode_service import voiceprint_model_id

        record_machine_identity(
            speaker,
            previous_display_name=previous_display_name,
            detail=voiceprint_detail(
                ORIGIN_CLUSTER_JOIN,
                confidence=confidence,
                matched_profile_uuid=profile_uuid,
                embedding_model=voiceprint_model_id(),
            ),
        )
    else:
        record_human_identity(speaker, user_id=user_id, origin=ORIGIN_CLUSTER_MERGED)
    tracker.record(int(speaker.media_file_id), indexed_as, canonical_speaker_label_for_row(speaker))
    return {p for p in (old, int(profile_id)) if p is not None}


def detach_from_cluster(
    db: Session,
    cluster: SpeakerCluster,
    speakers: list[Speaker],
    *,
    blacklist: bool,
    tracker: SpeakerRenameTracker,
) -> SpeakerImpact:
    """Unassign speakers from a cluster: that section is no longer that person.

    Removes both membership representations, unlinks the cluster's promoted profile
    (:func:`unlink_from_profile`) and, when ``blacklist`` is set, records the speakers
    as cannot-link against every remaining member and as blacklisted from the profile.
    The cluster and its profile remain for the other members; the caller commits, flushes
    ``tracker`` and hands the returned impact to :func:`reconcile_speaker_impact`, which
    rebuilds the centroid and the profile embedding (an emptied cluster is deleted).
    """
    from app.models.speaker import SpeakerClusterMember

    remove_ids = sorted({int(s.id) for s in speakers})
    remaining = [
        int(r[0])
        for r in db.query(SpeakerClusterMember.speaker_id)
        .filter(
            SpeakerClusterMember.cluster_id == cluster.id,
            SpeakerClusterMember.speaker_id.notin_(remove_ids),
        )
        .all()
    ]
    impact = SpeakerImpact(owner_ids={int(cluster.user_id)}, cluster_uuids={str(cluster.uuid)})
    db.query(SpeakerClusterMember).filter(
        SpeakerClusterMember.cluster_id == cluster.id,
        SpeakerClusterMember.speaker_id.in_(remove_ids),
    ).delete(synchronize_session="fetch")
    for speaker in speakers:
        if speaker.cluster_id is not None and int(speaker.cluster_id) == int(cluster.id):
            speaker.cluster_id = None  # type: ignore[assignment]

    profile_id = (
        int(cluster.promoted_to_profile_id) if cluster.promoted_to_profile_id is not None else None
    )
    if profile_id is not None:
        unlinked = unlink_from_profile(db, speakers, profile_id, tracker)
        if unlinked:
            impact.profile_ids.add(profile_id)
            impact.relabel_speaker_ids |= unlinked
    if blacklist:
        record_unassignment(db, remove_ids, against_speaker_ids=remaining, profile_id=profile_id)
    cluster.member_count = len(remaining)  # type: ignore[assignment]
    return impact
