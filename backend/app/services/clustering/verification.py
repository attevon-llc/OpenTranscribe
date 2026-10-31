"""The speaker Inbox: unverified speakers and batch verify/assign/name/skip."""

import logging
import math
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy.orm import joinedload

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerProfile
from app.services.permission_service import org_scope_pred
from app.services.privacy.redact_view import redact_speaker_rows
from app.services.speaker_cleanup import SpeakerImpact
from app.services.speaker_cleanup import clusters_holding_speakers
from app.services.speaker_cleanup import raise_if_incomplete
from app.services.speaker_cleanup import reconcile_speaker_impact
from app.services.speaker_identity_rules import blocked_profile_names
from app.services.speaker_identity_rules import blocking_profile_ids_named
from app.services.speaker_identity_rules import lift_profile_blacklist
from app.services.speaker_identity_rules import speaker_blocked_from_profile
from app.services.takedown_service import exclude_quarantined
from app.utils.speaker_identity_detail import ORIGIN_ACCEPTED_SUGGESTION
from app.utils.speaker_identity_detail import ORIGIN_BATCH_ASSIGN
from app.utils.speaker_identity_detail import ORIGIN_PROFILE_ASSIGNED
from app.utils.speaker_identity_detail import ORIGIN_TYPED
from app.utils.speaker_identity_source import after_skip
from app.utils.speaker_identity_source import mark_review_skipped
from app.utils.speaker_identity_source import record_human_identity
from app.utils.speaker_labels import canonical_speaker_label_for_row

if TYPE_CHECKING:
    from app.services.speaker_clustering_service import SpeakerClusteringService

logger = logging.getLogger(__name__)


def get_unverified_speakers(
    self: "SpeakerClusteringService",
    user_id: int,
    page: int = 1,
    per_page: int = 20,
    *,
    include_quarantined: bool = False,
    organization_id: OrgScope = UNSCOPED,
) -> dict[str, Any]:
    """Get paginated list of unverified speakers across the files of one tenant.

    Prioritized by: cluster size (larger first), then suggestion confidence.

    Args:
        user_id: Owner user ID.
        page: Page number (1-based).
        per_page: Items per page.
        include_quarantined: Admin "see all" — skip the takedown exclusion below.

    Returns:
        Dict with items, total, page, per_page, pages.
    """
    query = (
        self.db.query(Speaker)
        .options(
            joinedload(Speaker.media_file),
            joinedload(Speaker.cluster),
        )
        .filter(
            Speaker.user_id == user_id,
            Speaker.verified.is_(False),
            Speaker.profile_id.is_(None),
        )
        .outerjoin(SpeakerCluster, Speaker.cluster_id == SpeakerCluster.id)
        .order_by(
            SpeakerCluster.member_count.desc().nullslast(),
            Speaker.confidence.asc().nullslast(),
            Speaker.created_at.desc(),
        )
    )
    # `Speaker.media_file_id` is never NULL, so this inner join drops no
    # legitimate row. Applied BEFORE `.count()` — filtering `items` alone after
    # pagination would leak the true total and silently shorten pages, rather
    # than actually excluding the quarantined speaker from the inbox.
    query = query.join(MediaFile, Speaker.media_file_id == MediaFile.id)
    query = exclude_quarantined(query, include_quarantined=include_quarantined)
    tenant_pred = org_scope_pred(MediaFile.organization_id, organization_id)
    if tenant_pred is not None:
        query = query.filter(tenant_pred)

    total = query.count()
    pages = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page
    speakers = query.offset(offset).limit(per_page).all()

    # Privacy policy (#1280): withhold stored voice suggestions and inferred gender the
    # owner's switches forbid, and the cluster a file's speaker sits in while voiceprints
    # are off for it. The inbox itself (unnamed speakers to review) stays.
    redact_speaker_rows(self._privacy, speakers)
    voiceprints_off = self._privacy.voiceprints_off_file_ids(int(s.media_file_id) for s in speakers)

    items = []
    for speaker in speakers:
        media_file = speaker.media_file
        cluster = None if int(speaker.media_file_id) in voiceprints_off else speaker.cluster

        items.append(
            {
                "speaker_uuid": str(speaker.uuid),
                "speaker_name": speaker.name,
                "display_name": speaker.display_name,
                "suggested_name": speaker.suggested_name,
                "suggestion_source": speaker.suggestion_source,
                "confidence": float(speaker.confidence) if speaker.confidence else None,
                "media_file_uuid": str(media_file.uuid) if media_file else None,
                "media_file_title": (
                    media_file.title or media_file.filename if media_file else None
                ),
                "media_file_duration": float(media_file.duration)
                if media_file and media_file.duration
                else None,
                "cluster_uuid": str(cluster.uuid) if cluster else None,
                "cluster_label": cluster.label if cluster else None,
                "cluster_member_count": int(cluster.member_count or 0) if cluster else 0,
                "verified": False,
                "predicted_gender": speaker.predicted_gender,
                "predicted_age_range": speaker.predicted_age_range,
                "created_at": speaker.created_at.isoformat() if speaker.created_at else None,
            }
        )

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    }


