import logging

from sqlalchemy.orm import Session

from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.services.privacy import policy as privacy_policy
from app.services.privacy.redact_view import PrivacyView
from app.services.speaker_embedding_service import SpeakerEmbeddingService
from app.services.speaker_matching import match_handling
from app.services.speaker_matching import match_queries
from app.services.speaker_matching import match_storage
from app.services.speaker_matching import processing
from app.services.speaker_matching import profiles
from app.services.speaker_matching import propagation
from app.services.speaker_matching.thresholds import ConfidenceLevel
from app.services.speaker_rename_tracker import SpeakerRenameTracker

logger = logging.getLogger(__name__)


class SpeakerMatchingService:
    """Service for matching speakers across media files with confidence levels.

    Tenant scope (cloud edition): every kNN query and OpenSearch write this
    service performs is gated on ``organization_id``:

    * **Per-file speaker docs** (voiceprints of a speaker instance in one media
      file) follow the FILE's ``organization_id`` — resolved from the DB via
      ``_media_file_org_id`` and passed to both the store calls and the match
      queries, matching what the transcription pipeline stamps in the v4
      staging path (``ctx.organization_id``).
    * **Profile docs** (cross-video profile centroids) mirror the
      ``SpeakerProfile.organization_id`` row value at write time and are
      queried with the current file's org, so cross-video matching works
      within an org without leaking into personal scope or other orgs.

    Community edition: files and profiles carry ``organization_id`` NULL, so
    every resolved scope is None, docs stay org-less, and the personal gate
    (``must_not exists organization_id``) is a behavior-preserving no-op.
    """

    def __init__(self, db: Session, embedding_service: SpeakerEmbeddingService | None):
        self.db = db
        self.embedding_service = embedding_service
        # Auto-accepted matches rename speakers whose chunks may already be
        # indexed — the cloud-ASR path extracts embeddings on the GPU worker in
        # parallel with chunk indexing, and rediarize re-matches a long-completed
        # file. Recorded here, dispatched after the commit (issue #432).
        self._rename_tracker = SpeakerRenameTracker()

    def _media_file_org_id(self, media_file_id: int | None) -> int | None:
        """Resolve the tenant scope of a per-file speaker operation.

        The org of a speaker instance is defined by the media file it belongs
        to (``media_file.organization_id``), never by caller-supplied state.

        Args:
            media_file_id: Media file ID (None-safe).

        Returns:
            The file's organization_id, or None for personal/community files.
        """
        if media_file_id is None:
            return None
        row = self.db.query(MediaFile.organization_id).filter(MediaFile.id == media_file_id).first()
        return int(row[0]) if row and row[0] is not None else None

    def _voiceprints_allowed(self, user_id: int, organization_id: int | None) -> bool:
        """Privacy policy (#1280): may voiceprints be matched or stored for this scope?

        Asked fresh at every public entry point (never cached on the instance), so a
        switch flipped while a task was queued or running is honoured. Fails closed.
        """
        return privacy_policy.voiceprint_decision(
            self.db, user_id=user_id, organization_id=organization_id
        ).enabled

    def _scope_gate(
        self, user_id: int, organization_id: OrgScope, *, speaker_id: int | None = None
    ) -> tuple[bool, int | None]:
        """Decide a public call's privacy scope; return ``(allowed, organization to query in)``.

        A call that names a speaker is judged in that speaker's FILE (its owner and its
        organization, exactly as ``PrivacyView`` reads them), never in what the caller passed:
        ``organization_id=None`` used to mean "personal", so a caller that merely omitted the
        argument escaped an organization's off switch. The argument is only a hint here; if it
        names a different organization it can narrow the answer (both scopes must allow) and
        never widen it. A call with no speaker has nothing to derive the scope from, so an
        omitted organization (``UNSCOPED``) is refused rather than read as personal; an explicit
        ``None`` is a statement of personal scope and is judged as such.
        """
        if speaker_id is not None:
            row = (
                self.db.query(MediaFile.id, MediaFile.user_id, MediaFile.organization_id)
                .join(Speaker, Speaker.media_file_id == MediaFile.id)
                .filter(Speaker.id == speaker_id)
                .first()
            )
            if row is None:
                logger.warning("Speaker %s not found; voiceprint matching refused", speaker_id)
                return False, None
            file_org = int(row[2]) if row[2] is not None else None
            allowed = PrivacyView(self.db).voiceprints(int(row[0])).enabled
            if (
                allowed
                and not isinstance(organization_id, _Unscoped)
                and organization_id != file_org
            ):
                logger.warning(
                    "Speaker %s: organization hint %s differs from its file's %s; the hint may "
                    "only narrow",
                    speaker_id,
                    organization_id,
                    file_org,
                )
                allowed = self._voiceprints_allowed(user_id, organization_id)
            return allowed, file_org
        if isinstance(organization_id, _Unscoped):
            logger.error(
                "Voiceprint matching refused: the call named neither a speaker nor a scope"
            )
            return False, None
        return self._voiceprints_allowed(user_id, organization_id), organization_id

    def get_confidence_level(self, confidence: float) -> str:
        """
        Determine the confidence level for UI display.

        Args:
            confidence: Confidence score (0-1)

        Returns:
            'high', 'medium', or 'low'
        """
        if confidence >= ConfidenceLevel.HIGH:
            return "high"
        elif confidence >= ConfidenceLevel.MEDIUM:
            return "medium"
        else:
            return "low"

    match_speaker_to_known_speakers = match_queries.match_speaker_to_known_speakers
    match_speaker_to_profiles = match_queries.match_speaker_to_profiles
    find_unlabeled_speaker_matches = match_queries.find_unlabeled_speaker_matches
    match_speaker_to_profile = match_queries.match_speaker_to_profile

    create_speaker_profile = profiles.create_speaker_profile
    assign_speaker_to_profile = profiles.assign_speaker_to_profile
    update_speaker_embeddings = profiles.update_speaker_embeddings
    add_speaker_to_collection = profiles.add_speaker_to_collection
    find_speaker_occurrences = profiles.find_speaker_occurrences

    process_speaker_segments = processing.process_speaker_segments
    process_speaker_embeddings_native = processing.process_speaker_embeddings_native
    _process_single_speaker = processing.process_single_speaker

    _linked_speaker_count = match_handling.linked_speaker_count
    _refusals = match_handling.refusals
    _match_is_blocked = match_handling.match_is_blocked
    _match_detail = match_handling.match_detail
    _record_match_suggestion = staticmethod(match_handling.record_match_suggestion)

    _handle_speaker_match = match_handling.handle_speaker_match
    _handle_no_speaker_match = match_handling.handle_no_speaker_match

    _get_speaker_embedding_for_propagation = propagation.get_speaker_embedding_for_propagation
    _should_propagate_to_speaker = propagation.should_propagate_to_speaker
    _update_speakers_in_opensearch = propagation.update_speakers_in_opensearch
    _propagate_profile_assignment = propagation.propagate_profile_assignment

    _build_speaker_match_query = match_storage.build_speaker_match_query
    _create_speaker_match_record = match_storage.create_speaker_match_record
    _build_match_result = match_storage.build_match_result
    _process_match_hit = match_storage.process_match_hit
    _perform_opensearch_query = match_storage.perform_opensearch_query
    find_and_store_speaker_matches = match_storage.find_and_store_speaker_matches
    get_speaker_matches = match_storage.get_speaker_matches