@dataclass
class _Batch:
    """One ``batch_verify_speakers`` call: its inputs, what was refused and what changed."""

    user_id: int
    action: str
    profile: SpeakerProfile | None
    display_name: str | None
    override_unassignment: bool
    speaker_count: int
    refused_names: dict[int, set[str]]
    # "assign" moves speakers into ``profile`` and out of any profile they had.
    touched: SpeakerImpact
    assigned: set[int] = field(default_factory=set)
    updated: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    blocked_uuids: list[str] = field(default_factory=list)

    def fail(self, error: str) -> None:
        self.errors.append(error)
        self.failed += 1

    def refuse(self, suuid: str, error: str) -> None:
        """A correction the user recorded forbids this speaker: listed, explained, counted."""
        self.blocked_uuids.append(suuid)
        self.fail(error)


def _lookup_assign_profile(
    self: "SpeakerClusteringService",
    profile_uuid: str,
    user_id: int,
    organization_id: OrgScope,
) -> SpeakerProfile | None:
    profile_query = self.db.query(SpeakerProfile).filter(
        SpeakerProfile.uuid == profile_uuid, SpeakerProfile.user_id == user_id
    )
    profile_pred = org_scope_pred(SpeakerProfile.organization_id, organization_id)
    if profile_pred is not None:
        profile_query = profile_query.filter(profile_pred)
    profile: SpeakerProfile | None = profile_query.first()
    return profile


def _apply_name(
    self: "SpeakerClusteringService",
    b: _Batch,
    speaker: Speaker,
    suuid: str,
    name: str,
    indexed_as: str,
) -> None:
    """ "accept" (the suggestion) and "name" (the typed name): set the display name."""
    if str(name).casefold() in b.refused_names.get(int(speaker.id), set()):
        if not b.override_unassignment:
            b.refuse(
                str(suuid),
                f"Speaker {suuid} was unassigned from that identity; "
                "name it individually to override",
            )
            return
        for blocking in blocking_profile_ids_named(self.db, int(speaker.id), str(name)):
            lift_profile_blacklist(self.db, int(speaker.id), blocking)
    self._rename_tracker.record(int(speaker.media_file_id), indexed_as, str(name))
    speaker.display_name = str(name)  # type: ignore[assignment]
    speaker.verified = True  # type: ignore[assignment]
    record_human_identity(
        speaker,
        user_id=b.user_id,
        origin=ORIGIN_ACCEPTED_SUGGESTION if b.action == "accept" else ORIGIN_TYPED,
    )
    b.updated += 1


def _apply_assign(
    self: "SpeakerClusteringService",
    b: _Batch,
    profile: SpeakerProfile,
    speaker: Speaker,
    suuid: str,
    indexed_as: str,
) -> None:
    """ "assign": link the speaker to the profile and take its name."""
    if speaker_blocked_from_profile(self.db, int(speaker.id), int(profile.id)):
        if not b.override_unassignment:
            b.refuse(
                str(suuid),
                f"Speaker {suuid} was unassigned from this profile; "
                "assign it individually to override",
            )
            return
        lift_profile_blacklist(self.db, int(speaker.id), int(profile.id))
    b.assigned.add(int(speaker.id))
    if speaker.profile_id:
        b.touched.profile_ids.add(int(speaker.profile_id))
    speaker.profile_id = profile.id  # type: ignore[assignment]
    self._rename_tracker.record(int(speaker.media_file_id), indexed_as, str(profile.name))
    speaker.display_name = profile.name  # type: ignore[assignment]
    speaker.verified = True  # type: ignore[assignment]
    record_human_identity(
        speaker,
        user_id=b.user_id,
        origin=ORIGIN_BATCH_ASSIGN if b.speaker_count > 1 else ORIGIN_PROFILE_ASSIGNED,
    )
    b.updated += 1


def _apply_skip(b: _Batch, speaker: Speaker) -> None:
    # Mark as verified (reviewed) so it doesn't reappear in inbox
    speaker.verified = True  # type: ignore[assignment]
    # Only stamp provenance when there is none to destroy. "user_skipped"
    # has no reader anywhere (backend or frontend), so overwriting a real
    # "llm_analysis" here only ever erased the historical marker
    # task_detection_service reads (issue #620 item 4).
    mark_review_skipped(speaker)
    # Reviewed is not confirmed: a voiceprint suggestion stays the voiceprint's.
    after_skip(speaker)
    b.updated += 1


def _verify_speaker(
    self: "SpeakerClusteringService", b: _Batch, suuid: str, speaker: Speaker
) -> None:
    """Apply the batch's action to one speaker."""
    # Every branch below overwrites the name the chunk plane was
    # indexed with, so each records the old one first (issue #432).
    # "accept" applies a DIFFERENT name per speaker, which is why the
    # tracker groups by new name rather than assuming one per batch.
    indexed_as = canonical_speaker_label_for_row(speaker)

    if b.action in ("accept", "name") and (
        name := speaker.suggested_name if b.action == "accept" else b.display_name
    ):
        _apply_name(self, b, speaker, suuid, name, indexed_as)
    elif b.action == "accept":
        b.fail(f"Speaker {suuid} has no suggestion to accept")
    elif b.action == "assign" and b.profile:
        _apply_assign(self, b, b.profile, speaker, suuid, indexed_as)
    elif b.action == "skip":
        _apply_skip(b, speaker)
    else:
        b.fail(f"Invalid action '{b.action}' or missing parameters")


def _is_unreachable(
    speaker: Speaker,
    file_org: int | None,
    user_id: int,
    organization_id: OrgScope,
    profile: SpeakerProfile | None,
) -> bool:
    """Not the caller's, outside the tenant, or in a different tenant than the profile."""
    return (
        int(speaker.user_id) != user_id
        or (not isinstance(organization_id, _Unscoped) and file_org != organization_id)
        or (profile is not None and file_org != profile.organization_id)
    )


def batch_verify_speakers(
    self: "SpeakerClusteringService",
    speaker_uuids: list[str],
    user_id: int,
    action: str = "accept",
    profile_uuid: str | None = None,
    display_name: str | None = None,
    *,
    organization_id: OrgScope = UNSCOPED,
    override_unassignment: bool = False,
) -> dict[str, Any]:
    """Batch verify/name multiple speakers.

    ``organization_id`` confines the profile and the speakers to one tenant;
    a speaker is only assigned to a profile of its own file's tenant.

    A bulk action honours every correction the user recorded (an unassignment, a
    rejection): the corrected speaker is refused, listed in ``blocked_speaker_uuids``
    and explained in ``errors``, and the rest of the batch proceeds (issue #1233).
    ``override_unassignment`` is the one explicit way past it, and only for ONE
    speaker — the Inbox's per-speaker action — so a correction is never lifted by a
    batch the user did not look at speaker by speaker.

    Args:
        speaker_uuids: List of speaker UUIDs to verify.
        user_id: Owner user ID.
        action: "accept" (apply suggestion), "assign" (assign to profile), "name" (set display_name), "skip" (mark as reviewed/skipped).
        profile_uuid: Profile UUID (for "assign" action).
        display_name: Display name (for "name" action).
        override_unassignment: Lift the correction that blocks this one speaker.

    Returns:
        Summary dict with updated_count, failed_count, errors, blocked_speaker_uuids.

    Raises:
        ValueError: ``override_unassignment`` with other than exactly one speaker.
    """
    if override_unassignment and len(speaker_uuids) != 1:
        raise ValueError("An unassignment can only be overridden for one speaker at a time")

    profile = None
    if action == "assign" and profile_uuid:
        profile = _lookup_assign_profile(self, profile_uuid, user_id, organization_id)
        if not profile:
            return {
                "updated_count": 0,
                "failed_count": len(speaker_uuids),
                "errors": ["Profile not found"],
            }

    # Batch-fetch all speakers (with their file's tenant) to avoid per-UUID queries
    speaker_rows = (
        self.db.query(Speaker, MediaFile.organization_id)
        .join(MediaFile, Speaker.media_file_id == MediaFile.id)
        .filter(Speaker.uuid.in_([str(u) for u in speaker_uuids]))
        .all()
    )
    speakers_by_uuid = {str(s.uuid): s for s, _ in speaker_rows}
    file_org_by_uuid = {str(s.uuid): org for s, org in speaker_rows}
    b = _Batch(
        user_id=user_id,
        action=action,
        profile=profile,
        display_name=display_name,
        override_unassignment=override_unassignment,
        speaker_count=len(speaker_uuids),
        # A bulk action is exactly where a corrected speaker gets swept back in unnoticed,
        # so it honours the blacklist and reports each refusal; naming one speaker on
        # its own is the explicit override (issue #1233).
        refused_names=blocked_profile_names(self.db, [int(s.id) for s, _ in speaker_rows]),
        touched=SpeakerImpact(owner_ids={user_id}),
    )

    for suuid in speaker_uuids:
        try:
            speaker = speakers_by_uuid.get(str(suuid))
            file_org = file_org_by_uuid.get(str(suuid))
            if speaker is None or _is_unreachable(
                speaker, file_org, user_id, organization_id, profile
            ):
                b.fail(f"Speaker {suuid} not found")
                continue
            _verify_speaker(self, b, suuid, speaker)

        except Exception as e:
            # `errors` is the response body of this endpoint's batch-verify
            # route (api/endpoints/speaker_clusters.py) -- never interpolate
            # the raw exception text, only its class name (#914).
            logger.exception("batch_verify_speakers: error processing speaker %s", suuid)
            b.fail(f"Error processing speaker {suuid} ({type(e).__name__})")

    try:
        self.db.commit()
    except Exception as e:
        self.db.rollback()
        # The renames never landed — dropping them keeps a later flush on the
        # same service instance from propagating a rolled-back name.
        self._rename_tracker.discard()
        logger.exception("batch_verify_speakers: commit failed")
        return {
            "updated_count": 0,
            "failed_count": len(speaker_uuids),
            "errors": [f"Commit failed ({type(e).__name__})"],
            "blocked_speaker_uuids": [],
        }

    self._rename_tracker.flush(self.db)
    if b.assigned and profile is not None:
        # The target profile gains the voices and each former profile loses them; the
        # speakers' clusters re-weight them (profile-linked members count double) or,
        # when promoted to another profile, release them; their own documents carry
        # the new profile.
        b.touched.profile_ids.add(int(profile.id))
        b.touched.cluster_uuids = clusters_holding_speakers(self.db, b.assigned)
        b.touched.relabel_speaker_ids |= b.assigned
        raise_if_incomplete(reconcile_speaker_impact(self.db, b.touched))
    return {
        "updated_count": b.updated,
        "failed_count": b.failed,
        "errors": b.errors,
        "blocked_speaker_uuids": b.blocked_uuids,
    }
